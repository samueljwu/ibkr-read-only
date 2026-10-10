"""Validated, replay-safe Cash history from an Activity Flex query."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import xml.etree.ElementTree as ET

from app.imports.flex_import import DEFAULT_REQUEST_PACING_SECONDS, FlexRequestPacer, FlexServiceError, fetch_flex, weekends_only
from app.storage.secret_store import load_flex_token
from app.sync.sync_service import single_writer_lock, check_automatic_flex_sync
from app.sync.history_coverage import catchup_windows, covered_windows, remember_account_open_day
from app.broker.broker_fees import reconcile_fee_adjustments


CENT = Decimal("0.01")


class CashReconciliationError(ValueError):
    """A report failed cash arithmetic; independent sources can still run."""


UNSUPPORTED_CASH_COMPONENTS = (
    'clientFees', 'referralFee', 'commissionCreditsRedemption', 'billableCommissions',
    'carbonCredits', 'donations', 'accountTransfers', 'linkingAdjustments',
    'internalTransfers', 'paxosTransfers', 'excessFundSweep', 'debitCardActivity',
    'billPay', 'insuredDepositInterest', 'brokerFees', 'cashSettlingMtm',
    'realizedVm', 'realizedForexVm', 'cfdCharges', 'advisorFees', 'feesReceivables',
    'taxReceivables', 'withholding871m', 'withholdingCollectedTax', 'salesTax',
    'billableSalesTax', 'ipoSubscription', 'otherIncome', 'other', 'fxTranslationGainLoss',
)


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _day(value):
    try:
        return datetime.strptime((value or "")[:10].replace("-", "")[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        raise ValueError("Invalid Cash Flex date") from None


def _amount(value, label, *, required=False):
    if value in (None, ""):
        if required:
            raise ValueError(f"Cash Flex is missing {label}")
        return Decimal(0)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid Cash Flex {label}") from None
    if not number.is_finite():
        raise ValueError(f"Invalid Cash Flex {label}")
    return number


def _kind(raw, amount):
    name = (raw or "").strip().lower().replace("&", "and")
    if name in ("deposits/withdrawals", "deposits and withdrawals", "deposit", "withdrawal"):
        return "Deposit" if amount >= 0 else "Withdrawal"
    if "payment in lieu" in name:
        return "Payment in lieu"
    if "dividend" in name:
        return "Dividend"
    if "transaction tax" in name:
        return "Transaction tax"
    if "withholding" in name:
        return "Withholding tax"
    if "stock lending" in name or "syep" in name:
        return "Stock lending interest"
    if "interest" in name:
        return "Debit interest" if amount < 0 else "Credit interest"
    if name in ("other fee", "other fees", "fee", "fees"):
        return "Other fee"
    if name == 'commission adjustments':
        return 'Commission adjustment'
    raise ValueError(f"Unsupported Cash Flex transaction type: {raw or '(blank)'}")


def parse_cash_flex(xml, account_id, first, last):
    """Return one complete, currency-specific cash bridge and its transactions."""
    root = ET.fromstring(xml)
    statements = [node for node in root.iter() if _tag(node) == "FlexStatement"]
    if len(statements) != 1:
        raise ValueError("Cash Flex query must return exactly one account statement")
    statement = statements[0]
    if (statement.get("accountId") != account_id or
            _day(statement.get("fromDate")) != first or _day(statement.get("toDate")) != last):
        raise ValueError("Cash Flex account or date range mismatch")
    sections = {_tag(node): node for node in statement}
    if not {"CashReport", "CashTransactions"}.issubset(sections):
        raise ValueError("Cash Flex query needs Cash Report and Cash Transactions sections")
    reports, summaries = {}, []
    for node in sections["CashReport"]:
        if _tag(node) != "CashReportCurrency":
            continue
        code = (node.get("currency") or "").upper()
        if code in ("BASE_SUMMARY", "BASE", "BASE CURRENCY SUMMARY"):
            summaries.append(node)
            continue
        if not code or code in reports or node.get("accountId", account_id) != account_id:
            raise ValueError("Invalid or duplicate Cash Flex currency bridge")
        reports[code] = node
    if "USD" not in reports:
        # IBKR may emit only the base summary for a single-currency period.
        # Prefer native rows whenever provided: a multi-currency aggregate
        # must never be relabelled as a native USD cash balance.
        if reports or len(summaries) != 1:
            raise ValueError("Cash Flex query needs a USD Cash Report row or a verified USD-only base summary")
        bases = {statement.get('baseCurrency')}
        for node in statement.iter():
            if _tag(node) == 'AccountInformation':
                if node.get('accountId') not in (None, '', account_id):
                    raise ValueError('Cash Flex Account Information account mismatch')
                bases.update((node.get('baseCurrency'), node.get('currency')))
        bases.discard(None)
        bases.discard('')
        if bases != {'USD'}:
            raise ValueError('Cash Flex base summary must identify USD as the base currency; '
                             'include Account Information > Currency in the Cash query')
        currencies = {(node.get('currency') or '').upper() for node in sections['CashTransactions']
                      if _tag(node) == 'CashTransaction'}
        if currencies - {'USD'}:
            raise ValueError('Cash Flex base summary includes non-USD transactions; '
                             'include native currency Cash Report rows')
        summary = summaries[0]
        if _amount(summary.get('fxTranslationGainLoss'), 'FX translation', required=True):
            raise CashReconciliationError('Cash Flex base summary includes FX translation; '
                                          'include native currency Cash Report rows')
        reports['USD'] = summary
    for code, node in reports.items():
        if node.get('accountId', account_id) != account_id:
            raise ValueError('Cash Flex currency bridge account mismatch')
        start = _day(node.get("fromDate")) if node.get("fromDate") else first
        end = _day(node.get("toDate")) if node.get("toDate") else last
        # IBKR's currency rows trim weekends even though the enclosing
        # statement retains the requested calendar dates. Never trim weekdays.
        if (not first <= start <= end <= last or
                not weekends_only(first, (date.fromisoformat(start) - timedelta(days=1)).isoformat()) or
                not weekends_only((date.fromisoformat(end) + timedelta(days=1)).isoformat(), last)):
            raise ValueError("Cash Flex currency bridge date mismatch")

    movements = defaultdict(list)
    interest_totals = defaultdict(Decimal)
    occurrences = Counter()
    for node in sections["CashTransactions"]:
        if _tag(node) != "CashTransaction":
            continue
        if node.get("accountId", account_id) != account_id:
            raise ValueError("Cash Flex transaction account mismatch")
        code = (node.get("currency") or "").upper()
        if code not in reports:
            raise ValueError(f"Cash Flex transaction has no {code or 'currency'} bridge")
        amount = _amount(node.get("amount"), "transaction amount", required=True)
        kind = _kind(node.get("type"), amount)
        if kind in ('Credit interest', 'Debit interest', 'Stock lending interest'):
            # Display categories combine interest, but IBKR's Cash Report has
            # separate broker and bond subtotals, including signed reversals.
            raw_type = (node.get('type') or '').strip().lower()
            field = 'bondInterest' if raw_type.startswith('bond interest') else 'brokerInterest'
            interest_totals[code, field] += amount
        reported_day = _day(node.get("reportDate") or node.get("dateTime"))
        if not first <= reported_day <= last:
            raise ValueError("Cash Flex transaction date is outside its statement; review posting date before importing")
        identity = ((account_id, code, node.get('transactionID')) if node.get('transactionID') else
                    (account_id, code, reported_day, kind, str(amount), node.get('description') or ''))
        occurrences[identity] += 1
        key = sha256(json.dumps((*identity, occurrences[identity]),
                                separators=(",", ":")).encode()).hexdigest()
        movements[code].append({
            "source_key": "cash-flex:" + key, "reported_day": reported_day,
            "kind": kind, "currency": code, "amount": str(amount),
            "description": node.get("description") or kind,
            "tradeID": node.get('tradeID') or '',
        })

    bridges = []
    for code, node in sorted(reports.items()):
        opening = _amount(node.get("startingCash"), "starting cash", required=True)
        closing = _amount(node.get("endingCash"), "ending cash", required=True)
        sales = _amount(node.get("netTradesSales"), "trade sales")
        purchases = _amount(node.get("netTradesPurchases"), "trade purchases")
        commissions = _amount(node.get("commissions"), "commissions")
        trade_tax = _amount(node.get("transactionTax"), "transaction tax")
        for field in UNSUPPORTED_CASH_COMPONENTS:
            value = _amount(node.get(field), field)
            if value:
                raise CashReconciliationError(f"Cash Flex {code} has unsupported nonzero {field}={value}")
        rows = movements[code]
        tax_rows = [row for row in rows if row['kind'] == 'Transaction tax']
        if tax_rows:
            detailed_tax = sum((Decimal(row['amount']) for row in tax_rows), Decimal(0))
            if node.get('transactionTax') in (None, '') or abs(detailed_tax - trade_tax) > CENT:
                raise CashReconciliationError(f"Cash Flex {code} transactionTax does not match transactions: "
                                              f"report={trade_tax}, transactions={detailed_tax}")
        # Trade tax belongs to the bridge, not to non-trade cash. Never invent
        # a dated transaction from an aggregate or count detailed tax twice.
        rows = [row for row in rows if row['kind'] != 'Transaction tax']
        totals = defaultdict(Decimal)
        for row in rows:
            if row['kind'] != 'Commission adjustment':
                totals[row["kind"]] += Decimal(row["amount"])
        nontrade = sum(totals.values(), Decimal(0))
        difference = opening + sales + purchases + commissions + trade_tax + nontrade - closing
        if abs(difference) > CENT:
            raise CashReconciliationError(f"Cash Flex {code} bridge differs by {difference}; check missing transaction types or report fields")
        checks = (
            ("depositWithdrawals", totals["Deposit"] + totals["Withdrawal"]),
            ("dividends", totals["Dividend"]),
            ("paymentInLieu", totals["Payment in lieu"]),
            ("withholdingTax", totals["Withholding tax"]),
            ("otherFees", totals["Other fee"]),
            ("brokerInterest", interest_totals[code, 'brokerInterest']),
            ("bondInterest", interest_totals[code, 'bondInterest']),
        )
        for field, actual in checks:
            if node.get(field) not in (None, ""):
                reported = _amount(node.get(field), field)
                subtotal_difference = actual - reported
                if abs(subtotal_difference) > CENT:
                    raise CashReconciliationError(f"Cash Flex {code} {field} does not match transactions: "
                                     f"report={reported}, transactions={actual}, "
                                     f"difference={subtotal_difference} (transactions minus report)")
        bridges.append({
            "currency": code, "period_from": first, "period_to": last,
            "opening_cash": str(opening), "trade_sales": str(sales),
            "trade_purchases": str(purchases), "trade_commissions": str(commissions),
            "trade_tax": str(trade_tax),
            "nontrade_cash": str(nontrade), "closing_cash": str(closing),
            "difference": str(difference), "movements": rows,
        })
    return bridges


def planned_cash_windows(db, portfolio_id, today, account_id=None):
    return catchup_windows(db, account_id or db.setting('ib_account_id'), 'cash', today, 30, portfolio_id)


def run_cash_flex_sync(db, account_id, query_id, *, portfolio_id=1, today=None,
                       token=None, fetcher=fetch_flex, pacing_seconds=DEFAULT_REQUEST_PACING_SECONDS, windows_override=None,
                       on_window_start=None, on_window_complete=None,
                       on_window_unavailable=None, request_pacer=None,
                       stop_on_unavailable=False, backup=True):
    """Fetch completed cash statements; commit only windows whose bridges close."""
    if not account_id or not query_id:
        raise ValueError("IBKR account and Cash Flex query ID are required")
    db.validate_account_ownership(account_id)
    with single_writer_lock(db.path):
        pacer = request_pacer or FlexRequestPacer(pacing_seconds)
        pacer.configure(db, account_id)
        if windows_override is None:
            check_automatic_flex_sync(db, pacer, query_id)
        windows = windows_override if windows_override is not None else planned_cash_windows(db, portfolio_id, today, account_id)
        if not windows:
            return {"windows": 0, "movements": 0}
        secret = token or load_flex_token(account_id)
        if not secret:
            raise ValueError("IBKR Flex token is missing; renew it in Client Portal")
        count = 0
        if backup:
            db.backup()
        pacer.protected_windows = [tuple(row) for row in covered_windows(db, account_id, 'cash', portfolio_id)]
        unavailable = 0
        processed = 0
        def unavailable_window(first, last):
            nonlocal unavailable
            unavailable += 1
            if on_window_unavailable:
                on_window_unavailable(first, last)
        for first, last, xml in pacer.reports(fetcher, query_id, secret, windows,
                on_window_start, unavailable_window if on_window_unavailable else None):
            try:
                bridges = parse_cash_flex(xml, account_id, first, last)
            except (ValueError, ET.ParseError):
                pacer.invalidate()
                raise
            digest = sha256(xml).hexdigest()
            with db.conn:
                remember_account_open_day(db, account_id, xml)
                overlaps = db.conn.execute('''SELECT DISTINCT period_from,period_to
                    FROM cash_reconciliations WHERE portfolio_id=? AND account_id=?
                    AND period_from<=? AND period_to>=?''', (portfolio_id, account_id, last, first)).fetchall()
                if any(start < first or end > last for start, end in overlaps):
                    raise ValueError('Cash reimport must include the complete existing overlapping periods')
                db.conn.execute('''DELETE FROM cash_reconciliations WHERE portfolio_id=? AND account_id=?
                    AND period_from>=? AND period_to<=?''', (portfolio_id, account_id, first, last))
                db.conn.execute('''DELETE FROM cash_movements WHERE portfolio_id=? AND account_id=?
                    AND source_section IN ('Cash Transactions','Deposits & Withdrawals','Dividends',
                        'Withholding Tax','Interest','Fees') AND reported_day BETWEEN ? AND ?''',
                    (portfolio_id, account_id, first, last))
                db.conn.execute('''DELETE FROM trade_fee_adjustments WHERE portfolio_id=? AND account_id=?
                    AND reported_day BETWEEN ? AND ?''', (portfolio_id, account_id, first, last))
                fee_keys = [row[0] for row in db.conn.execute('''SELECT source_id FROM broker_source_records
                    WHERE account_id=? AND source='flex_fee'
                    AND json_extract(raw_json,'$.reported_day') BETWEEN ? AND ?''', (account_id, first, last))]
                for key in fee_keys:
                    db.conn.execute('DELETE FROM trade_fee_adjustments WHERE portfolio_id=? AND account_id=? AND source_key=?',
                                    (portfolio_id, account_id, key))
                    db.conn.execute("DELETE FROM broker_source_records WHERE account_id=? AND source='flex_fee' AND source_id=?",
                                    (account_id, key))
                for bridge in bridges:
                    prior = db.conn.execute("""SELECT closing_cash,period_to FROM cash_reconciliations
                        WHERE portfolio_id=? AND account_id=? AND currency=? AND period_to<?
                        ORDER BY period_to DESC LIMIT 1""",
                        (portfolio_id, account_id, bridge["currency"], first)).fetchone()
                    if (prior and date.fromisoformat(prior[1]) == date.fromisoformat(first) - timedelta(days=1)
                            and abs(Decimal(prior[0]) - Decimal(bridge["opening_cash"])) > CENT):
                        raise CashReconciliationError(f"Cash Flex {bridge['currency']} opening does not match prior close")
                    following = db.conn.execute("""SELECT period_from,opening_cash FROM cash_reconciliations
                        WHERE portfolio_id=? AND account_id=? AND currency=? AND period_from>?
                        ORDER BY period_from LIMIT 1""",
                        (portfolio_id, account_id, bridge["currency"], last)).fetchone()
                    if (following and date.fromisoformat(following[0]) ==
                            date.fromisoformat(last) + timedelta(days=1) and
                            abs(Decimal(following[1]) - Decimal(bridge["closing_cash"])) > CENT):
                        raise CashReconciliationError(f"Cash Flex {bridge['currency']} closing does not match next opening")
                    for row in bridge["movements"]:
                        if row['kind'] == 'Commission adjustment':
                            raw = {**row, 'source_hash': digest}
                            db.conn.execute('''INSERT INTO broker_source_records
                                (account_id,source,source_id,status,issue,raw_json)
                                VALUES (?,'flex_fee',?,'review','Waiting for source execution',?)
                                ON CONFLICT(account_id,source,source_id) DO UPDATE SET
                                raw_json=excluded.raw_json,status='review' ''',
                                (account_id, row['source_key'], json.dumps(raw, sort_keys=True)))
                            continue
                        db.conn.execute("""INSERT INTO cash_movements
                            (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
                             kind,currency,amount,description,source_section,source_hash)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(portfolio_id,account_id,source_key) DO UPDATE SET
                            kind=excluded.kind,amount=excluded.amount,description=excluded.description,
                            reported_day=excluded.reported_day,period_from=excluded.period_from,period_to=excluded.period_to,
                            source_hash=excluded.source_hash""",
                            (portfolio_id, account_id, row["source_key"], row["reported_day"],
                             first, last, row["kind"], row["currency"], row["amount"],
                             row["description"], "Cash Transactions", digest))
                    db.conn.execute("""INSERT INTO cash_reconciliations
                        (portfolio_id,account_id,period_from,period_to,currency,opening_cash,
                         trade_sales,trade_purchases,trade_commissions,trade_tax,nontrade_cash,
                         closing_cash,difference,source_hash)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (portfolio_id, account_id, first, last, bridge["currency"],
                         bridge["opening_cash"], bridge["trade_sales"],
                         bridge["trade_purchases"], bridge["trade_commissions"], bridge["trade_tax"],
                         bridge["nontrade_cash"], bridge["closing_cash"],
                         bridge["difference"], digest))
                    count += len(bridge["movements"])
                reconcile_fee_adjustments(db, account_id, portfolio_id)
            if on_window_complete:
                on_window_complete(first, last)
            processed += 1
        db.set_metadata("ibkr_cash_sync_completed", datetime.now().isoformat(timespec="seconds"))
        if not unavailable:
            db.set_metadata('ibkr_flex_paused', '')
    result = {"windows": processed, "movements": count}
    if unavailable:
        result["unavailable_windows"] = unavailable
    return result
