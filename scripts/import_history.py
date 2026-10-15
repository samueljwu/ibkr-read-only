"""Fetch dated Activity Flex history into the local portfolio database."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import sys
import time

from app.flex.analytics_flex import run_analytics_flex_sync
from app.flex.cash_flex import cash_import_windows, run_cash_flex_sync
from app.storage.database import Database
from app.flex.flex_import import (HISTORY_YEARS, MAX_FLEX_WINDOW_DAYS, FlexServiceError,
                                  FlexValidationError, safe_flex_error)
from app.platform.diagnostics import configure_diagnostics
from app.sync.sync_service import run_flex_sync


SOURCES = ("trades", "analytics", "cash")
QUERY_KEYS = {"trades": "flex_query_id", "analytics": "analytics_flex_query_id",
              "cash": "cash_flex_query_id"}
RUNNERS = {"trades": run_flex_sync, "analytics": run_analytics_flex_sync,
           "cash": run_cash_flex_sync}
WINDOW_DAYS = MAX_FLEX_WINDOW_DAYS
REQUEST_PACING_SECONDS = 15
# IBKR only serves a limited span of Activity Flex history; older dates are
# reported as outside history instead of being requested.


def windows(first: date, last: date, days: int = WINDOW_DAYS) -> list[tuple[str, str]]:
    result = []
    while first <= last:
        end = min(first + timedelta(days=days - 1), last)
        result.append((first.isoformat(), end.isoformat()))
        first = end + timedelta(days=1)
    return result


def prior_weekday(day: date) -> date:
    return day - timedelta(days=max(0, day.weekday() - 4))


def format_flex_failure(code, stage):
    """Describe a service failure without trusting server text or request URLs."""
    return safe_flex_error(FlexServiceError(code, "", stage))


def main(argv=None, progress=None, should_pause=None, completed_windows=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=(*SOURCES, "all"), required=True)
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
    db = Database(args.database)
    try:
        log = configure_diagnostics(db.path, "history-import")
        log.info("Import started: source=%s range=%s to %s", args.source,
                 args.from_date, args.to_date)
        account = db.setting("ib_account_id")
        if not account:
            parser.error("Set the IBKR account in the app before importing history")
        sources = list(SOURCES) if args.source == "all" else [args.source]
        for source in sources:
            if not db.setting(QUERY_KEYS[source]):
                parser.error(f"Set the {source} Flex query ID in the app first")
        floor_day = date(date.today().year - HISTORY_YEARS, 1, 1)
        floor = floor_day.isoformat()
        spans = windows(args.from_date, args.to_date, WINDOW_DAYS)
        available_spans = [(max(start, floor), end) for start, end in spans if end >= floor]

        def emit(event, source, first=None, last=None):
            if progress is not None:
                progress(event, source, first, last)

        def emit_unverified(items):
            by_source = {}
            for item_spans, item_source in items:
                if item_spans:
                    by_source.setdefault(item_source, []).extend([start, end] for start, end in item_spans)
            for item_source, spans in by_source.items():
                emit("windows_unverified", item_source, spans)

        requests = []
        outside = {}
        for source in sources:
            if args.from_date < floor_day:
                outside[source] = [[args.from_date.isoformat(),
                                    min(args.to_date, floor_day - timedelta(days=1)).isoformat()]]
            source_spans = available_spans
            if source == "cash" and available_spans:
                try:
                    source_spans = cash_import_windows(db, account, available_spans[0][0],
                                                      args.to_date.isoformat(), days=WINDOW_DAYS)
                except FlexValidationError as exc:
                    emit("source_start", source)
                    emit("source_needs_review", source, safe_flex_error(exc))
                    print(safe_flex_error(exc), file=sys.stderr)
                    return 1
                expanded = [[start, end] for start, end in source_spans
                            if start < available_spans[0][0] or end > args.to_date.isoformat()]
                if expanded:
                    emit("cash_boundary_periods", source, expanded)
                outside_boundary = [[start, end] for start, end in source_spans if start < floor]
                if outside_boundary:
                    outside.setdefault(source, []).extend(outside_boundary)
                    source_spans = [(start, end) for start, end in source_spans if start >= floor]
                requests.extend(([(start, end)], source) for start, end in source_spans)
            else:
                requests.extend(([(start, end)], source) for start, end in source_spans)
        # Request the newest window first so recent history is saved before an
        # unavailable older window stops the run.
        requests.sort(key=lambda item: (item[0][0][0], item[1]), reverse=True)
        if completed_windows:
            requests = [(spans, source) for spans, source in requests
                        if not all((first, prior_weekday(date.fromisoformat(last)).isoformat())
                                   in completed_windows.get(source, ())
                                   for first, last in spans)]

        for source in sources:
            emit("source_start", source)
            if source in outside:
                emit("windows_outside_history", source, outside[source])

        last_runner_finished_at = None
        for index, (request_spans, source) in enumerate(requests):
            if should_pause is not None and should_pause():
                emit_unverified(requests[index:])
                emit("paused", source)
                return 0
            state = {"unavailable": False, "unavailable_window": None,
                     "completed": set(), "current": None}

            def hook_start(first, last):
                state["current"] = (first, last)
                log.info("Request started: source=%s window=%s to %s", source, first, last)
                emit("window_start", source, first, last)

            def hook_complete(first, last):
                state["completed"].add((first, last))
                log.info("Request completed: source=%s window=%s to %s", source, first, last)
                emit("window_complete", source, first, last)

            def hook_unavailable(first, last):
                state["unavailable"] = True
                state["unavailable_window"] = (first, last)

            def hook_unavailable_detail(first, last, code, stage):
                safe_code = code if type(code) is int and 1000 <= code <= 1999 else "unknown"
                safe_stage = stage if stage in ("SendRequest", "GetStatement") else "Flex service"
                log.warning("Request unavailable: source=%s window=%s to %s code=%s stage=%s",
                            source, first, last, safe_code, safe_stage)

            def hook_result(first, last, outcome):
                log.info("Request result: source=%s window=%s to %s outcome=%s",
                         source, first, last, outcome)
                emit("window_" + outcome, source, first, last)

            # IBKR can reject an end date that falls on a weekend. Request
            # through Friday and keep the omitted dates visible as a gap.
            attempt_spans = []
            for first, last in request_spans:
                end_day = date.fromisoformat(last)
                if end_day.weekday() >= 5:
                    friday = prior_weekday(end_day)
                    omitted_start = max(date.fromisoformat(first), friday + timedelta(days=1))
                    emit("windows_weekend_deferred", source,
                         [[omitted_start.isoformat(), last]])
                    log.info("Weekend end moved to Friday: source=%s original=%s to %s through=%s",
                             source, first, last, friday)
                    if friday.isoformat() < first:
                        continue
                    last = friday.isoformat()
                attempt_spans.append((first, last))
            if not attempt_spans:
                continue

            def emit_remaining():
                pending = [span for span in attempt_spans if span not in state["completed"]
                           and span != state["current"]]
                emit_unverified([(pending, source), *requests[index + 1:]])

            def run_request(spans):
                nonlocal last_runner_finished_at
                # Each runner receives a window or connected Cash group and starts a local pacer.
                # Wait from its return so backups, polling, and retries cannot
                # shorten spacing before the next window or source request.
                if last_runner_finished_at is not None:
                    delay = REQUEST_PACING_SECONDS - (time.monotonic() - last_runner_finished_at)
                    if delay > 0:
                        time.sleep(delay)
                RUNNERS[source](db, account, db.setting(QUERY_KEYS[source]),
                                windows_override=spans,
                                on_window_start=hook_start,
                                on_window_complete=hook_complete,
                                on_window_unavailable=hook_unavailable,
                                on_window_unavailable_detail=hook_unavailable_detail,
                                pacing_seconds=REQUEST_PACING_SECONDS,
                                compare_existing=True, on_window_result=hook_result,
                                **({"stop_on_unavailable": True, "consolidate_existing": True}
                                   if source == "cash" else {}))
                last_runner_finished_at = time.monotonic()

            try:
                run_request(attempt_spans)
            except FlexServiceError as exc:
                emit_remaining()
                safe_code = exc.code if type(exc.code) is int and 1000 <= exc.code <= 1999 else "unknown"
                safe_stage = exc.stage if exc.stage in ("SendRequest", "GetStatement") else "Flex service"
                log.error("Request failed: source=%s window=%s code=%s stage=%s",
                          source, state["current"], safe_code, safe_stage)
                emit("source_failed", source, exc.code, exc.stage)
                print(format_flex_failure(exc.code, exc.stage), file=sys.stderr)
                return 1
            except Exception as exc:
                emit_remaining()
                log.error("Import failed: source=%s window=%s exception=%s",
                          source, state["current"], type(exc).__name__)
                message = safe_flex_error(exc)
                emit("source_needs_review" if message.startswith("Needs review:") else
                     "source_validation_failed", source, message)
                print(message, file=sys.stderr)
                return 1
            if state["unavailable"]:
                first, last = state["unavailable_window"]
                emit("window_unavailable", source, first, last)
                emit_remaining()
                log.warning("Import stopped after unavailable window: source=%s", source)
                emit("stopped_unavailable", source)
                break

        for source in sources:
            emit("source_complete", source)
        log.info("Import finished: source=%s", args.source)
        return 0
    finally:
        db.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
