"""Validated, replay-safe cash transactions from an Activity Flex query."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import xml.etree.ElementTree as ET

from app.flex.flex_import import (MAX_FLEX_WINDOW_DAYS, FlexRequestPacer, FlexServiceError,
                                  FlexValidationError, flex_day, flex_today, fetch_flex)
from app.platform.secret_store import load_flex_token
from app.sync.sync_service import single_writer_lock


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _day(value):
    return flex_day(value)


def _amount(value, label, *, required=False):
    if value in (None, ""):
        if required:
            raise FlexValidationError(f"Cash Flex is missing {label}")
        return Decimal(0)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise FlexValidationError(f"Invalid Cash Flex {label}") from None
    if not number.is_finite():
        raise FlexValidationError(f"Invalid Cash Flex {label}")
    return number


def _kind(raw, amount):
    name = (raw or "").strip().lower().replace("&", "and")
    if name in ("deposits/withdrawals", "deposits and withdrawals", "deposit", "withdrawal"):
        return "Deposit" if amount >= 0 else "Withdrawal"
    if "payment in lieu" in name:
        return "Payment in lieu"
    if "dividend" in name:
        return "Dividend"
    if "withholding" in name or "transaction tax" in name:
        return "Withholding tax"
    if "stock lending" in name or "syep" in name:
        return "Stock lending interest"
    if "interest" in name:
        return "Debit interest" if amount < 0 else "Credit interest"
    if name in ("other fee", "other fees", "fee", "fees"):
        return "Other fee"
    if name in ("commission adjustment", "commission adjustments"):
        return "Commission adjustment"
    # Type is a broker category, but never echo arbitrary XML into a status file.
    label = (raw or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z &()/._-]{0,63}", label):
        raise FlexValidationError(f"Unsupported Cash Flex transaction type: {label}")
    raise FlexValidationError("Unsupported Cash Flex transaction type; review Cash Transactions types")


def parse_cash_flex(xml, account_id, first, last):
    """Return dated cash transactions from one account's Cash Flex report."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise FlexValidationError("Cash Flex XML is invalid; request a fresh XML report") from None
    statements = [node for node in root.iter() if _tag(node) == "FlexStatement"]
    if len(statements) != 1:
        raise FlexValidationError("Cash Flex query must return exactly one account statement")
    statement = statements[0]
    if (statement.get("accountId") != account_id or
            _day(statement.get("fromDate")) != first or _day(statement.get("toDate")) != last):
        raise FlexValidationError("Cash Flex account or date range mismatch")
    sections = {_tag(node): node for node in statement}
    if "CashTransactions" not in sections:
        raise FlexValidationError("Cash Flex query needs Cash Transactions")

    movements = []
    occurrences = Counter()
    for node in sections["CashTransactions"]:
        if _tag(node) != "CashTransaction":
            continue
        if node.get("accountId", account_id) != account_id:
            raise FlexValidationError("Cash Flex transaction account mismatch")
        code = (node.get("currency") or "").upper()
        if not code:
            raise FlexValidationError("Cash Flex transaction needs a currency")
        amount = _amount(node.get("amount"), "transaction amount", required=True)
        kind = _kind(node.get("type"), amount)
        raw_type = (node.get("type") or "").lower()
        income_type = ("Transaction tax" if "transaction tax" in raw_type else
                       "Bond interest" if "bond" in raw_type and "interest" in raw_type else
                       kind if kind in {"Dividend", "Payment in lieu", "Withholding tax"} else "")
        raw_con_id = node.get("conid") or node.get("conId") or ""
        try:
            con_id = int(raw_con_id) if raw_con_id else None
        except ValueError:
            raise FlexValidationError("Invalid Cash Flex transaction conid") from None
        if con_id is not None and con_id < 0:
            raise FlexValidationError("Invalid Cash Flex transaction conid")
        if con_id == 0:
            con_id = None
        raw_fx = node.get("fxRateToBase")
        fx = _amount(raw_fx, "transaction FX rate") if raw_fx not in (None, "") else None
        if fx is not None and fx < 0:
            raise FlexValidationError("Invalid Cash Flex transaction FX rate")
        if fx == 0:
            fx = None
        reported_day = _day(node.get("reportDate") or node.get("dateTime"))
        if not first <= reported_day <= last:
            raise FlexValidationError("Cash Flex transaction date is outside its statement; review posting date before importing")
        identity = (account_id, code, reported_day, kind, format(amount.normalize(), "f"),
                    node.get("description") or "", node.get("transactionID") or "")
        occurrences[identity] += 1
        key = sha256(json.dumps((*identity, occurrences[identity]),
                                separators=(",", ":")).encode()).hexdigest()
        movements.append({
            "source_key": "cash-flex:" + key, "reported_day": reported_day,
            "kind": kind, "currency": code, "amount": str(amount),
            "description": node.get("description") or kind,
            "con_id": con_id, "symbol": node.get("symbol") or "",
            "transaction_id": node.get("transactionID") or "",
            "fx_to_usd": str(fx) if fx is not None else ("1" if code == "USD" else None),
            "income_type": income_type,
        })

    return movements


def planned_cash_windows(db, portfolio_id, today, account_id=None):
    account_id = account_id or db.setting("ib_account_id")
    latest = db.conn.execute("""SELECT MAX(to_day) FROM broker_sync_windows
        WHERE account_id=? AND source='flex_cash' AND status='complete'""",
        (account_id,)).fetchone()[0]
    through = today - timedelta(days=1)
    first = (date.fromisoformat(latest) + timedelta(days=1)
             if latest else through - timedelta(days=29))
    windows = []
    while first <= through:
        end = min(first + timedelta(days=29), through)
        windows.append((first.isoformat(), end.isoformat()))
        first = end + timedelta(days=1)
    return windows


def cash_import_windows(db, account_id, first, last, *, portfolio_id=1,
                        days=MAX_FLEX_WINDOW_DAYS):
    """Recheck complete imported periods that intersect a selected date range."""
    existing = sorted(set(tuple(row) for row in db.conn.execute("""SELECT period_from,period_to
        FROM cash_movements WHERE portfolio_id=? AND account_id=?
        AND period_to>=? AND period_from<=?
        UNION SELECT from_day,to_day FROM broker_sync_windows
        WHERE account_id=? AND source='flex_cash' AND status='complete'
        AND to_day>=? AND from_day<=?""",
        (portfolio_id, account_id, first, last, account_id, first, last))))
    result = []
    cursor = date.fromisoformat(first)
    through = date.fromisoformat(last)

    def gaps(end):
        nonlocal cursor
        while cursor <= end:
            stop = min(cursor + timedelta(days=days - 1), end)
            result.append((cursor.isoformat(), stop.isoformat()))
            cursor = stop + timedelta(days=1)

    previous_existing = False
    for start, end in existing:
        if result and start <= result[-1][1]:
            raise FlexValidationError("Needs review: existing Cash periods overlap")
        had_gap = cursor < date.fromisoformat(start)
        gaps(date.fromisoformat(start) - timedelta(days=1))
        if (previous_existing and not had_gap and result and
                (date.fromisoformat(end) - date.fromisoformat(result[-1][0])).days < days):
            result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
        previous_existing = True
        cursor = date.fromisoformat(end) + timedelta(days=1)
    gaps(through)
    return result


def _cash_same(db, portfolio_id, account_id, first, last, new_movements):
    complete = db.conn.execute("""SELECT 1 FROM broker_sync_windows
        WHERE account_id=? AND source='flex_cash' AND from_day=? AND to_day=?
        AND status='complete'""", (account_id, first, last)).fetchone()
    if not complete:
        return False
    movements = db.conn.execute("""SELECT * FROM cash_movements WHERE portfolio_id=?
        AND account_id=? AND period_from=? AND period_to=?""",
        (portfolio_id, account_id, first, last)).fetchall()

    def contents(row):
        return (row["reported_day"], row["kind"], row["currency"],
                Decimal(row["amount"]), row["description"], row["con_id"],
                row["symbol"], row["transaction_id"], row["fx_to_usd"], row["income_type"])

    return Counter(map(contents, movements)) == Counter(map(contents, new_movements))


def _validate_cash_replacements(db, portfolio_id, account_id, staged, *,
                                guard_missing_currency=True, consolidate_existing=False):
    """Avoid partial replacement of an existing Cash import period."""
    existing = set(tuple(row) for row in db.conn.execute("""SELECT period_from,period_to
        FROM cash_movements WHERE portfolio_id=? AND account_id=?
        UNION SELECT from_day,to_day FROM broker_sync_windows
        WHERE account_id=? AND source='flex_cash' AND status='complete'""",
        (portfolio_id, account_id, account_id)))
    replaced = set()
    for first, last, movements, _digest in staged:
        for span in existing:
            if span[1] < first or span[0] > last:
                continue
            if span != (first, last) and not (consolidate_existing and
                    first <= span[0] and span[1] <= last):
                raise FlexValidationError("Needs review: Cash report overlaps an existing period")
            replaced.add(span)
        old_currencies = {row[0] for row in db.conn.execute("""SELECT DISTINCT currency
            FROM cash_movements WHERE portfolio_id=? AND account_id=?
            AND period_from>=? AND period_to<=?""", (portfolio_id, account_id, first, last))}
        if guard_missing_currency and old_currencies - {row["currency"] for row in movements}:
            raise FlexValidationError("Needs review: Cash report omits an existing currency; check query filters")
    return replaced


def run_cash_flex_sync(db, account_id, query_id, *, portfolio_id=1, today=None,
                       token=None, fetcher=fetch_flex, pacing_seconds=15, windows_override=None,
                       on_window_start=None, on_window_complete=None,
                       on_window_unavailable=None, request_pacer=None,
                       stop_on_unavailable=False, backup=True, compare_existing=False,
                       consolidate_existing=False,
                       on_window_result=None, on_window_unavailable_detail=None):
    """Fetch and replace completed Cash Flex transaction periods."""
    if not account_id or not query_id:
        raise FlexValidationError("IBKR account and Cash Flex query ID are required")
    db.validate_account_ownership(account_id)
    today = today or flex_today()
    windows = windows_override if windows_override is not None else planned_cash_windows(
        db, portfolio_id, today, account_id)
    if not windows:
        return {"windows": 0, "movements": 0}
    secret = token or load_flex_token(account_id)
    if not secret:
        raise FlexValidationError("IBKR Flex token is missing; renew it in Client Portal")
    count = 0
    with single_writer_lock(db.path, "ibkr-cash-sync.lock"):
        if backup and not compare_existing:
            db.backup()
        pacer = request_pacer or FlexRequestPacer(pacing_seconds)
        unavailable = 0
        unverified = []
        completed = 0
        staged = []
        unchanged = set()
        for first, last in windows:
            if on_window_start:
                on_window_start(first, last)
            if compare_existing and (date.fromisoformat(last) - date.fromisoformat(first)).days >= 365:
                raise FlexValidationError("Needs review: existing Cash period exceeds the Flex date override limit")
            try:
                xml = pacer.fetch(fetcher, query_id, secret, first, last)
            except FlexServiceError as exc:
                if exc.code != 1003 or on_window_unavailable is None:
                    raise
                unavailable += 1
                if on_window_unavailable_detail is not None:
                    on_window_unavailable_detail(first, last, exc.code, exc.stage)
                on_window_unavailable(first, last)
                if stop_on_unavailable:
                    if compare_existing:
                        # A connected correction group is only valid as a whole.
                        # Its fetched prefix must remain unverified if a member is unavailable.
                        staged.clear()
                        unchanged.clear()
                        unverified = [span for span in windows if tuple(span) != (first, last)]
                    break
                continue
            movements = parse_cash_flex(xml, account_id, first, last)
            digest = sha256(xml).hexdigest()
            if compare_existing:
                staged.append((first, last, movements, digest))
                if _cash_same(db, portfolio_id, account_id, first, last, movements):
                    unchanged.add((first, last))
                continue
            staged = [(first, last, movements, digest)]
            _apply_cash_periods(db, portfolio_id, account_id, staged, guard_missing_currency=False)
            count += len(movements)
            completed += 1
            if on_window_complete:
                on_window_complete(first, last)
        if compare_existing:
            _validate_cash_replacements(db, portfolio_id, account_id, staged,
                                        consolidate_existing=consolidate_existing)
            changed = [item for item in staged if item[:2] not in unchanged]
            if changed:
                if backup:
                    db.backup()
                _apply_cash_periods(db, portfolio_id, account_id, changed,
                                    consolidate_existing=consolidate_existing)
            for first, last, movements, _digest in staged:
                count += len(movements)
                if on_window_result:
                    on_window_result(first, last, "unchanged" if (first, last) in unchanged else "updated")
                if on_window_complete:
                    on_window_complete(first, last)
        if not compare_existing or any(item[:2] not in unchanged for item in staged):
            db.set_metadata("ibkr_cash_sync_completed", datetime.now().isoformat(timespec="seconds"))
    result = {"windows": len(staged) if compare_existing else completed, "movements": count}
    if compare_existing:
        result.update(unchanged_windows=len(unchanged), updated_windows=len(staged) - len(unchanged))
    if unavailable:
        result["unavailable_windows"] = unavailable
    if unverified:
        result["unverified_windows"] = unverified
    return result


def _apply_cash_periods(db, portfolio_id, account_id, staged, *, guard_missing_currency=True,
                        consolidate_existing=False):
    replaced = _validate_cash_replacements(db, portfolio_id, account_id, staged,
        guard_missing_currency=guard_missing_currency, consolidate_existing=consolidate_existing)
    with db.conn:
        # Replace each complete period atomically, including periods with no transactions.
        for first, last in replaced:
            db.conn.execute("""DELETE FROM cash_movements WHERE portfolio_id=?
                AND account_id=? AND period_from=? AND period_to=?""",
                (portfolio_id, account_id, first, last))
            db.conn.execute("""DELETE FROM broker_sync_windows WHERE account_id=?
                AND source='flex_cash' AND from_day=? AND to_day=?""",
                (account_id, first, last))
        for first, last, movements, digest in staged:
            for row in movements:
                db.conn.execute("""INSERT INTO cash_movements
                    (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
                     kind,currency,amount,description,source_section,source_hash,
                     con_id,symbol,transaction_id,fx_to_usd,income_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(portfolio_id,account_id,source_key) DO UPDATE SET
                    reported_day=excluded.reported_day,period_from=excluded.period_from,
                    period_to=excluded.period_to,kind=excluded.kind,currency=excluded.currency,
                    amount=excluded.amount,description=excluded.description,
                    source_hash=excluded.source_hash,con_id=excluded.con_id,
                    symbol=excluded.symbol,transaction_id=excluded.transaction_id,
                    fx_to_usd=excluded.fx_to_usd,income_type=excluded.income_type""",
                    (portfolio_id, account_id, row["source_key"], row["reported_day"],
                     first, last, row["kind"], row["currency"], row["amount"],
                     row["description"], "Cash Transactions", digest,
                     row["con_id"], row["symbol"], row["transaction_id"],
                     row["fx_to_usd"], row["income_type"]))
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status,source_count,imported_count)
                VALUES (?,'flex_cash',?,?,'complete',?,?)
                ON CONFLICT(account_id,source,from_day,to_day) DO UPDATE SET
                status='complete',source_count=excluded.source_count,
                imported_count=excluded.imported_count,completed_at=CURRENT_TIMESTAMP""",
                (account_id, first, last, len(movements), len(movements)))
