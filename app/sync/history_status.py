"""Local status for the most recent dated Flex import."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from app.storage.local_files import replace_with_retry


def status_path(database_path: Path) -> Path:
    return Path(database_path).parent / "history-import-status.json"


def pause_request_path(path: Path) -> Path:
    """Separate control file so the GUI cannot overwrite worker progress."""
    return Path(path).with_suffix('.pause')


def read_status(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_status(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                prefix="history-status-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _merged_ranges(windows: list) -> list[tuple[str, str]]:
    merged = []
    for first, last in sorted((date.fromisoformat(pair[0]), date.fromisoformat(pair[1]))
                              for pair in windows):
        if merged and first <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return [(first.isoformat(), last.isoformat()) for first, last in merged]


def synced_history_coverage(db, portfolio_id: int) -> list[dict]:
    """Return saved ranges and review ranges without bridging unverified gaps."""
    account = db.setting("ib_account_id").strip()
    if not account:
        return []
    result = []
    for label, source in (("Trades", "flex"), ("Analytics", "flex_analytics"),
                          ("Cash", None)):
        if source:
            windows = db.conn.execute("""SELECT from_day,to_day FROM broker_sync_windows
                WHERE account_id=? AND source=? AND status='complete'""",
                (account, source)).fetchall()
        else:
            windows = db.conn.execute("""SELECT DISTINCT period_from,period_to
                FROM cash_reconciliations WHERE account_id=? AND portfolio_id=?""",
                (account, portfolio_id)).fetchall()
        pending = []
        if source:
            pending = db.conn.execute("""SELECT from_day,to_day FROM broker_sync_windows
                WHERE account_id=? AND source=? AND status='review'""",
                (account, source)).fetchall()
        result.append({"label": label, "ranges": _merged_ranges(windows),
                       "review": _merged_ranges(pending)})
    return result


def imported_statement_trade_summary(db, portfolio_id: int) -> tuple[int, str, str]:
    account = db.setting("ib_account_id").strip()
    if not account:
        return 0, "", ""
    row = db.conn.execute("""SELECT COUNT(DISTINCT t.id),MIN(t.trade_date),MAX(t.trade_date)
        FROM broker_source_records r JOIN trades t ON t.id=r.trade_row_id
        WHERE r.account_id=? AND r.source='activity_statement' AND r.status='complete'
          AND t.portfolio_id=?""", (account, portfolio_id)).fetchone()
    return int(row[0] or 0), row[1] or "", row[2] or ""


def imported_statement_transfer_summary(db, portfolio_id: int) -> tuple[int, float]:
    row = db.conn.execute('''SELECT COUNT(DISTINCT t.position_id),COALESCE(SUM(t.quantity),0)
        FROM broker_source_records r JOIN trades t ON t.id=r.trade_row_id
        WHERE r.account_id=? AND r.source='activity_statement_transfer_lot'
          AND r.status='complete' AND t.portfolio_id=?''',
        (db.setting('ib_account_id').strip(), portfolio_id)).fetchone()
    return int(row[0]), float(row[1])


def reconciliation_status(db, portfolio_id: int = 1) -> dict:
    """Read current saved evidence, distinguishing missing inputs from failed links."""
    account = db.setting('ib_account_id').strip()
    groups = defaultdict(list)
    purchases = set()
    saved_trades = set()
    transferred = set()
    for row in db.conn.execute('''SELECT p.con_id,t.trade_date,t.source,t.side FROM trades t
            JOIN positions p ON p.id=t.position_id
            WHERE t.portfolio_id=? ''', (portfolio_id,)):
        saved_trades.add((str(row['con_id']), row['trade_date'], row['side']))
        if row['side'] == 'BUY':
            purchases.add((str(row['con_id']), row['trade_date']))
        if row['side'] == 'BUY' and row['source'] in {'IBKR legacy transfer', 'IBKR statement transfer'}:
            transferred.add(str(row['con_id']))
    for row in db.conn.execute('''SELECT r.* FROM broker_source_records r
            LEFT JOIN trades t ON t.id=r.trade_row_id WHERE r.account_id=?
            AND (t.id IS NULL OR t.portfolio_id=?) AND
            ((r.status='review' AND (r.source IN ('flex_closed_lot','flex_fee','flex_event')
                OR (r.source='flex_open_lot' AND r.issue LIKE 'Broker%')))
             OR (r.status='pending' AND (r.source='flex_closed_lot'
                OR (r.source='flex_open_lot' AND r.issue LIKE 'Waiting for earlier%'))))
            ORDER BY r.source,r.issue,r.source_id''', (account, portfolio_id)):
        try:
            raw = json.loads(row['raw_json'])
            raw = raw.get('raw', raw) if isinstance(raw, dict) else {}
            raw = raw if isinstance(raw, dict) else {}
        except (ValueError, TypeError):
            raw = {}
        # Unlinked lots still belong to the selected portfolio's contract.
        conid = raw.get('conid') or raw.get('con_id')
        if (conid and db.conn.execute('SELECT 1 FROM positions WHERE con_id=?', (conid,)).fetchone()
                and not db.conn.execute('SELECT 1 FROM positions WHERE portfolio_id=? AND con_id=?',
                                        (portfolio_id, conid)).fetchone()):
            continue
        symbol = str(raw.get('symbol') or conid or 'Unknown instrument')
        groups[row['status'], row['source'], row['issue'], symbol].append(raw)

    def day(value):
        value = str(value or '').split(';')[0]
        if len(value) >= 8 and value[:8].isdigit():
            value = f'{value[:4]}-{value[4:6]}-{value[6:8]}'
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return ''

    result = dict(review_issues=[], pending_issues=[], reconciliation_guidance=[], reconciliation_details=[],
                  reconciliation_actions=[])
    actions = set()
    for (state, source, issue, symbol), records in groups.items():
        result['review_issues' if state == 'review' else 'pending_issues'].append(
            f'{symbol}: {issue} ({len(records)} records)')
        sales = sorted({value for raw in records if (value := day(raw.get('dateTime')))})
        opened = sorted({value for raw in records if (value := day(raw.get('openDateTime')))})
        snapshots = sorted({value for raw in records if (value := day(raw.get('reportDate')))})
        lower = issue.casefold()
        if 'basis' in lower or 'allocation' in lower:
            next_action = 'Medium - Copy diagnostics and share them for review.'
            needed = 'A verified purchase-lot allocation and cost basis that agree with IBKR.'
            action = ('Broker lot data is saved, but its cost basis or allocation does not reconcile. '
                      'This needs app-side review; another statement is not known to be missing. '
                      'Copy diagnostics for investigation. Do not change purchase prices or lot matches to force agreement.')
        elif 'sale execution' in lower:
            needed = 'One closing execution linked to its broker Closed Lots (purchase dates, quantities and basis).'
            if all((str(raw.get('conid') or raw.get('con_id')), day(raw.get('dateTime')),
                    str(raw.get('buySell') or 'SELL').upper()) in saved_trades
                   for raw in records):
                next_action = 'Medium - Copy diagnostics and share them for review.'
                action = ('Trades on the listed dates and broker Closed Lots are already saved, '
                          'but the closing trade is not linked unambiguously. This needs app-side matching; '
                          'no additional download is identified. Copy diagnostics for investigation.')
            else:
                next_action = 'Medium - Check closing trade dates in Show Details; import any missing Trades.'
                action = ('Some broker Closed Lots are not linked to one closing trade. If the trade is already saved, '
                          'this needs app-side matching; copy diagnostics. Otherwise import Trades for the closing dates in Details.')
        elif 'earlier' in lower or 'purchase history' in lower:
            needed = 'The original purchase or transferred opening, including quantity and original cost basis.'
            missing = []
            for raw in records:
                conid = str(raw.get('conid') or raw.get('con_id') or '')
                bought = day(raw.get('openDateTime'))
                if bought and (conid, bought) not in purchases and conid not in transferred:
                    missing.append(bought)
            if missing:
                next_action = 'Medium - Import missing purchases/openings; see dates and required CSV in Show Details.'
                cutoff = date(date.today().year - 4, 1, 1).isoformat()
                dates = ', '.join(sorted(set(missing)))
                action = (f'Purchase/opening records are missing for {dates}. '
                          'Use Import history with source Trades for dates within Flex availability first; '
                          'the earlier report may not have been checked. For older dates use '
                          'Import older trades… with an English Activity Statement CSV. '
                          'For transferred stocks, start the statement at account opening and include Transfers, '
                          'Open Positions and Financial Instrument Information. '
                          f'Current Flex range starts {cutoff}.')
            else:
                next_action = 'Medium - Copy diagnostics and share them for review.'
                action = ('Earlier purchases or transferred openings are not linked to these broker lots. '
                          'Saved holdings may already contain the needed records; copy diagnostics for app-side review '
                          'before downloading another statement.')
        else:
            next_action = 'Medium - Copy diagnostics and share them for review.'
            needed = 'A verified link between the saved broker record and local history.'
            action = 'The saved broker record needs app-side review. Copy diagnostics; do not edit the database manually.'
        if action not in actions:
            result['reconciliation_guidance'].append(action)
            actions.add(action)
        if next_action not in result['reconciliation_actions']:
            result['reconciliation_actions'].append(next_action)
        details = [f'{symbol} — {issue} ({len(records)} records)', f'Needed: {needed}']
        if sales:
            details.append(('Closing trade dates: ' if source == 'flex_closed_lot' else 'Trade dates: ') + ', '.join(sales))
        if opened:
            details.append('Original purchase dates: ' + ', '.join(opened))
        if snapshots:
            details.append('Position report dates: ' + ', '.join(snapshots))
        result['reconciliation_details'].append('\n'.join(details))
    return result


def current_history_result(db, status: dict, portfolio_id: int = 1) -> dict:
    """Keep report outcomes, but replace stale lot warnings after CSV recovery."""
    result = dict(status)
    if status.get('state') in {'starting', 'running'} or status.get('account_id') != db.setting('ib_account_id').strip():
        return result
    result.update(reconciliation_status(db, portfolio_id))
    result['statement_executions'] = imported_statement_trade_summary(db, portfolio_id)[0]
    result['statement_transfer_stocks'] = imported_statement_transfer_summary(db, portfolio_id)[0]
    if status.get('state') in {'completed', 'completed_with_review', 'completed_with_pending_history'}:
        result['state'] = ('completed_with_review' if result['review_issues'] else
                           'completed_with_pending_history' if result['pending_issues'] else 'completed')
    return result


def format_synced_history(db, portfolio_id: int) -> str:
    """Describe persisted coverage, preserving gaps and excluding unfinished reports."""
    account = db.setting("ib_account_id").strip()
    if not account:
        return "History synced: set an IBKR account to see synced dates."
    lines = [f"History synced for {account} (inclusive dates):"]
    for row in synced_history_coverage(db, portfolio_id):
        ranges = "; ".join(f"{first} to {last}" for first, last in row["ranges"])
        lines.append(f"{row['label']}: {ranges or 'No synced history recorded'}")
        if row["review"]:
            ranges = "; ".join(f"{first} to {last}" for first, last in row["review"])
            lines.append(f"{row['label']} needs review: {ranges}")
    count, first, last = imported_statement_trade_summary(db, portfolio_id)
    if count:
        lines.append(f"Older Activity Statement executions: {count:,} individual trades ({first} to {last}); this is not continuous date coverage.")
    stocks, shares = imported_statement_transfer_summary(db, portfolio_id)
    if stocks:
        lines.append(f"Activity Statement transferred holdings: {stocks:,} stocks ({shares:g} shares); quantity and basis verified from broker lots.")
    lines.append("Separate ranges indicate gaps in Flex coverage; Activity Statement records do not fill continuous Flex coverage.")
    return "\n".join(lines)


def format_history_summary(status: dict) -> tuple[str, str]:
    """Return a short message first, with technical evidence left to Details."""
    state = status.get("state", "?")
    outside = status.get("outside_history_windows") or {}
    review = status.get("review_issues") or []
    pending = status.get("pending_issues") or []
    completed = status.get("completed_windows") or {}
    saved = status.get("saved_windows") or {}
    sources = ("trades", "analytics", "cash") if status.get("source") == "all" else (status.get("source", "?"),)
    has_saved = any(completed.get(source) or saved.get(source) for source in sources)
    if state == "failed":
        title = "History import failed"
        parts = ['Saved reports are kept.', 'High - Open Show Details, fix the reported error, then retry.']
    elif state == "paused":
        title = "History import paused"
        parts = ['Saved reports are kept. Select Resume import when ready.']
    elif state in ("starting", "running"):
        title = "History import in progress"
        parts = ["The current report and progress are shown in Details."]
    elif any((status.get('unavailable_windows') or {}).values()) or any((status.get('unverified_windows') or {}).values()) or (
            state == 'completed_with_gaps' and not any(outside.values())):
        title = "History imported with gaps"
        parts = ['Saved reports are kept.', 'Medium - Open Show Details to check missing dates before retrying.']
    elif review or pending:
        title = 'History saved; some lots need review'
        parts = []
    elif any(outside.values()) and not has_saved:
        title = 'Older dates are outside Flex'
        parts = ['No Flex reports were imported for those dates.']
    else:
        title = 'History saved'
        parts = []
    if has_saved and (review or pending):
        parts.insert(0, 'Daily use can continue. Flagged quantities, cost basis or P&L remain unverified.')
    if review or pending:
        parts.extend(status.get('reconciliation_actions') or
                     ['Medium - Open Show Details for unresolved lots and next steps.'])
    trade_outside = _merged_ranges(outside.get("trades") or [])
    if trade_outside:
        if status.get('statement_executions') or status.get('statement_transfer_stocks'):
            parts.append('Low - Older statement records are already saved. No need to import the same statement again.')
        else:
            ranges = '; '.join(f'{first} to {last}' for first, last in trade_outside)
            parts.append(f'Medium - Import older trades… for {ranges}; see the CSV instructions in Show Details.')
    elif any(outside.values()):
        parts.append('Low - Older Analytics/Cash dates are outside Flex; see Show Details. The older-trades CSV does not fill them.')
    return title, '\n\n'.join(dict.fromkeys(parts)) or 'No action needed.'


def format_history_details(status: dict) -> str:
    selected = status.get("source", "?")
    sources = ("trades", "analytics", "cash") if selected == "all" else (selected,)
    state = status.get("state", "?")
    completed = status.get("completed_windows") or {}
    has_completed = any(completed.get(source) for source in sources)
    saved_windows = status.get("saved_windows") or {}
    has_saved = has_completed or any(saved_windows.get(source) for source in sources)
    heading = {"completed": "History import succeeded.",
               "completed_with_gaps": "Available history was imported; some requested dates are not included.",
               'completed_with_review': 'Saved Flex history has trade records that need app-side review.',
               'completed_with_pending_history': 'Saved Flex history needs earlier trades to reconcile some lots.',
               'paused': 'History backfill paused; imported data was saved.',
               "failed": "Import failed.",
               "running": "Import is running.", "starting": "Import is starting."}.get(state, "Import status unknown.")
    if state == "completed_with_gaps" and not has_saved:
        heading = "The requested dates are outside Flex availability; no history was imported."
    if status.get('pause_requested') and state in ('starting', 'running'):
        heading = 'Pausing after the current report is saved.'
    elif status.get('paused_by_user') and state == 'paused':
        heading = 'Import paused; imported data was saved. Use Resume import to continue.'
    lines = [heading, f"Source: {selected}",
             f"Requested: {status.get('from_date', '?')} to {status.get('to_date', '?')}",
             f"Status: {state}"]
    if status.get("completeness_limit"):
        lines.extend(("", status["completeness_limit"]))
    if state in ('completed', 'completed_with_gaps', 'completed_with_review',
                 'completed_with_pending_history'):
        success_text = ("Flex reports completed in this attempt were saved locally." if has_completed else
                        "No new Flex reports were downloaded in this attempt.")
        lines.extend(('', f"What succeeded: {success_text}"))
        if has_saved:
            lines.append("Daily use can continue; flagged trade-lot matches and their P&L may be unverified.")
    saved = status.get('saved_windows')
    if isinstance(saved, dict):
        lines.extend(('', 'Total saved history:'))
        for source in sources:
            ranges = ', '.join(f'{first} to {last}' for first, last in _merged_ranges(saved.get(source) or []))
            lines.append(f'{source.title()}: {ranges or "No synced history recorded"}')
    completed = status.get("completed_windows")
    if isinstance(completed, dict):
        started = status.get("started_sources") or []
        lines.extend(("", "Completed in this attempt:"))
        for source in sources:
            windows = completed.get(source) or []
            if windows:
                ranges = ", ".join(f"{first} to {last}" for first, last in _merged_ranges(windows))
                count = len(windows)
                lines.append(f"{source.title()}: {ranges} ({count} report window{'s' if count != 1 else ''})")
            elif source in started:
                lines.append(f"{source.title()}: no windows completed")
            else:
                lines.append(f"{source.title()}: not started")
    else:
        lines.extend(("", "Completed ranges were not recorded for this earlier attempt."))
    unavailable = status.get("unavailable_windows") or {}
    if any(unavailable.values()):
        lines.extend(("", "IBKR did not provide reports for these requested windows:"))
        for source in sources:
            spans = unavailable.get(source) or []
            if spans:
                ranges = ", ".join(f"{first} to {last}" for first, last in _merged_ranges(spans))
                lines.append(f"{source.title()}: {ranges}")
        lines.append("Possible reasons include dates before account opening or limits on historical report availability. IBKR did not specify the cause.")
    outside = status.get("outside_history_windows") or {}
    before = status.get('before_account_windows') or {}
    if any(before.values()):
        lines.extend(('', 'Before account opening (not requested):'))
        for source in sources:
            if before.get(source):
                lines.append(f'{source.title()}: ' + ', '.join(
                    f'{first} to {last}' for first, last in _merged_ranges(before[source])))
    if any(outside.values()):
        lines.extend(("", "Older dates outside IBKR Flex availability (not imported):"))
        for source in sources:
            spans = outside.get(source) or []
            if spans:
                lines.append(f"{source.title()}: " + ", ".join(
                    f"{first} to {last}" for first, last in _merged_ranges(spans)))
        if outside.get("trades"):
            trade_ranges = "; ".join(
                f"{first} to {last}" for first, last in _merged_ranges(outside["trades"]))
            lines.extend((
                f"To recover individual executions for {trade_ranges}, if the account was open then:",
                "1. IBKR Client Portal > Performance & Reports > Statements.",
                "2. Select the period and download an English Activity Statement CSV.",
                "3. In Portfolio Monitor, choose Settings > Import older trades and select the CSV.",
                "Include Trades and Financial Instrument Information. For transferred stocks, start at account opening and include Transfers and Open Positions.",
                "This restores executions and verified transferred holdings; it does not add older Analytics/Cash history or mark Flex dates as covered."))
        elif any(outside.values()):
            lines.append("Import older trades restores executions and verified transferred holdings, not Analytics or Cash history.")
    unverified = status.get("unverified_windows") or {}
    if any(unverified.values()):
        lines.extend(("", "Not checked in this attempt:"))
        for source in sources:
            spans = unverified.get(source) or []
            if spans:
                lines.append(f"{source.title()}: " + ", ".join(
                    f"{first} to {last}" for first, last in _merged_ranges(spans)))
    if status.get("stopped_after_unavailable"):
        paused = ', '.join(source.title() for source in status.get('paused_sources') or [])
        lines.extend(("", f"Paused {paused or 'the affected source'} after repeated unavailable reports. "
                      "Other sources can continue. A listed unavailable window may still contain some available days."))
    failures = status.get('failed_windows') or {}
    if failures:
        lines.extend(('', 'Failed report windows (not saved):'))
        for label, records in failures.items():
            for record in records:
                lines.append(f"{label.title()}: {record['from_date']} to {record['to_date']}: {record['error']}")
    failed = None if failures else status.get("failed_window")
    if failed and len(failed) == 2:
        lines.extend(("", f"Failed window: {str(status.get('active_source') or '?').title()} "
                          f"{failed[0]} to {failed[1]}"))
    elif not failures and status.get("current_window"):
        current = status["current_window"]
        lines.extend(("", f"Current window: {str(status.get('active_source') or '?').title()} "
                          f"{current[0]} to {current[1]}"))
    if status.get("message"):
        lines.extend(("", f"Details: {status['message']}"))
    if status.get('review_issues'):
        lines.extend(('', 'App-side trade reconciliation (no manual database edits needed):',
                      'These records need investigation in the app; importing the same dates again is unlikely to fix them.',
                      *status['review_issues']))
    if status.get('pending_issues'):
        lines.extend(('', 'Lots waiting for earlier trades:',
                      'This affects lot matching and related P&L. The count does not prove all purchases are outside Flex history; some executions may already be saved but need app-side matching. Do not repeat the full import based on this warning alone.',
                      *status['pending_issues']))
    if status.get('reconciliation_guidance'):
        lines.extend(('', 'What is needed and what to do next:', *status['reconciliation_guidance'],
                      '', 'Affected instruments and dates:', *status.get('reconciliation_details', [])))
    if status.get("state") == "failed" and not failures:
        lines.extend(("", "Other dates were not verified by this attempt."))
    return "\n".join(lines)


def process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x101000, 0, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied still means a process exists
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
