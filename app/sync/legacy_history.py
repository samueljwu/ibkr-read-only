"""Validated, monthly pre-Flex history from IBKR CSV exports.

PortfolioAnalyst supplies monthly observations. Activity Statement rows are
kept as dated evidence, never promoted to execution-level or daily coverage.
"""

from __future__ import annotations

import calendar
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path
import json
import re

from app.sync.sync_service import single_writer_lock


MAX_CSV_BYTES = 30_000_000
MARKET_BASELINE_SOURCE = "CSV market baseline"
EVIDENCE_SECTIONS = {
    "Trades": "Date/Time",
    "Transfers": "Date",
    "Deposits & Withdrawals": "Settle Date",
    "Fees": "Date",
    "Dividends": "Date",
    "Withholding Tax": "Date",
    "Interest": "Date",
    "Open Positions": None,
    "Financial Instrument Information": None,
}


class LegacyHistoryError(ValueError):
    """A safe, user-facing validation or reconciliation failure."""


def _read_csv(path):
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_CSV_BYTES:
        raise LegacyHistoryError("Choose an existing IBKR CSV under 30 MB.")
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        rows = list(csv.reader(StringIO(text, newline="")))
    except (UnicodeError, csv.Error) as exc:
        raise LegacyHistoryError("The selected report is not a readable UTF-8 CSV.") from exc
    return rows, sha256(raw).hexdigest()


def _number(value, label, *, allow_blank=False):
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text and allow_blank:
        return Decimal(0)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise LegacyHistoryError(f"{label} contains a nonnumeric value.") from exc
    if not number.is_finite():
        raise LegacyHistoryError(f"{label} contains a nonfinite value.")
    return number


def _field(rows, section, name):
    values = [row[3] for row in rows if len(row) > 3 and row[:3] == [section, "Data", name]]
    if len(values) != 1:
        raise LegacyHistoryError(f"The Activity Statement needs one {section}/{name} field.")
    return values[0]


def _section_rows(rows, section):
    """Yield data under its most recent header; reject ambiguous columns."""
    header = None
    for row in rows:
        if len(row) < 2 or row[0] != section:
            continue
        if row[1] == "Header":
            header = row[2:]
        elif row[1] == "Data":
            if header is None or len(row[2:]) != len(header):
                raise LegacyHistoryError(f"{section} has an unsupported CSV layout.")
            yield dict(zip(header, row[2:]))


def _month_end(month):
    year, number = int(month[:4]), int(month[4:])
    return date(year, number, calendar.monthrange(year, number)[1])


def _parse_portfolio_analyst(rows, account, first, last):
    introduction = list(_section_rows(rows, "Introduction"))
    if len(introduction) != 1 or introduction[0].get("Account") != account:
        raise LegacyHistoryError("PortfolioAnalyst account does not match the selected account.")
    if introduction[0].get("BaseCurrency") != "USD":
        raise LegacyHistoryError("PortfolioAnalyst must report a USD base account.")
    nav_by_month = {}
    cash_by_month = {}
    for item in _section_rows(rows, "Allocation by Asset Class"):
        month = item.get("Date", "")
        if len(month) != 6 or not month.isdigit():
            raise LegacyHistoryError("PortfolioAnalyst monthly NAV has an invalid month.")
        try:
            period_end = _month_end(month)
        except ValueError as exc:
            raise LegacyHistoryError("PortfolioAnalyst monthly NAV has an invalid month.") from exc
        if month in nav_by_month:
            raise LegacyHistoryError("PortfolioAnalyst has duplicate monthly NAV rows.")
        nav = _number(item.get("NAV", ""), "Monthly NAV")
        if nav < 0:
            raise LegacyHistoryError("Monthly NAV cannot be negative.")
        nav_by_month[month] = nav
        cash_by_month[month] = _number(item.get("Cash", ""), "Monthly cash allocation")
    if not nav_by_month:
        raise LegacyHistoryError("PortfolioAnalyst has no monthly NAV rows.")
    returns = {}
    for item in _section_rows(rows, "Time Period Benchmark Comparison"):
        label = item.get("Date", "")
        try:
            month = datetime.strptime(label, "%b %Y").strftime("%Y%m")
        except ValueError as exc:
            raise LegacyHistoryError("PortfolioAnalyst monthly return has an invalid month.") from exc
        if month in returns:
            raise LegacyHistoryError("PortfolioAnalyst has duplicate monthly return rows.")
        if account not in item or account + "Return" not in item:
            raise LegacyHistoryError("PortfolioAnalyst monthly return lacks the selected account.")
        returns[month] = _number(item[account + "Return"], "Monthly return")
    selected = []
    cursor = date(first.year, first.month, 1)
    while cursor <= last:
        month = cursor.strftime("%Y%m")
        if month not in nav_by_month or month not in returns:
            raise LegacyHistoryError("PortfolioAnalyst lacks a month within the statement period.")
        period_end = _month_end(month)
        selected.append((month[:4] + "-" + month[4:],
                         max(first, cursor).isoformat(), min(last, period_end).isoformat(),
                         str(nav_by_month[month]), str(returns[month])))
        cursor = period_end + date.resolution
    return selected, cash_by_month


def _parse_account_performance(rows, first, last):
    introduction = list(_section_rows(rows, "Introduction"))[0]
    period = introduction.get("AnalysisPeriod", "")
    match = re.fullmatch(r"(.+?) to (.+?) \(Monthly\)", period)
    if not match:
        raise LegacyHistoryError("PortfolioAnalyst needs a since-inception monthly period.")
    try:
        began, ended = (datetime.strptime(value, "%B %d, %Y").date()
                        for value in match.groups())
    except ValueError as exc:
        raise LegacyHistoryError("PortfolioAnalyst has an invalid analysis period.") from exc
    if began != first or ended < last or ended > date.today():
        raise LegacyHistoryError("PortfolioAnalyst period must start at account inception and include the statement.")
    statistics = list(_section_rows(rows, "Key Statistics"))
    if len(statistics) != 1:
        raise LegacyHistoryError("PortfolioAnalyst needs one Key Statistics row.")
    reported = _number(statistics[0].get("CumulativeReturn", ""), "Since-inception return")
    measure = introduction.get("PerformanceMeasure", "").strip().upper()
    if measure not in {"TWR", "MWR"}:
        raise LegacyHistoryError("PortfolioAnalyst has an unsupported performance measure.")
    ending_nav = _number(statistics[0].get("EndingNAV", ""), "PortfolioAnalyst ending NAV")
    final_month = ended.strftime("%Y%m")
    monthly_nav = [_number(item.get("NAV", ""), "Final PortfolioAnalyst monthly NAV")
                   for item in _section_rows(rows, "Allocation by Asset Class")
                   if item.get("Date") == final_month]
    if len(monthly_nav) != 1:
        raise LegacyHistoryError("PortfolioAnalyst lacks NAV for its report end month.")
    if abs(ending_nav - monthly_nav[0]) > Decimal("0.01"):
        raise LegacyHistoryError("PortfolioAnalyst ending NAV does not match its monthly NAV.")
    return began.isoformat(), ended.isoformat(), str(reported), str(ending_nav), measure


def _parse_opening_positions(rows):
    """Use reconciled closing shares and an optional reported closing mark."""
    conids = {}
    for item in _section_rows(rows, "Financial Instrument Information"):
        if item.get("Asset Category") != "Stocks":
            continue
        symbol = item.get("Symbol", "").strip().upper()
        raw_id = item.get("Conid", "").strip()
        if not symbol or not raw_id.isdigit() or int(raw_id) <= 0:
            continue
        conids.setdefault(symbol, set()).add(int(raw_id))
    ledger = {}
    for item in _section_rows(rows, "Transfers"):
        if item.get("Asset Category") != "Stocks":
            continue
        direction = item.get("Direction", "")
        if direction not in {"In", "Out"}:
            raise LegacyHistoryError("Statement stock transfer direction needs review.")
        key = (item.get("Symbol", "").strip().upper(), item.get("Currency", "").strip().upper())
        quantity = abs(_number(item.get("Qty", ""), "Stock transfer quantity"))
        ledger[key] = ledger.get(key, Decimal(0)) + (quantity if direction == "In" else -quantity)
    for item in _section_rows(rows, "Trades"):
        if item.get("Asset Category") != "Stocks":
            continue
        key = (item.get("Symbol", "").strip().upper(), item.get("Currency", "").strip().upper())
        ledger[key] = ledger.get(key, Decimal(0)) + _number(item.get("Quantity", ""), "Stock trade quantity")
    openings = {}
    for item in _section_rows(rows, "Open Positions"):
        if item.get("DataDiscriminator") != "Summary":
            continue
        if item.get("Asset Category") != "Stocks":
            raise LegacyHistoryError("Statement opening quantities for this asset category need review.")
        key = (item.get("Symbol", "").strip().upper(), item.get("Currency", "").strip().upper())
        if not all(key) or key in openings:
            raise LegacyHistoryError("Statement opening position identity needs review.")
        quantity = _number(item.get("Quantity", ""), "Closing position quantity")
        raw_close = item.get("Close Price", "").strip()
        close = (_number(raw_close, "Closing market price")
                 if raw_close and raw_close != "--" else None)
        if close is not None and close <= 0:
            raise LegacyHistoryError("Statement closing market price must be positive.")
        openings[key] = (quantity, close)
    if any(abs(ledger.get(key, Decimal(0)) - openings.get(key, (Decimal(0), None))[0]) > Decimal("0.00000001")
           for key in set(ledger) | set(openings)):
        raise LegacyHistoryError("Statement stock activity does not reconcile to closing positions.")
    return [(symbol, currency, str(quantity), str(close) if close is not None else None,
             next(iter(conids[symbol])) if len(conids.get(symbol, ())) == 1 else None)
            for (symbol, currency), (quantity, close) in openings.items()]


def _parse_activity_statement(rows, account):
    if _field(rows, "Account Information", "Account") != account:
        raise LegacyHistoryError("Activity Statement account does not match the selected account.")
    if _field(rows, "Account Information", "Base Currency") != "USD":
        raise LegacyHistoryError("Activity Statement must report a USD base account.")
    period = _field(rows, "Statement", "Period")
    try:
        start_text, end_text = period.split(" - ", 1)
        first = datetime.strptime(start_text, "%B %d, %Y").date()
        last = datetime.strptime(end_text, "%B %d, %Y").date()
    except ValueError as exc:
        raise LegacyHistoryError("Activity Statement has an invalid period.") from exc
    if first > last or last >= date(2022, 1, 1) or last != _month_end(last.strftime("%Y%m")):
        raise LegacyHistoryError("Choose a pre-2022 statement ending at month end.")
    nav_parts = {}
    for item in _section_rows(rows, "Net Asset Value"):
        if "Asset Class" in item:
            key = item["Asset Class"].strip()
            if key in nav_parts:
                raise LegacyHistoryError("Activity Statement has duplicate NAV components.")
            nav_parts[key] = _number(item.get("Current Total", ""), "Statement NAV", allow_blank=True)
    if not {"Total", "Cash", "Dividend Accruals"} <= nav_parts.keys():
        raise LegacyHistoryError("Activity Statement lacks the required NAV components.")
    if sum((value for key, value in nav_parts.items() if key != "Total"), Decimal(0)) != nav_parts["Total"]:
        raise LegacyHistoryError("Activity Statement NAV components do not reconcile.")
    bridge = {item["Field Name"]: _number(item.get("Field Value", ""), "NAV change", allow_blank=True)
              for item in _section_rows(rows, "Change in NAV")}
    if "Starting Value" not in bridge or "Ending Value" not in bridge:
        raise LegacyHistoryError("Activity Statement lacks the NAV change bridge.")
    movement = sum((value for key, value in bridge.items()
                    if key not in {"Starting Value", "Ending Value"}), Decimal(0))
    if abs(bridge["Starting Value"] + movement - bridge["Ending Value"]) > Decimal("0.01") or abs(
            bridge["Ending Value"] - nav_parts["Total"]) > Decimal("0.01"):
        raise LegacyHistoryError("Activity Statement NAV change does not reconcile.")
    evidence = []
    for section, date_field in EVIDENCE_SECTIONS.items():
        for item in _section_rows(rows, section):
            if section in {"Open Positions", "Financial Instrument Information"}:
                if section == "Open Positions" and item.get("DataDiscriminator") not in {"Summary", "Lot"}:
                    continue
                day = last
            else:
                raw_day = item.get(date_field, "")[:10]
                try:
                    day = date.fromisoformat(raw_day)
                except ValueError:
                    continue  # Section totals have no transaction date.
                # The opening statement can show a deposit settlement date just
                # before its account period begins. Keep its reported date.
                if not (first <= day <= last or section == "Deposits & Withdrawals"
                        and first - timedelta(days=7) <= day < first):
                    raise LegacyHistoryError(f"{section} contains a date outside the statement period.")
            evidence.append((section, day.isoformat(), json.dumps(item, sort_keys=True)))
    if not evidence:
        raise LegacyHistoryError("Activity Statement has no dated activity or positions.")
    return first, last, nav_parts, evidence


def _report_kind(rows):
    sections = {row[0] for row in rows if len(row) > 1 and row[1] in {"Header", "Data"}}
    portfolio = {"Introduction", "Allocation by Asset Class"} <= sections
    statement = {"Statement", "Account Information"} <= sections
    return "portfolio" if portfolio and not statement else "statement" if statement and not portfolio else None


def _baseline_gap(db, portfolio_id, position_id, day, quantity, close_price, currency):
    if close_price is None or Decimal(quantity) <= 0:
        return None
    later = db.conn.execute("""SELECT 1 FROM legacy_position_openings
        WHERE portfolio_id=? AND position_id=? AND as_of>? LIMIT 1""",
        (portfolio_id, position_id, day)).fetchone()
    if later:
        return None
    trade_id = f"CSV-MARK:{portfolio_id}:{position_id}:{day}"
    if db.conn.execute("SELECT 1 FROM trades WHERE trade_id=?", (trade_id,)).fetchone():
        return None
    prior = db.conn.execute("""SELECT side,quantity FROM trades
        WHERE portfolio_id=? AND position_id=? AND trade_date<=? AND source<>?""",
        (portfolio_id, position_id, day, MARKET_BASELINE_SOURCE)).fetchall()
    if any(row["side"] == "SELL" for row in prior):
        return None  # Earlier sales need a lot-level review.
    missing = Decimal(quantity) - sum((Decimal(str(row["quantity"])) for row in prior), Decimal(0))
    if missing <= 0:
        return None
    fx = 1.0 if currency == "USD" else db.fx_rate(currency, day)
    if not fx or fx <= 0:
        return None
    return missing, fx


def _baseline_lot(db, portfolio_id, position_id, day, quantity, close_price, currency):
    """Add a priced proxy for the unrecorded part of a CSV closing stock holding."""
    gap = _baseline_gap(db, portfolio_id, position_id, day, quantity, close_price, currency)
    if gap is None:
        return False
    missing, fx = gap
    trade_id = f"CSV-MARK:{portfolio_id}:{position_id}:{day}"
    position = db.conn.execute("SELECT display_ticker,ib_symbol FROM positions WHERE id=?",
                               (position_id,)).fetchone()
    db.conn.execute("""INSERT INTO trades
        (portfolio_id,position_id,trade_id,trade_date,display_ticker,ib_symbol,
         side,quantity,price,security_type,multiplier,currency,fx_to_usd,
         commission_usd,source,notes)
        VALUES (?,?,?,?,?,?,'BUY',?,?,'STK',1,?,?,0,?,?)""",
        (portfolio_id, position_id, trade_id, day, position["display_ticker"],
         position["ib_symbol"], float(missing), float(close_price), currency, fx,
         MARKET_BASELINE_SOURCE,
         "Estimated opening value from the Activity Statement closing market price; not an acquisition cost."))
    return True


def import_legacy_history(db, account, first_path, second_path, *, portfolio_id=1):
    """Identify and validate both reports, then save historical evidence atomically."""
    if not account or db.setting("ib_account_id") != account:
        raise LegacyHistoryError("The selected account does not match the saved IBKR account.")
    first_report = _read_csv(first_path)
    second_report = _read_csv(second_path)
    reports = {_report_kind(rows): (rows, digest) for rows, digest in (first_report, second_report)}
    if set(reports) != {"portfolio", "statement"}:
        raise LegacyHistoryError(
            "Choose one PortfolioAnalyst Detailed CSV and one Activity Statement CSV.")
    pa_rows, pa_hash = reports["portfolio"]
    statement_rows, statement_hash = reports["statement"]
    first, last, nav, evidence = _parse_activity_statement(statement_rows, account)
    months, pa_cash = _parse_portfolio_analyst(pa_rows, account, first, last)
    final_month = last.strftime("%Y%m")
    final_nav = _number(months[-1][3], "Final monthly NAV")
    if abs(final_nav - nav["Total"]) > Decimal("0.01"):
        raise LegacyHistoryError("Month-end NAV differs between the two reports.")
    if abs(pa_cash[final_month] - nav["Cash"] - nav["Dividend Accruals"]) > Decimal("0.01"):
        raise LegacyHistoryError("The reports' month-end cash allocation cannot be reconciled.")
    performance = _parse_account_performance(pa_rows, first, last)
    openings = _parse_opening_positions(statement_rows)
    with single_writer_lock(db.path, "ibkr-legacy-history.lock"):
        existing = {row["month"]: row for row in db.conn.execute(
            """SELECT * FROM account_monthly_history WHERE portfolio_id=? AND account_id=?
               AND month BETWEEN ? AND ?""",
            (portfolio_id, account, months[0][0], months[-1][0]))}
        new_months = []
        for month, period_from, period_to, nav_usd, return_pct in months:
            old = existing.get(month)
            if old:
                if (old["period_from"], old["period_to"], old["nav_usd"], old["return_pct"]) != (
                        period_from, period_to, nav_usd, return_pct):
                    raise LegacyHistoryError("Saved monthly history conflicts with the selected report.")
            else:
                new_months.append((portfolio_id, account, month, period_from, period_to,
                                   nav_usd, return_pct, pa_hash))
        saved = db.conn.execute(
            """SELECT source_hash FROM legacy_statement_sources WHERE portfolio_id=?
               AND account_id=? AND period_from=? AND period_to=?""",
            (portfolio_id, account, first.isoformat(), last.isoformat())).fetchone()
        if saved and saved["source_hash"] != statement_hash:
            raise LegacyHistoryError("Saved Activity Statement differs from the selected file; review it before replacing history.")
        new_statement = saved is None
        saved_performance = db.conn.execute(
            """SELECT period_from,period_to,cumulative_return_pct,ending_nav_usd,measure
               FROM legacy_account_performance WHERE portfolio_id=? AND account_id=? AND period_to=?""",
            (portfolio_id, account, performance[1])).fetchone()
        if saved_performance and tuple(saved_performance) != performance:
            raise LegacyHistoryError("Saved since-inception performance conflicts with the selected report.")
        new_performance = saved_performance is None
        positions = db.rows("positions", portfolio_id)
        planned_openings = []
        mark_updates = []
        baseline_inputs = []
        contract_updates = []
        identity_moves = []
        identity_assignments = []
        for symbol, currency, quantity, close_price, con_id in openings:
            old = db.conn.execute(
                """SELECT position_id,quantity,close_price,con_id FROM legacy_position_openings
                   WHERE portfolio_id=? AND account_id=? AND as_of=? AND symbol=? AND currency=?""",
                (portfolio_id, account, last.isoformat(), symbol, currency)).fetchone()
            if old:
                if Decimal(old["quantity"]) != Decimal(quantity):
                    raise LegacyHistoryError("Saved opening quantity conflicts with the selected statement.")
                if old["close_price"] and old["close_price"] != close_price:
                    raise LegacyHistoryError("Saved closing market price conflicts with the selected statement.")
                if old["con_id"] and con_id and old["con_id"] != con_id:
                    raise LegacyHistoryError("Saved opening contract differs from the selected statement.")
                if close_price and not old["close_price"]:
                    mark_updates.append((close_price, portfolio_id, account,
                                         last.isoformat(), symbol, currency))
                if con_id and not old["con_id"]:
                    contract_updates.append((con_id, portfolio_id, account,
                                             last.isoformat(), symbol, currency))
                position_id = old["position_id"]
                if con_id:
                    old_position = next((p for p in positions if p["id"] == position_id), None)
                    if old_position is None or old_position["con_id"] not in (None, con_id):
                        raise LegacyHistoryError("Saved opening position conflicts with the statement contract.")
                    all_matches = [p for p in positions if p["con_id"] == con_id]
                    if any(p["security_type"] != "STK" or p["currency"].upper() != currency
                           for p in all_matches):
                        raise LegacyHistoryError("Statement contract conflicts with a saved instrument.")
                    matches = [p for p in all_matches if
                               p["security_type"] == "STK" and p["currency"].upper() == currency]
                    if len(matches) > 1:
                        raise LegacyHistoryError("Statement contract matches multiple positions.")
                    if matches and matches[0]["id"] != position_id:
                        non_estimated = db.conn.execute("""SELECT 1 FROM trades
                            WHERE position_id=? AND source<>? LIMIT 1""",
                            (position_id, MARKET_BASELINE_SOURCE)).fetchone()
                        manual = db.conn.execute("""SELECT 1 FROM trade_matches m
                            JOIN trades t ON t.id=m.buy_trade_id OR t.id=m.sell_trade_id
                            WHERE t.position_id=? AND m.origin='manual' LIMIT 1""",
                            (position_id,)).fetchone()
                        if non_estimated or manual:
                            raise LegacyHistoryError("Older CSV identity has recorded trades or manual matches; review before relinking.")
                        identity_moves.append((position_id, matches[0]["id"],
                                               last.isoformat(), symbol, currency))
                        position_id = matches[0]["id"]
                    elif not matches:
                        identity_assignments.append((con_id, position_id))
                baseline_inputs.append((position_id, currency, quantity, close_price))
                continue
            all_by_conid = [position for position in positions if con_id and
                            position["con_id"] == con_id]
            if any(position["security_type"] != "STK" or
                   position["currency"].upper() != currency for position in all_by_conid):
                raise LegacyHistoryError("Statement contract conflicts with a saved instrument.")
            by_conid = all_by_conid
            if con_id and not by_conid and any(
                    position["security_type"] == "STK" and
                    position["currency"].upper() == currency and
                    position["con_id"] not in (None, con_id) and
                    symbol in {position["ib_symbol"].strip().upper(),
                               position["display_ticker"].strip().upper()}
                    for position in positions):
                raise LegacyHistoryError("Statement symbol conflicts with another saved contract.")
            candidates = by_conid or [position for position in positions
                                      if position["security_type"] == "STK" and
                                      position["currency"].upper() == currency and
                                      position["con_id"] in (None, con_id) and
                                      symbol in {position["ib_symbol"].strip().upper(),
                                                 position["display_ticker"].strip().upper()}]
            if len(candidates) > 1:
                raise LegacyHistoryError("Opening position matches multiple saved instruments; review its identity.")
            position_id = candidates[0]["id"] if candidates else None
            planned_openings.append((position_id, symbol, currency, quantity, close_price, con_id))
        # An unchanged report may be reimported after this feature or after FX
        # becomes available. Add only missing estimated lots.
        missing_baselines = any(
            _baseline_gap(db, portfolio_id, position_id, last.isoformat(),
                          quantity, close_price, currency) is not None
            for position_id, currency, quantity, close_price in baseline_inputs)
        baseline_positions = set()
        if (new_months or new_statement or new_performance or planned_openings or
                mark_updates or contract_updates or identity_moves or identity_assignments or
                missing_baselines):
            db.backup()
            with db.conn:
                db.conn.executemany(
                    """INSERT INTO account_monthly_history
                       (portfolio_id,account_id,month,period_from,period_to,nav_usd,return_pct,source_hash)
                       VALUES (?,?,?,?,?,?,?,?)""", new_months)
                if new_performance:
                    db.conn.execute(
                        """INSERT INTO legacy_account_performance
                           (portfolio_id,account_id,period_from,period_to,cumulative_return_pct,
                            ending_nav_usd,measure,source_hash) VALUES (?,?,?,?,?,?,?,?)""",
                        (portfolio_id, account, *performance, pa_hash))
                db.conn.executemany("""UPDATE legacy_position_openings SET close_price=?
                    WHERE portfolio_id=? AND account_id=? AND as_of=? AND symbol=? AND currency=?""",
                    mark_updates)
                db.conn.executemany("""UPDATE legacy_position_openings SET con_id=?
                    WHERE portfolio_id=? AND account_id=? AND as_of=? AND symbol=? AND currency=?""",
                    contract_updates)
                for con_id, position_id in identity_assignments:
                    db.conn.execute("UPDATE positions SET con_id=? WHERE id=? AND con_id IS NULL",
                                    (con_id, position_id))
                    db.conn.execute("INSERT OR IGNORE INTO contracts(position_id,con_id) VALUES (?,?)",
                                    (position_id, con_id))
                for old_id, new_id, day, symbol, currency in identity_moves:
                    old_lots = db.conn.execute("""SELECT id FROM trades WHERE position_id=?
                        AND source=?""", (old_id, MARKET_BASELINE_SOURCE)).fetchall()
                    for row in old_lots:
                        db.conn.execute("DELETE FROM trade_matches WHERE buy_trade_id=? OR sell_trade_id=?",
                                        (row["id"], row["id"]))
                        db.conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                    db.conn.execute("""UPDATE legacy_position_openings SET position_id=?
                        WHERE portfolio_id=? AND account_id=? AND as_of=? AND symbol=? AND currency=?""",
                        (new_id, portfolio_id, account, day, symbol, currency))
                    db.conn.execute("""DELETE FROM positions WHERE id=? AND con_id IS NULL
                        AND notes='' AND position_qty=0 AND average_cost=0
                        AND NOT EXISTS (SELECT 1 FROM trades WHERE position_id=?)
                        AND NOT EXISTS (SELECT 1 FROM legacy_position_openings WHERE position_id=?)
                        AND NOT EXISTS (SELECT 1 FROM contracts WHERE position_id=?)
                        AND NOT EXISTS (SELECT 1 FROM market_quotes WHERE position_id=?)""",
                        (old_id, old_id, old_id, old_id, old_id))
                    baseline_positions.update((old_id, new_id))
                for position_id, symbol, currency, quantity, close_price, con_id in planned_openings:
                    if position_id is None:
                        position_id = db.conn.execute(
                            """INSERT INTO positions
                               (portfolio_id,display_ticker,ib_symbol,security_type,currency,con_id)
                               VALUES (?,?,?,'STK',?,?)""",
                            (portfolio_id, symbol, symbol, currency, con_id)).lastrowid
                    elif con_id:
                        db.conn.execute("UPDATE positions SET con_id=? WHERE id=? AND con_id IS NULL",
                                        (con_id, position_id))
                    if con_id:
                        db.conn.execute("""INSERT OR IGNORE INTO contracts(position_id,con_id)
                            VALUES (?,?)""", (position_id, con_id))
                    db.conn.execute(
                        """INSERT INTO legacy_position_openings
                           (portfolio_id,account_id,position_id,as_of,symbol,currency,quantity,source_hash,close_price,con_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (portfolio_id, account, position_id, last.isoformat(), symbol, currency,
                         quantity, statement_hash, close_price, con_id))
                    baseline_inputs.append((position_id, currency, quantity, close_price))
                for position_id, currency, quantity, close_price in baseline_inputs:
                    if _baseline_lot(db, portfolio_id, position_id, last.isoformat(),
                                     quantity, close_price, currency):
                        baseline_positions.add(position_id)
                for position_id, *_ in baseline_inputs:
                    latest = db.conn.execute("""SELECT MAX(as_of) FROM legacy_position_openings
                        WHERE portfolio_id=? AND position_id=?""",
                        (portfolio_id, position_id)).fetchone()[0]
                    obsolete = db.conn.execute("""SELECT id FROM trades WHERE portfolio_id=?
                        AND position_id=? AND source=? AND trade_date<>?""",
                        (portfolio_id, position_id, MARKET_BASELINE_SOURCE, latest)).fetchall()
                    for row in obsolete:
                        db.conn.execute("DELETE FROM trade_matches WHERE buy_trade_id=? OR sell_trade_id=?",
                                        (row["id"], row["id"]))
                        db.conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                        baseline_positions.add(position_id)
                if new_statement:
                    db.conn.execute(
                        """INSERT INTO legacy_statement_sources
                           (portfolio_id,account_id,period_from,period_to,source_hash,
                            ending_nav_usd,ending_cash_usd,dividend_accrual_usd,event_count)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (portfolio_id, account, first.isoformat(), last.isoformat(), statement_hash,
                         str(nav["Total"]), str(nav["Cash"]), str(nav["Dividend Accruals"]),
                         len(evidence)))
                    counts = {}
                    for section, day, payload in evidence:
                        counts[section] = counts.get(section, 0) + 1
                        db.conn.execute(
                            """INSERT INTO legacy_statement_rows
                               (portfolio_id,account_id,period_from,period_to,section,
                                row_number,reported_day,payload_json) VALUES (?,?,?,?,?,?,?,?)""",
                            (portfolio_id, account, first.isoformat(), last.isoformat(),
                             section, counts[section], day, payload))
            if baseline_positions:
                from app.finance.services import MatchingService
                MatchingService(db, portfolio_id).rebuild_auto(baseline_positions)
    return {"period_from": first.isoformat(), "period_to": last.isoformat(),
            "months": len(months), "new_months": len(new_months),
            "evidence_rows": len(evidence), "new_statement": new_statement,
            "opening_positions": len(openings), "new_opening_positions": len(planned_openings),
            "new_performance": new_performance,
            "estimated_opening_lots": len(baseline_positions)}


def remove_legacy_history(db, account, *, portfolio_id=1):
    """Remove saved older CSV observations for one portfolio and account."""
    if not account or db.setting("ib_account_id") != account:
        raise LegacyHistoryError("The selected account does not match the saved IBKR account.")
    db.validate_account_ownership(account)
    tables = ("legacy_statement_rows", "legacy_statement_sources",
              "legacy_position_openings", "legacy_account_performance",
              "account_monthly_history")
    with single_writer_lock(db.path, "ibkr-legacy-history.lock"):
        counts = {table: db.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE portfolio_id=? AND account_id=?",
            (portfolio_id, account)).fetchone()[0] for table in tables}
        baseline_rows = db.conn.execute("""SELECT t.id,t.position_id FROM trades t
            JOIN legacy_position_openings o ON o.position_id=t.position_id
              AND o.as_of=t.trade_date AND o.portfolio_id=t.portfolio_id
            WHERE o.portfolio_id=? AND o.account_id=? AND t.source=?""",
            (portfolio_id, account, MARKET_BASELINE_SOURCE)).fetchall()
        if any(counts.values()):
            db.backup()
            with db.conn:
                for row in baseline_rows:
                    db.conn.execute("DELETE FROM trade_matches WHERE buy_trade_id=? OR sell_trade_id=?",
                                    (row["id"], row["id"]))
                    db.conn.execute("DELETE FROM trades WHERE id=?", (row["id"],))
                for table in tables:
                    db.conn.execute(
                        f"DELETE FROM {table} WHERE portfolio_id=? AND account_id=?",
                        (portfolio_id, account))
            if baseline_rows:
                from app.finance.services import MatchingService
                MatchingService(db, portfolio_id).rebuild_auto(
                    {row["position_id"] for row in baseline_rows})
    return {"months": counts["account_monthly_history"],
            "statements": counts["legacy_statement_sources"],
            "opening_positions": counts["legacy_position_openings"],
            "performance_periods": counts["legacy_account_performance"]}
