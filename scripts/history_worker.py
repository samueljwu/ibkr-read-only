"""Run an import and always leave a local completion result for the GUI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
import os
from pathlib import Path
import sys

from app.platform.history_status import pause_request_path, read_status, write_status
from scripts.import_history import format_flex_failure, main as import_history


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "--status-file":
        return 2
    path = Path(argv[1])
    status = read_status(path)
    args = argv[2:]

    def argument(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    resuming = (status["state"] == "paused" and status.get("source") == argument("--source")
                and status["from_date"] == argument("--from-date")
                and status["to_date"] == argument("--to-date"))
    completed = {source: {tuple(span) for span in spans}
                 for source, spans in status["completed_windows"].items()} if resuming else {}
    status.update(state="running", pid=os.getpid(),
                  started_sources=status["started_sources"] if resuming else [],
                  completed_sources=status["completed_sources"] if resuming else [],
                  completed_windows=status["completed_windows"] if resuming else {},
                  unavailable_windows={},
                  unverified_windows={}, outside_history_windows={},
                  weekend_windows=status["weekend_windows"] if resuming else {},
                  updated_windows=status["updated_windows"] if resuming else {},
                  unchanged_windows=status["unchanged_windows"] if resuming else {},
                  review_windows=status["review_windows"] if resuming else {},
                  cash_boundary_periods={}, needs_review=status.get("needs_review", False) if resuming else False,
                  stopped_after_unavailable=False, active_source=None,
                  current_window=None, failed_window=None, message="")
    try:
        write_status(path, status)
    except OSError:
        return 1

    output = StringIO()
    code = 1
    stopped_for_review = False
    stopped_for_pause = False

    def progress(event, source, first=None, last=None):
        nonlocal stopped_for_review, stopped_for_pause
        if event == "source_start":
            if source not in status["started_sources"]:
                status["started_sources"].append(source)
            status["active_source"] = source
        elif event == "window_start":
            status["active_source"] = source
            status["current_window"] = [first, last]
        elif event == "window_complete":
            span = [first, last]
            rows = status["completed_windows"].setdefault(source, [])
            if span not in rows:
                rows.append(span)
            status["current_window"] = None
        elif event in ("window_updated", "window_unchanged", "window_needs_review"):
            key = {"window_updated": "updated_windows", "window_unchanged": "unchanged_windows",
                   "window_needs_review": "review_windows"}[event]
            span = [first, last]
            rows = status[key].setdefault(source, [])
            if span not in rows:
                rows.append(span)
            if event == "window_needs_review":
                status["needs_review"] = True
        elif event == "cash_boundary_periods":
            status["cash_boundary_periods"].setdefault(source, []).extend(first)
        elif event == "window_unavailable":
            status["unavailable_windows"].setdefault(source, []).append([first, last])
            status["current_window"] = None
        elif event == "windows_unverified":
            status["unverified_windows"].setdefault(source, []).extend(first)
        elif event == "windows_weekend_deferred":
            status["weekend_windows"].setdefault(source, []).extend(first)
        elif event == "windows_outside_history":
            status["outside_history_windows"].setdefault(source, []).extend(first)
        elif event == "stopped_unavailable":
            status["stopped_after_unavailable"] = True
        elif event == "paused":
            stopped_for_pause = True
            status["current_window"] = None
            status["active_source"] = None
        elif event == "source_failed":
            stopped_for_review = False
            status["failed_window"] = status["current_window"]
            status["active_source"] = source
            status["current_window"] = None
            status["message"] = format_flex_failure(first, last)
        elif event in ("source_validation_failed", "source_needs_review"):
            stopped_for_review = event == "source_needs_review"
            if event == "source_needs_review":
                status["needs_review"] = True
            if event == "source_needs_review" and status["current_window"]:
                status["review_windows"].setdefault(source, []).append(status["current_window"])
            status["failed_window"] = status["current_window"]
            status["active_source"] = source
            status["current_window"] = None
            status["message"] = first
        elif event == "source_complete":
            if source not in status["completed_sources"]:
                status["completed_sources"].append(source)
            status["active_source"] = None
        try:
            write_status(path, status)
        except OSError as exc:
            print(f"Could not update import progress: {type(exc).__name__}", file=sys.stderr)

    try:
        with redirect_stdout(output), redirect_stderr(output):
            try:
                code = import_history(args, progress=progress,
                                      should_pause=lambda: pause_request_path(path).exists(),
                                      completed_windows=completed)
            except SystemExit as exc:
                stopped_for_review = False
                code = exc.code if isinstance(exc.code, int) else 1
                if code == 2:
                    status["message"] = ("Import configuration rejected. Check the account, query IDs, "
                                         "dates, and database path.")
            except BaseException:
                stopped_for_review = False
                # Exception text and captured output may contain credential-bearing URLs.
                # Persist only fixed messages and safe progress fields.
                status["message"] = "Import failed because of an unexpected error; check local configuration before retrying."
                code = 1
    finally:
        gaps = any(status[key] for key in ("unavailable_windows", "unverified_windows",
                                           "outside_history_windows", "weekend_windows"))
        review = status["needs_review"] and (code == 0 or code == 1 and stopped_for_review)
        status.update(state="paused" if stopped_for_pause and code == 0 else
                      "needs_review" if review else "completed_with_gaps" if code == 0 and gaps else
                      "completed" if code == 0 else "failed", pid=None,
                      finished_at=datetime.now().isoformat(timespec="seconds"),
                      message=status["message"] or
                      ("Import paused after the current report; completed windows were saved."
                       if stopped_for_pause else
                       "Import needs review; existing data was preserved where corrections could not be verified."
                       if review else "Import completed." if code == 0 else "Import failed; check diagnostic logs."))
        try:
            pause_request_path(path).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            write_status(path, status)
        except OSError:
            return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
