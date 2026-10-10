"""Fetch dated Activity Flex history into the local portfolio database."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import sys

from app.imports.analytics_flex import run_analytics_flex_sync
from app.imports.cash_flex import run_cash_flex_sync, CashReconciliationError
from app.storage.database import Database
from app.imports.flex_import import DEFAULT_REQUEST_PACING_SECONDS, FlexRequestPacer, FlexServiceError
from app.sync.sync_service import run_flex_sync
from app.sync.history_coverage import missing_windows, account_open_day, covered_windows
from app.broker.broker_lots import restore_saved_lot_links, reconcile_closed_lots, check_open_lots
from app.finance.services import MatchingService


WINDOW_DAYS = {"trades": 365, "analytics": 365, "cash": 365}
QUERY_KEYS = {"trades": "flex_query_id", "analytics": "analytics_flex_query_id",
              "cash": "cash_flex_query_id"}
RUNNERS = {"trades": run_flex_sync, "analytics": run_analytics_flex_sync,
           "cash": run_cash_flex_sync}
REQUEST_PACING_SECONDS = DEFAULT_REQUEST_PACING_SECONDS


def windows(first: date, last: date, days: int) -> list[tuple[str, str]]:
    result = []
    while first <= last:
        end = min(first + timedelta(days=days - 1), last)
        result.append((first.isoformat(), end.isoformat()))
        first = end + timedelta(days=1)
    return result


def missing_cash_windows(db, first: date, last: date) -> list[tuple[str, str]]:
    """Avoid replaying reconciled cash periods when retrying a partial import."""
    return missing_windows(db, db.setting('ib_account_id'), 'cash', first, last, WINDOW_DAYS['cash'])


def main(argv=None, *, progress=None, should_pause=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=(*WINDOW_DAYS, "all"), required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--to-date", type=date.fromisoformat, required=True)
    parser.add_argument("--database", help="Optional path to an existing local portfolio SQLite file")
    args = parser.parse_args(argv)
    if args.from_date > args.to_date or args.to_date >= date.today():
        parser.error("Choose an ordered date range ending before today")
    if args.database:
        from pathlib import Path
        if not Path(args.database).is_file():
            parser.error("The selected database does not exist")
    try:
        db = Database(args.database)
    except Exception:
        parser.error("Portfolio storage is unavailable. Open the app to resolve migration, recovery, or active-session errors.")
    try:
        account = db.setting("ib_account_id")
        if not account:
            parser.error("Set the IBKR account in the app before importing history")
        sources = list(WINDOW_DAYS) if args.source == "all" else [args.source]
        for source in sources:
            query = db.setting(QUERY_KEYS[source])
            if not query:
                parser.error(f"Set the {source} Flex query ID in the app first")
        earliest = date(date.today().year - 4, 1, 1)
        opened = account_open_day(db, account)
        first_account = max(args.from_date, opened or args.from_date)
        first_eligible = max(first_account, earliest)
        # An explicit retry may resume a broker pause. Startup never clears it.
        db.set_metadata('ibkr_flex_paused', '')
        spans_by_source = {}
        for source in sources:
            if opened and args.from_date < opened and progress:
                progress('windows_before_account', source, [[args.from_date.isoformat(),
                         min(args.to_date, opened - timedelta(days=1)).isoformat()]])
            if first_account < earliest and first_account <= args.to_date and progress:
                progress("windows_outside_history", source,
                         [[first_account.isoformat(),
                           min(args.to_date, earliest - timedelta(days=1)).isoformat()]])
            spans_by_source[source] = (
                missing_windows(db, account, source, first_eligible, args.to_date, WINDOW_DAYS[source],
                                report_through=date.today() - timedelta(days=1)))
        schedule = sorted(((source, span) for source in sources
                           for span in spans_by_source[source]),
                          key=lambda item: (item[1][1], item[1][0], -sources.index(item[0])),
                          reverse=True)
        if progress:
            progress("schedule_ready", args.source, len(schedule))
        if schedule or db.conn.execute("""SELECT 1 FROM broker_source_records WHERE account_id=?
                AND source='flex_closed_lot' AND execution_key='' LIMIT 1""", (account,)).fetchone():
            db.backup()
        pacers = {source: FlexRequestPacer(REQUEST_PACING_SECONDS) for source in sources}
        for source, pacer in pacers.items():
            pacer.reuse_cached = True
            pacer.should_pause = should_pause
            pacer.large_window_recovery = True
        paused = set()
        started = set()
        failed_sources = set()
        completed = {source: 0 for source in sources}

        def mark_unverified(remaining):
            if progress:
                for source in sources:
                    spans = [list(span) for label, span in remaining if label == source]
                    if spans:
                        progress("windows_unverified", source, spans)

        def pause_import(remaining):
            mark_unverified(remaining)
            if progress:
                progress('user_paused', args.source)
            print('Import paused; imported data was saved. Resume import to continue.')
            return 0

        for index, (source, span) in enumerate(schedule):
            if should_pause and should_pause():
                return pause_import(schedule[index:])
            if source in paused:
                if progress:
                    progress("batch_skipped", source)
                continue
            pacer = pacers[source]
            opened = account_open_day(db, account)
            if opened and span[0] < opened.isoformat():
                if progress:
                    progress('windows_before_account', source, [[span[0], min(span[1],
                             (opened - timedelta(days=1)).isoformat())]])
                if span[1] < opened.isoformat():
                    if progress:
                        progress("batch_skipped", source)
                    continue
                span = (opened.isoformat(), span[1])
            if source not in started:
                started.add(source)
                if progress:
                    progress("source_start", source)
            unavailable = []
            active_span = span

            def event(name, first, last):
                nonlocal active_span
                if name == "window_start":
                    active_span = (first, last)
                if name == "window_complete":
                    completed[source] += 1
                elif name == "window_unavailable":
                    unavailable.append((first, last))
                if progress:
                    progress(name, source, first, last)

            try:
                if progress:
                    progress("batch_start", source)
                RUNNERS[source](db, account, db.setting(QUERY_KEYS[source]),
                                windows_override=[span], request_pacer=pacer,
                                stop_on_unavailable=True, backup=False,
                                on_window_start=lambda first, last: event("window_start", first, last),
                                on_window_complete=lambda first, last: event("window_complete", first, last),
                                on_window_unavailable=lambda first, last: event("window_unavailable", first, last))
            except Exception as exc:
                if progress:
                    progress("source_failed", source)
                recoverable = isinstance(exc, CashReconciliationError)
                remaining = schedule[index + 1:]
                mark_unverified([(label, pair) for label, pair in remaining if label == source]
                                if recoverable else remaining)
                mark_unverified([(source, pair) for pair in pacer.unverified])
                if recoverable and progress:
                    progress('report_failed', source, list(active_span), str(exc))
                stage = f" during {exc.stage}" if isinstance(exc, FlexServiceError) else ""
                detail = (" IBKR reported too many failed attempts. Check the Flex query in Client Portal "
                          "and wait for IBKR access to recover before retrying."
                          + (" Automatic Trades sync is paused; click Sync IBKR or run Import history "
                             "when ready to retry." if source == "trades" else "")
                          if isinstance(exc, FlexServiceError) and exc.code == 1025 else "")
                print(f"{source} import failed ({active_span[0]} to {active_span[1]}){stage}: {exc}.{detail}",
                      file=sys.stderr)
                if recoverable:
                    failed_sources.add(source)
                    paused.add(source)
                    if progress:
                        progress('batch_complete', source)
                    continue
                return 1
            if pacer.user_paused:
                return pause_import(schedule[index + 1:] +
                                    [(source, pair) for pair in pacer.unverified])
            if progress:
                progress("batch_complete", source)
            if pacer.recovery_stopped:
                paused.add(source)
                mark_unverified([(label, pair) for label, pair in schedule[index + 1:] if label == source])
                mark_unverified([(source, pair) for pair in pacer.unverified])
                if progress:
                    progress('stopped_unavailable', source)
                print(f'{source.title()} backfill paused after repeated unavailable reports. '
                      'Other sources can continue; retry this source explicitly after checking report availability.')
                continue
            if not any(label == source for label, _pair in schedule[index + 1:]) and progress:
                progress("source_complete", source)
        for source in sources:
            count = completed[source]
            print(f"{source}: {count} report window{'s' if count != 1 else ''} processed")
        # Revisit saved evidence even when every requested execution window was
        # already imported. This upgrades older timestamp-only lot associations.
        with db.conn:
            restore_saved_lot_links(db, account, db.setting('flex_query_id'))
            changed = reconcile_closed_lots(db, account)
            if changed:
                MatchingService(db).rebuild_auto(changed, transaction=False)
            check_open_lots(db, account)
        issues = [f"{row[0]}: {row[1]} ({row[2]} records)" for row in db.conn.execute('''
            SELECT source,issue,COUNT(*) FROM broker_source_records WHERE account_id=? AND status='review'
            AND (source IN ('flex_closed_lot','flex_fee','flex_event')
                 OR (source='flex_open_lot' AND issue LIKE 'Broker%'))
            GROUP BY source,issue''', (account,))]
        if issues:
            if progress:
                progress('review_required', 'trades', issues)
            else:
                print('Broker records need review: ' + '; '.join(issues))
        pending = [f"{row[0]}: {row[1]} ({row[2]} records)" for row in db.conn.execute('''
            SELECT source,issue,COUNT(*) FROM broker_source_records
            WHERE account_id=? AND status='pending'
            AND (source='flex_closed_lot' OR (source='flex_open_lot' AND issue LIKE 'Waiting for earlier%'))
            GROUP BY source,issue''', (account,))]
        if pending:
            if progress:
                progress('waiting_for_history', 'trades', pending)
            else:
                print('Waiting for earlier history: ' + '; '.join(pending))
        return 1 if failed_sources else 0
    finally:
        if progress and db.setting('ib_account_id'):
            for source in list(WINDOW_DAYS) if args.source == 'all' else [args.source]:
                progress('saved_coverage', source, [list(pair) for pair in
                    covered_windows(db, db.setting('ib_account_id'), source)])
        db.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
