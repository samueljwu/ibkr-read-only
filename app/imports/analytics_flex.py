"""Catch up completed account valuations and external flows through IBKR Flex."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import xml.etree.ElementTree as ET

from app.imports.flex_import import DEFAULT_REQUEST_PACING_SECONDS, FlexRequestPacer, FlexServiceError, fetch_flex, weekends_only
from app.storage.secret_store import load_flex_token
from app.sync.sync_service import single_writer_lock
from app.sync.history_coverage import catchup_windows, remember_account_open_day


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _day(value):
    value = (value or "")[:10].replace("-", "")
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        raise ValueError("Invalid Flex report date") from None


def _amount(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid Flex {label}") from None
    if not number.is_finite():
        raise ValueError(f"Invalid Flex {label}")
    return number


def parse_analytics_flex(xml, account_id, first, last):
    """Return dated USD NAV and external-flow records from one account report."""
    root = ET.fromstring(xml)
    statements = [node for node in root.iter() if _tag(node) == "FlexStatement"]
    if not statements:
        raise ValueError("Analytics Flex report contains no statements")
    valuations = {}
    flows = []
    seen = set()
    cash_occurrences = defaultdict(int)
    intervals = []

    def add_flow(kind, node, day, amount):
        if not first <= day <= last:
            return
        transaction_id = node.get("transactionID")
        if transaction_id:
            key = f"id:{transaction_id}"
        elif kind == "cash":
            # Older Flex templates may omit Transaction ID. Keep identical
            # cash rows distinct when falling back to their contents.
            row = repr(sorted(node.attrib.items())).encode("utf-8")
            fingerprint = sha256(row).hexdigest()[:24]
            cash_occurrences[fingerprint] += 1
            key = f"row:{fingerprint}:{cash_occurrences[fingerprint]}"
        else:
            raise ValueError("Analytics Flex query needs Transaction ID for asset transfers")
        source_id = f"flex-analytics:{account_id}:{kind}:{key}"
        if source_id in seen:
            raise ValueError("Duplicate external-flow transaction in Flex report")
        seen.add(source_id)
        flows.append((source_id, day, float(amount), kind))

    cash_by_day = defaultdict(list)
    for statement in statements:
        start, end = _day(statement.get("fromDate")), _day(statement.get("toDate"))
        if statement.get("accountId") != account_id or not first <= start <= end <= last:
            raise ValueError("Analytics Flex account or date range mismatch")
        intervals.append((start, end))
        sections = {_tag(child): child for child in statement}
        if not {"EquitySummaryInBase", "CashTransactions", "Transfers"}.issubset(sections):
            raise ValueError("Analytics Flex query needs NAV in Base, Cash Transactions, and Transfers sections")
        bases = {statement.get("baseCurrency")}
        for node in statement.iter():
            if _tag(node) == "AccountInformation":
                if node.get("accountId") not in (None, "", account_id):
                    raise ValueError("Analytics Flex Account Information account mismatch")
                bases.add(node.get("baseCurrency"))
                # IBKR's Account Information 'Currency' field is the account
                # base currency; current reports emit it as currency="USD".
                bases.add(node.get("currency"))
        if any(base not in (None, "", "USD") for base in bases):
            raise ValueError("Analytics currently requires a USD base account")
        if "USD" not in bases:
            raise ValueError("Analytics Flex report must identify USD as the base currency. "
                             "Include Account Information > Currency in the Analytics Flex query.")
        for node in sections["EquitySummaryInBase"]:
            if _tag(node) != "EquitySummaryByReportDateInBase":
                continue
            day = _day(node.get("reportDate"))
            # A daily breakout includes the previous valuation as context.
            if not start <= day <= end:
                continue
            if day in valuations:
                raise ValueError("Duplicate Flex NAV date")
            nav = _amount(node.get("total"), "NAV")
            if nav < 0:
                raise ValueError("Flex NAV cannot be negative")
            cash = node.get("cash")
            valuations[day] = (float(nav), float(_amount(cash, "cash")) if cash not in (None, "") else None)
        for node in sections["CashTransactions"]:
            if _tag(node) != "CashTransaction" or (node.get("type") or "").lower() not in {
                "deposits/withdrawals", "deposits and withdrawals", "deposit", "withdrawal"
            }:
                continue
            day = _day(node.get("reportDate") or node.get("dateTime"))
            currency = node.get("currency") or "USD"
            rate = _amount(node.get("fxRateToBase") or ("1" if currency == "USD" else None), "FX rate")
            if rate <= 0:
                raise ValueError("Flex external-flow FX rate must be positive")
            amount = _amount(node.get("amount"), "cash flow") * rate
            add_flow("cash", node, day, amount)
            cash_by_day[day].append(amount)
        for node in sections["Transfers"]:
            if _tag(node) != "Transfer":
                continue
            day = _day(node.get("reportDate") or node.get("date"))
            direction = (node.get("direction") or "").upper()
            if direction not in ("IN", "OUT"):
                raise ValueError("Unknown Flex transfer direction")
            if node.get("assetCategory") == "CASH":
                currency = node.get("currency") or "USD"
                rate = _amount(node.get("fxRateToBase") or ("1" if currency == "USD" else None), "FX rate")
                if rate <= 0:
                    raise ValueError("Flex transfer FX rate must be positive")
                amount = abs(_amount(node.get("cashTransfer"), "cash transfer") * rate)
                amount = amount if direction == "IN" else -amount
                if amount in cash_by_day[day]:
                    cash_by_day[day].remove(amount)
                    continue
            else:
                amount = abs(_amount(node.get("positionAmountInBase"), "asset transfer"))
                amount = amount if direction == "IN" else -amount
            add_flow("transfer", node, day, amount)
    covered_through = None
    for start, end in sorted(intervals):
        if covered_through is None:
            if not weekends_only(first, (date.fromisoformat(start) - timedelta(days=1)).isoformat()):
                raise ValueError("Analytics Flex report does not cover the requested dates")
        elif not weekends_only(
                (date.fromisoformat(covered_through) + timedelta(days=1)).isoformat(),
                (date.fromisoformat(start) - timedelta(days=1)).isoformat()):
            raise ValueError("Analytics Flex report has a date gap")
        covered_through = max(covered_through or end, end)
    # Daily breakout statements omit weekends. Keep their NAV dates as supplied
    # and still reject an omitted weekday, including at either request boundary.
    if not weekends_only((date.fromisoformat(covered_through) + timedelta(days=1)).isoformat(), last):
        raise ValueError("Analytics Flex report does not cover the requested dates")
    if (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 > 30:
        # A year-wide statement header alone cannot prove daily NAV coverage.
        day = date.fromisoformat(first)
        through = date.fromisoformat(last)
        while day <= through:
            if day.weekday() < 5 and day.isoformat() not in valuations:
                raise ValueError(f'Analytics Flex report is missing daily NAV for {day}; '
                                 'enable daily breakout in the Analytics Flex query')
            day += timedelta(days=1)
    return valuations, flows


def planned_analytics_windows(db, account_id, today):
    return catchup_windows(db, account_id, 'analytics', today, 30)


def run_analytics_flex_sync(db, account_id, query_id, *, portfolio_id=1, today=None, token=None,
                            fetcher=fetch_flex, pacing_seconds=DEFAULT_REQUEST_PACING_SECONDS, windows_override=None,
                            on_window_start=None, on_window_complete=None,
                            on_window_unavailable=None, request_pacer=None,
                            stop_on_unavailable=False, backup=True):
    """Replay a dated Flex report without requiring a TWS/Gateway session."""
    if not account_id or not query_id:
        raise ValueError("IBKR account and Analytics Flex query ID are required")
    db.validate_account_ownership(account_id)
    today = today or date.today()
    secret = token or load_flex_token(account_id)
    if not secret:
        raise ValueError("IBKR Flex token is missing; renew it in Client Portal")
    with single_writer_lock(db.path):
        windows = windows_override if windows_override is not None else planned_analytics_windows(db, account_id, today)
        if windows and backup:
            db.backup()
        count = 0
        pacer = request_pacer or FlexRequestPacer(pacing_seconds)
        pacer.configure(db, account_id)
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
                valuations, flows = parse_analytics_flex(xml, account_id, first, last)
            except (ValueError, ET.ParseError):
                pacer.invalidate()
                raise
            with db.conn:
                remember_account_open_day(db, account_id, xml)
                db.conn.execute("""DELETE FROM account_history WHERE portfolio_id=?
                    AND source='flex' AND day BETWEEN ? AND ?""", (portfolio_id, first, last))
                db.conn.execute("""DELETE FROM external_flows WHERE portfolio_id=?
                    AND source_id LIKE ? AND day BETWEEN ? AND ?""",
                    (portfolio_id, f"flex-analytics:{account_id}:%", first, last))
                db.conn.execute("""DELETE FROM external_flows WHERE portfolio_id=?
                    AND source_id LIKE 'history-csv:%' AND day BETWEEN ? AND ?""",
                    (portfolio_id, first, last))
                for day, (nav, cash) in valuations.items():
                    db.conn.execute("""INSERT INTO account_history
                        (portfolio_id,day,nav_usd,cash_usd,daily_pnl_usd,source,flow_covered)
                        VALUES (?,?,?,?,NULL,'flex',1)
                        ON CONFLICT(portfolio_id,day) DO UPDATE SET
                        nav_usd=excluded.nav_usd,cash_usd=excluded.cash_usd,
                    daily_pnl_usd=NULL,source='flex',flow_covered=1""", (portfolio_id, day, nav, cash))
                db.conn.executemany("""INSERT INTO external_flows
                    (portfolio_id,source_id,day,amount_usd,kind) VALUES (?,?,?,?,?)""",
                    [(portfolio_id, *flow) for flow in flows])
                db.conn.execute("""INSERT INTO broker_sync_windows
                    (account_id,source,from_day,to_day,status,source_count,imported_count)
                    VALUES (?,'flex_analytics',?,?,'complete',?,?)
                    ON CONFLICT(account_id,source,from_day,to_day) DO UPDATE SET
                    status='complete',source_count=excluded.source_count,
                    imported_count=excluded.imported_count,completed_at=CURRENT_TIMESTAMP""",
                    (account_id, first, last, len(valuations), len(valuations)))
            count += len(valuations)
            processed += 1
            if on_window_complete:
                on_window_complete(first, last)
        db.set_metadata("ibkr_analytics_sync_completed", datetime.now().isoformat(timespec="seconds"))
        if not unavailable:
            db.set_metadata('ibkr_flex_paused', '')
        result = {"windows": processed, "valuations": count}
        if unavailable:
            result["unavailable_windows"] = unavailable
        return result
