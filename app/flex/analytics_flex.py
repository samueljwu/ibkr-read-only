"""Catch up completed account valuations and external flows through IBKR Flex."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import time
import xml.etree.ElementTree as ET

from app.flex.flex_import import (FlexServiceError, FlexValidationError, flex_day, flex_today,
                                  require_usd_base, fetch_flex, resolve_flex_token)
from app.sync.sync_service import single_writer_lock


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _day(value):
    return flex_day(value)


def _amount(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise FlexValidationError(f"Invalid Flex {label}") from None
    if not number.is_finite():
        raise FlexValidationError(f"Invalid Flex {label}")
    return number


def parse_analytics_flex(xml, account_id, first, last):
    """Return dated USD NAV and external-flow records from one account report."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise FlexValidationError("Analytics Flex XML is invalid; request a fresh XML report") from None
    statements = [node for node in root.iter() if _tag(node) == "FlexStatement"]
    if not statements:
        raise FlexValidationError("Analytics Flex report contains no statements")
    valuations = {}
    nav_totals = {}
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
            row = repr((day, format(amount.normalize(), "f"), kind)).encode("utf-8")
            fingerprint = sha256(row).hexdigest()[:24]
            cash_occurrences[fingerprint] += 1
            key = f"row:{fingerprint}:{cash_occurrences[fingerprint]}"
        else:
            raise FlexValidationError("Analytics Flex query needs Transaction ID for asset transfers")
        source_id = f"flex-analytics:{account_id}:{kind}:{key}"
        if source_id in seen:
            raise FlexValidationError("Duplicate external-flow transaction in Flex report")
        seen.add(source_id)
        flows.append((source_id, day, float(amount), kind))

    cash_by_day = defaultdict(list)
    for statement in statements:
        start, end = _day(statement.get("fromDate")), _day(statement.get("toDate"))
        if statement.get("accountId") != account_id or not first <= start <= end <= last:
            raise FlexValidationError("Analytics Flex account or date range mismatch")
        sections = {_tag(child): child for child in statement}
        if not {"EquitySummaryInBase", "CashTransactions", "Transfers"}.issubset(sections):
            raise FlexValidationError("Analytics Flex query needs NAV Summary in Base, Cash Transactions, and Transfers sections")
        require_usd_base(statement, account_id)
        # Omitted child accounts inherit the already validated statement account.
        # An explicit conflicting or blank account must never be accepted.
        for section in sections.values():
            for node in section.iter():
                if node.get("accountId", account_id) != account_id:
                    raise FlexValidationError("Analytics Flex child account mismatch")
        context = {}
        for node in sections["EquitySummaryInBase"]:
            if _tag(node) != "EquitySummaryByReportDateInBase":
                continue
            day = _day(node.get("reportDate"))
            if node.get("currency", "USD").upper() != "USD":
                raise FlexValidationError("Analytics Flex NAV must use USD base currency")
            if day > end:
                raise FlexValidationError("Analytics Flex report does not cover the requested dates; NAV date is outside its statement")
            nav = _amount(node.get("total"), "NAV")
            if nav < 0:
                raise FlexValidationError("Flex NAV cannot be negative")
            # Daily reports carry a previous NAV row, including Friday on Monday.
            if day < start:
                if day in context:
                    raise FlexValidationError("Duplicate Flex NAV context date")
                context[day] = nav
                continue
            if day in valuations:
                raise FlexValidationError("Duplicate Flex NAV date")
            nav_totals[day] = nav
            cash = node.get("cash")
            valuations[day] = (float(nav), float(_amount(cash, "cash")) if cash not in (None, "") else None)
        intervals.append((start, end, context))
        for node in sections["CashTransactions"]:
            if _tag(node) != "CashTransaction" or (node.get("type") or "").lower() not in {
                "deposits/withdrawals", "deposits and withdrawals", "deposit", "withdrawal"
            }:
                continue
            day = _day(node.get("reportDate") or node.get("dateTime"))
            if not start <= day <= end:
                raise FlexValidationError("Analytics Flex cash flow date is outside its statement")
            currency = (node.get("currency") or "").upper()
            if not currency:
                raise FlexValidationError("Analytics Flex cash flow needs Currency")
            rate = _amount(node.get("fxRateToBase") or ("1" if currency == "USD" else None), "FX rate")
            if rate <= 0:
                raise FlexValidationError("Flex external-flow FX rate must be positive")
            amount = _amount(node.get("amount"), "cash flow") * rate
            add_flow("cash", node, day, amount)
            cash_by_day[day].append(amount)
        for node in sections["Transfers"]:
            if _tag(node) != "Transfer":
                continue
            day = _day(node.get("reportDate") or node.get("date"))
            if not start <= day <= end:
                raise FlexValidationError("Analytics Flex transfer date is outside its statement")
            direction = (node.get("direction") or "").upper()
            if direction not in ("IN", "OUT"):
                raise FlexValidationError("Unknown Flex transfer direction")
            if node.get("assetCategory") == "CASH":
                currency = (node.get("currency") or "").upper()
                if not currency:
                    raise FlexValidationError("Analytics Flex cash transfer needs Currency")
                rate = _amount(node.get("fxRateToBase") or ("1" if currency == "USD" else None), "FX rate")
                if rate <= 0:
                    raise FlexValidationError("Flex transfer FX rate must be positive")
                amount = abs(_amount(node.get("cashTransfer"), "cash transfer") * rate)
                amount = amount if direction == "IN" else -amount
                if amount in cash_by_day[day]:
                    cash_by_day[day].remove(amount)
                    continue
            else:
                amount = abs(_amount(node.get("positionAmountInBase"), "asset transfer"))
                amount = amount if direction == "IN" else -amount
            add_flow("transfer", node, day, amount)

    def weekend_only(start, end):
        day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        while day <= end_day:
            if day.weekday() < 5:
                return False
            day += timedelta(days=1)
        return True

    # An aggregate statement's date bounds do not prove daily NAV coverage.
    # Keep wholly empty early reports compatible, but a report containing NAV
    # must include every weekday inside its declared intervals.
    has_nav = bool(nav_totals) or any(context for _, _, context in intervals)
    covered_through = None
    for start, end, context in sorted(intervals, key=lambda item: item[:2]):
        if has_nav:
            day = date.fromisoformat(start)
            end_day = date.fromisoformat(end)
            while day <= end_day:
                if day.weekday() < 5 and day.isoformat() not in nav_totals:
                    raise FlexValidationError("Analytics Flex report does not cover the requested dates; include every weekday NAV date")
                day += timedelta(days=1)
        missing_start = first if covered_through is None else (
            date.fromisoformat(covered_through) + timedelta(days=1)).isoformat()
        if start > missing_start:
            missing_end = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            # Only observed daily NAV chains establish a skipped weekend.
            prior = max(context, default=None)
            linked = (start == end and start in valuations and prior is not None
                      and date.fromisoformat(prior).weekday() == 4
                      and prior < missing_start
                      and weekend_only(missing_start, missing_end))
            if covered_through is not None:
                linked = (linked and prior == covered_through and prior in valuations
                          and context[prior] == nav_totals[prior])
            if not linked:
                raise FlexValidationError("Analytics Flex report does not cover the requested dates; check daily NAV date gaps")
        covered_through = max(covered_through or end, end)
    if covered_through != last:
        trailing = (date.fromisoformat(covered_through) + timedelta(days=1)).isoformat()
        if (covered_through not in valuations
                or date.fromisoformat(covered_through).weekday() != 4
                or not weekend_only(trailing, last)):
            raise FlexValidationError("Analytics Flex report does not cover the requested dates")
    return valuations, flows


def planned_analytics_windows(db, account_id, today):
    through = today - timedelta(days=1)
    last = db.conn.execute("""SELECT MAX(to_day) FROM broker_sync_windows
        WHERE account_id=? AND source='flex_analytics' AND status='complete'""", (account_id,)).fetchone()[0]
    first = max(date(today.year - 4, 1, 1),
                date.fromisoformat(last) - timedelta(days=7) if last else date.min)
    windows = []
    while first <= through:
        end = min(first + timedelta(days=179), through)
        windows.append((first.isoformat(), end.isoformat()))
        first = end + timedelta(days=1)
    return windows


def run_analytics_flex_sync(db, account_id, query_id, *, portfolio_id=1, today=None, token=None,
                            fetcher=fetch_flex, pacing_seconds=15, windows_override=None,
                            on_window_start=None, on_window_complete=None,
                            on_window_unavailable=None, compare_existing=False,
                            on_window_result=None, on_window_unavailable_detail=None):
    """Replay a dated Flex report without requiring a TWS/Gateway session."""
    if not account_id or not query_id:
        raise FlexValidationError("IBKR account and Analytics Flex query ID are required")
    db.validate_account_ownership(account_id)
    today = today or flex_today()
    secret = resolve_flex_token(account_id, token)
    with single_writer_lock(db.path, "ibkr-analytics-sync.lock"):
        windows = windows_override if windows_override is not None else planned_analytics_windows(db, account_id, today)
        if windows and not compare_existing:
            db.backup()
        count = 0
        unchanged = updated = 0
        last_request = 0.0
        for first, last in windows:
            if on_window_start:
                on_window_start(first, last)
            xml = None
            for attempt in range(3):
                delay = pacing_seconds - (time.monotonic() - last_request)
                if last_request and delay > 0:
                    time.sleep(delay)
                last_request = time.monotonic()
                try:
                    xml = fetcher(query_id, secret, from_day=first, to_day=last)
                    break
                except FlexServiceError as exc:
                    if exc.code != 1003:
                        raise
                    if attempt < 2:
                        continue
                    if on_window_unavailable is None:
                        raise
                    if on_window_unavailable_detail is not None:
                        on_window_unavailable_detail(first, last, exc.code, exc.stage)
                    on_window_unavailable(first, last)
                    break
            if xml is None:
                continue
            valuations, flows = parse_analytics_flex(xml, account_id, first, last)
            old_nav = db.conn.execute("""SELECT day,nav_usd,cash_usd,daily_pnl_usd,source,flow_covered
                FROM account_history WHERE portfolio_id=? AND day BETWEEN ? AND ?""",
                (portfolio_id, first, last)).fetchall()
            old_flows = db.conn.execute("""SELECT day,amount_usd,kind FROM external_flows
                WHERE portfolio_id=? AND (source_id LIKE ? OR source_id LIKE 'history-csv:%')
                AND day BETWEEN ? AND ?""",
                (portfolio_id, f"flex-analytics:{account_id}:%", first, last)).fetchall()
            if (not valuations and (old_nav or old_flows) or compare_existing and
                    any(row["source"] == "flex" and row["day"] not in valuations for row in old_nav)):
                raise FlexValidationError("Needs review: Analytics Flex report does not cover the requested dates; empty or incomplete NAV cannot replace existing history")
            expected_nav = {(day, nav, cash, None, "flex", 1)
                            for day, (nav, cash) in valuations.items()}
            has_csv = db.conn.execute("""SELECT 1 FROM external_flows WHERE portfolio_id=?
                AND source_id LIKE 'history-csv:%' AND day BETWEEN ? AND ? LIMIT 1""",
                (portfolio_id, first, last)).fetchone()
            same = (not has_csv and set(map(tuple, old_nav)) == expected_nav and
                    Counter(map(tuple, old_flows)) == Counter((day, amount, kind)
                        for _source_id, day, amount, kind in flows))
            if compare_existing and same:
                unchanged += 1
                count += len(valuations)
                if on_window_result:
                    on_window_result(first, last, "unchanged")
                if on_window_complete:
                    on_window_complete(first, last)
                continue
            if compare_existing and not updated:
                db.backup()
            with db.conn:
                if not valuations and db.conn.execute("""SELECT 1 FROM account_history
                    WHERE portfolio_id=? AND day BETWEEN ? AND ? LIMIT 1""",
                    (portfolio_id, first, last)).fetchone():
                    raise FlexValidationError("Analytics Flex report does not cover the requested dates; empty NAV cannot replace existing history")
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
            updated += 1
            if on_window_result:
                on_window_result(first, last, "updated")
            if on_window_complete:
                on_window_complete(first, last)
        if not compare_existing or updated:
            db.set_metadata("ibkr_analytics_sync_completed", datetime.now().isoformat(timespec="seconds"))
        result = {"windows": len(windows), "valuations": count}
        if compare_existing:
            result.update(unchanged_windows=unchanged, updated_windows=updated)
        return result
