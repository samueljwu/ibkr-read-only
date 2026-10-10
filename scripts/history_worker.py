"""Run an import and always leave a local completion result for the GUI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
import os
from pathlib import Path
import sys
import time

from app.sync.history_status import pause_request_path, read_status, write_status
from app.platform.diagnostics import configure_diagnostics
from app.imports.import_contract import CONTRACT_VERSION, COMPLETENESS_LIMIT
from scripts.import_history import main as import_history


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "--status-file":
        return 2
    path = Path(argv[1])
    status = read_status(path)
    status.update(state="running", pid=os.getpid(), pause_supported=True, paused_by_user=False, started_sources=[],
                  support_contract_version=CONTRACT_VERSION,
                  result_scope="validated_report_windows", complete_result=False,
                  completeness_limit=COMPLETENESS_LIMIT,
                  completed_sources=[], completed_windows={}, unavailable_windows={},
                  unverified_windows={}, outside_history_windows={},
                  before_account_windows={},
                  review_issues=[], pending_issues=[], saved_windows={}, paused_sources=[],
                  stopped_after_unavailable=False, active_source=None,
                  current_window=None, failed_window=None, failed_windows={},
                  total_batches=0, processed_batches=0, timed_batches=0,
                  batch_seconds=0.0, batch_started_at=None, batch_finished_at=None)
    try:
        log = configure_diagnostics(path, "history-import")
        write_status(path, status)
    except OSError:
        return 1

    output = StringIO()
    code = 1
    log.info("History import started; source=%s dates=%s to %s",
             status.get("source"), status.get("from_date"), status.get("to_date"))

    batch_started = None

    def progress(event, source, first=None, last=None):
        nonlocal batch_started
        if event == "schedule_ready":
            status["total_batches"] = first
        elif event == "batch_start":
            batch_started = time.monotonic()
            status["batch_started_at"] = datetime.now().isoformat()
        elif event == "batch_complete":
            status["processed_batches"] += 1
            if batch_started is not None:
                status["timed_batches"] += 1
                status["batch_seconds"] += max(0.1, time.monotonic() - batch_started)
            batch_started = None
            status["batch_started_at"] = None
            status["batch_finished_at"] = datetime.now().isoformat()
        elif event == "batch_skipped":
            status["processed_batches"] += 1
        elif event == "source_start":
            status["started_sources"].append(source)
            status["active_source"] = source
        elif event == "window_start":
            status["active_source"] = source
            status["current_window"] = [first, last]
        elif event == "window_complete":
            status["completed_windows"].setdefault(source, []).append([first, last])
            status["current_window"] = None
        elif event == "window_unavailable":
            status["unavailable_windows"].setdefault(source, []).append([first, last])
            status["current_window"] = None
        elif event == "windows_unverified":
            status["unverified_windows"].setdefault(source, []).extend(first)
        elif event == "windows_outside_history":
            status["outside_history_windows"].setdefault(source, []).extend(first)
        elif event == 'windows_before_account':
            status['before_account_windows'].setdefault(source, []).extend(first)
        elif event == 'review_required':
            status['review_issues'] = first
        elif event == 'waiting_for_history':
            status['pending_issues'] = first
        elif event == 'saved_coverage':
            status['saved_windows'][source] = first
        elif event == "stopped_unavailable":
            status["stopped_after_unavailable"] = True
            status['paused_sources'].append(source)
        elif event == 'user_paused':
            status['paused_by_user'] = True
            status['active_source'] = None
            status['current_window'] = None
        elif event == 'report_failed':
            status['failed_windows'].setdefault(source, []).append({
                'from_date': first[0], 'to_date': first[1], 'error': last})
        elif event == "source_failed":
            status["failed_window"] = status["current_window"]
            status["active_source"] = source
            status["current_window"] = None
        elif event == "source_complete":
            status["completed_sources"].append(source)
            status["active_source"] = None
        try:
            write_status(path, status)
        except OSError as exc:
            log.exception("Could not update history import progress")
            print(f"Could not update import progress: {type(exc).__name__}", file=sys.stderr)

    try:
        with redirect_stdout(output), redirect_stderr(output):
            try:
                code = import_history(argv[2:], progress=progress,
                                      should_pause=pause_request_path(path).exists)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except BaseException as exc:
                print(f"Import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                code = 1
    finally:
        gaps = any(status[key] for key in ("unavailable_windows", "unverified_windows",
                                           "outside_history_windows"))
        status.update(state='paused' if code == 0 and (status['paused_by_user'] or status['paused_sources']) else
                      "completed_with_gaps" if code == 0 and gaps else
                      'completed_with_review' if code == 0 and status['review_issues'] else
                      'completed_with_pending_history' if code == 0 and status['pending_issues'] else
                      "completed" if code == 0 else "failed", pid=None,
                      finished_at=datetime.now().isoformat(timespec="seconds"),
                      message=output.getvalue().strip()[-2000:] or
                      ("Import completed." if code == 0 else "Import failed; check diagnostic logs."))
        log.log(20 if code == 0 else 40, "History import finished; state=%s code=%s details=%s",
                status["state"], code, status["message"])
        try:
            write_status(path, status)
        except OSError:
            log.exception("Could not record final history import result")
            return 1
        try:
            pause_request_path(path).unlink(missing_ok=True)
        except OSError:
            log.exception('Could not remove history pause request')
    return code


if __name__ == "__main__":
    from app.updates.update_handoff import source_worker_entry
    raise SystemExit(source_worker_entry(main))
