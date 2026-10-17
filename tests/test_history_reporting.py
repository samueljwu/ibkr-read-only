"""Synthetic checks for safe errors, request spacing, and history boundaries."""

from datetime import date, timedelta
from functools import partial

import pytest

from app.flex.analytics_flex import run_analytics_flex_sync
from app.flex.cash_flex import run_cash_flex_sync
from app.flex.flex_import import FlexServiceError
from app.platform import history_status
from app.platform.history_status import (format_history_details, format_imported_periods,
                                         pause_request_path, read_status, write_status)
from app.storage.database import Database
from app.sync.sync_service import run_flex_sync
from scripts import history_worker, import_history


FAKE_TOKEN = "9876543210123456"
FAKE_URL = f"https://example.invalid/flex?t={FAKE_TOKEN}"


def test_status_write_retries_transient_windows_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "history-import-synthetic.json"
    replace = history_status.os.replace
    calls = []

    def transient_replace(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("Synthetic sharing violation")
        replace(source, target)

    monkeypatch.setattr(history_status.os, "replace", transient_replace)
    monkeypatch.setattr(history_status.time, "sleep", lambda _seconds: None)
    write_status(path, {"state": "completed"})
    assert len(calls) == 2
    assert read_status(path)["state"] == "completed"


def test_imported_periods_merge_adjacent_windows_but_keep_gaps():
    periods = {"trades": [["2022-01-15", "2022-01-31"],
                           ["2022-01-01", "2022-01-14"],
                           ["2022-03-01", "2022-03-31"]]}
    assert format_imported_periods(periods) == (
        "Imported periods (all runs):\n"
        "Trades: 2022-01-01 to 2022-01-31, 2022-03-01 to 2022-03-31\n"
        "Analytics: none\nCash: none")


def configured_database(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "SYNTHETIC")
    for key in import_history.QUERY_KEYS.values():
        db.set_setting(key, "synthetic-query")
    db.conn.close()
    return path


def test_completed_flex_windows_include_prior_runs_for_selected_account_only(tmp_path):
    db = Database(configured_database(tmp_path))
    try:
        with db.conn:
            db.conn.executemany("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status) VALUES (?,?,?,?,?)""", [
                ("SYNTHETIC", "flex", "2022-01-01", "2022-01-31", "complete"),
                ("SYNTHETIC", "flex_cash", "2022-02-01", "2022-02-28", "complete"),
                ("SYNTHETIC", "flex_analytics", "2022-03-01", "2022-03-31", "review"),
                ("OTHER", "flex", "2021-01-01", "2021-01-31", "complete"),
            ])
        assert db.completed_flex_windows("SYNTHETIC") == {
            "trades": [("2022-01-01", "2022-01-31")],
            "analytics": [], "cash": [("2022-02-01", "2022-02-28")],
        }
    finally:
        db.conn.close()


def arguments(path, first="2026-01-01", last="2026-01-14", source="trades"):
    return ["--source", source, "--from-date", first, "--to-date", last,
            "--database", str(path)]


@pytest.mark.parametrize("code,stage", [(1012, "SendRequest"), (1025, "GetStatement"),
                                       (int(FAKE_TOKEN), FAKE_URL)])
def test_flex_errors_keep_safe_context_without_echoed_credentials(tmp_path, monkeypatch, capsys, code, stage):
    path = configured_database(tmp_path)
    status_file = tmp_path / "history-import-synthetic.json"

    def fail(_db, _account, _query, *, windows_override, on_window_start, **_kwargs):
        on_window_start(*windows_override[0])
        raise FlexServiceError(code, f"Echoed credential {FAKE_TOKEN}; request {FAKE_URL}", stage)

    monkeypatch.setitem(import_history.RUNNERS, "trades", fail)
    args = arguments(path)
    assert import_history.main(args) == 1
    output = capsys.readouterr().err
    assert FAKE_TOKEN not in output and FAKE_URL not in output
    assert "https://" not in output
    assert history_worker.main(["--status-file", str(status_file), *args]) == 1
    saved = status_file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in saved and FAKE_URL not in saved
    assert "https://" not in saved
    log = (tmp_path / "logs" / "history-import.log").read_text(encoding="utf-8")
    assert FAKE_TOKEN not in log and FAKE_URL not in log
    assert "https://" not in log
    assert "Request failed: source=trades" in log
    assert "stage=" in log
    status = read_status(status_file)
    assert status["state"] == "failed"
    assert status["active_source"] == "trades"
    assert status["failed_window"] == ["2026-01-01", "2026-01-14"]
    if code in (1012, 1025):
        assert str(code) in status["message"] and stage in status["message"]


@pytest.mark.parametrize("source,runner", [("trades", run_flex_sync),
                                           ("analytics", run_analytics_flex_sync),
                                           ("cash", run_cash_flex_sync)])
def test_unavailable_window_logs_code_and_stage_without_server_text(tmp_path, monkeypatch,
                                                                     source, runner):
    path = configured_database(tmp_path)

    def unavailable(_query, _token, *, from_day, to_day):
        raise FlexServiceError(1003, f"Untrusted {FAKE_TOKEN} {FAKE_URL}", "SendRequest")

    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    monkeypatch.setitem(import_history.RUNNERS, source,
                        partial(runner, token="synthetic", fetcher=unavailable))
    assert import_history.main(arguments(path, source=source)) == 0
    log = (tmp_path / "logs" / "history-import.log").read_text(encoding="utf-8")
    assert (f"Request unavailable: source={source} window=2026-01-01 to 2026-01-14 "
            "code=1003 stage=SendRequest") in log
    assert FAKE_TOKEN not in log and FAKE_URL not in log and "https://" not in log


def test_weekend_end_uses_friday_without_claiming_weekend(tmp_path, monkeypatch):
    path = configured_database(tmp_path)
    status_file = tmp_path / "history-import-synthetic.json"
    calls = []

    def runner(_db, _account, _query, *, windows_override, on_window_start,
               on_window_complete, on_window_result, **_kwargs):
        first, last = windows_override[0]
        calls.append((first, last))
        on_window_start(first, last)
        on_window_result(first, last, "updated")
        on_window_complete(first, last)

    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    for source in import_history.SOURCES:
        monkeypatch.setitem(import_history.RUNNERS, source, runner)
    assert history_worker.main(["--status-file", str(status_file),
                                *arguments(path, "2022-04-01", "2022-04-30", "all")]) == 0
    status = read_status(status_file)
    assert status["state"] == "completed_with_gaps"
    assert calls == [("2022-04-01", "2022-04-29")] * 3
    for source in import_history.SOURCES:
        assert status["completed_windows"][source] == [["2022-04-01", "2022-04-29"]]
        assert status["weekend_windows"][source] == [["2022-04-30", "2022-04-30"]]
    assert not status["unavailable_windows"]
    assert "Weekend end not requested: 2022-04-30 to 2022-04-30" in format_history_details(status)


def test_weekend_end_still_reports_friday_unavailable(tmp_path, monkeypatch):
    path = configured_database(tmp_path)
    calls = []

    def runner(_db, _account, _query, *, windows_override, on_window_start,
               on_window_unavailable, on_window_unavailable_detail, **_kwargs):
        first, last = windows_override[0]
        calls.append((first, last))
        on_window_start(first, last)
        on_window_unavailable_detail(first, last, 1003, "GetStatement")
        on_window_unavailable(first, last)

    monkeypatch.setitem(import_history.RUNNERS, "trades", runner)
    assert import_history.main(arguments(path, "2022-04-01", "2022-04-30", "trades")) == 0
    assert calls == [("2022-04-01", "2022-04-29")]


def test_weekend_only_window_is_skipped_and_shown_as_gap(tmp_path, monkeypatch):
    path = configured_database(tmp_path)
    status_file = tmp_path / "history-import-synthetic.json"
    calls = []
    monkeypatch.setitem(import_history.RUNNERS, "trades", lambda *_a, **_k: calls.append(1))
    assert history_worker.main(["--status-file", str(status_file),
                                *arguments(path, "2022-04-30", "2022-05-01")]) == 0
    status = read_status(status_file)
    assert not calls
    assert status["state"] == "completed_with_gaps"
    assert status["weekend_windows"]["trades"] == [["2022-04-30", "2022-05-01"]]


def test_resume_keeps_weekend_gap_without_reimporting_friday(tmp_path, monkeypatch):
    path = configured_database(tmp_path)
    status_file = tmp_path / "history-import-synthetic.json"
    first, last = "2023-01-01", "2023-01-28"
    write_status(status_file, {"state": "starting", "source": "trades",
                               "from_date": first, "to_date": last})
    calls = []

    def runner(_db, _account, _query, *, windows_override, on_window_start,
               on_window_complete, **_kwargs):
        span = windows_override[0]
        calls.append(span)
        on_window_start(*span)
        on_window_complete(*span)
        if len(calls) == 1:
            pause_request_path(status_file).touch()

    monkeypatch.setattr(import_history, "WINDOW_DAYS", 14)
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    monkeypatch.setitem(import_history.RUNNERS, "trades", runner)
    args = ["--status-file", str(status_file), *arguments(path, first, last)]
    assert history_worker.main(args) == 0
    assert read_status(status_file)["state"] == "paused"
    assert history_worker.main(args) == 0
    status = read_status(status_file)
    assert calls == [("2023-01-15", "2023-01-27"), ("2023-01-01", "2023-01-13")]
    assert status["state"] == "completed_with_gaps"
    assert status["weekend_windows"]["trades"] == [
        ["2023-01-28", "2023-01-28"], ["2023-01-14", "2023-01-14"]]


@pytest.mark.parametrize("outcome", ["exception", "configuration", "success"])
def test_worker_never_persists_captured_output_or_exception_text(tmp_path, monkeypatch, outcome):
    status_file = tmp_path / "history-import-synthetic.json"

    def fake_import(_args, progress, **_kwargs):
        progress("source_start", "cash")
        progress("window_start", "cash", "2026-01-01", "2026-01-14")
        print(f"Untrusted output {FAKE_TOKEN} {FAKE_URL}")
        if outcome == "exception":
            raise RuntimeError(f"Unexpected failure {FAKE_TOKEN} {FAKE_URL}")
        if outcome == "configuration":
            raise SystemExit(2)
        return 0

    monkeypatch.setattr(history_worker, "import_history", fake_import)
    expected_code = {"exception": 1, "configuration": 2, "success": 0}[outcome]
    assert history_worker.main(["--status-file", str(status_file)]) == expected_code
    saved = status_file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in saved and "https://" not in saved
    status = read_status(status_file)
    assert status["active_source"] == "cash"
    assert status["current_window"] == ["2026-01-01", "2026-01-14"]
    if outcome == "configuration":
        assert "database path" in status["message"]
        assert "cash period overlap" not in status["message"]


def test_import_spaces_windows_across_source_boundaries(tmp_path, monkeypatch):
    path = configured_database(tmp_path)
    clock = [0.0]
    calls = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def runner(_db, _account, _query, *, windows_override, on_window_start,
               on_window_complete, **_kwargs):
        calls.append((clock[0], windows_override[0]))
        on_window_start(*windows_override[0])
        clock[0] += 2.0
        on_window_complete(*windows_override[0])

    monkeypatch.setattr(import_history.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(import_history.time, "sleep", sleep)
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 15)
    for source in import_history.SOURCES:
        monkeypatch.setitem(import_history.RUNNERS, source, runner)
    assert import_history.main(arguments(path, last="2026-01-28", source="all")) == 0
    assert [stamp for stamp, _span in calls] == [0, 17, 34]
    assert sleeps == [15] * 2


@pytest.mark.parametrize("failure", ["service", "validation", "unexpected", "configuration"])
def test_later_hard_failure_takes_precedence_over_prior_review(tmp_path, monkeypatch, failure):
    path = tmp_path / "history-import-prior-review.json"
    def fake_import(_args, progress, **_kwargs):
        progress("source_start", "trades")
        progress("window_start", "trades", "2026-01-15", "2026-01-28")
        progress("window_needs_review", "trades", "2026-01-15", "2026-01-28")
        progress("window_complete", "trades", "2026-01-15", "2026-01-28")
        progress("source_start", "cash")
        progress("window_start", "cash", "2026-01-01", "2026-01-14")
        if failure == "service":
            progress("source_failed", "cash", 1012, "SendRequest")
        elif failure == "validation":
            progress("source_validation_failed", "cash", "Synthetic invalid Cash transaction")
        elif failure == "unexpected":
            raise RuntimeError(FAKE_TOKEN + FAKE_URL)
        else:
            raise SystemExit(2)
        return 1
    monkeypatch.setattr(history_worker, "import_history", fake_import)
    assert history_worker.main(["--status-file", str(path)]) == (2 if failure == "configuration" else 1)
    status = read_status(path)
    assert status["state"] == "failed"
    assert status["review_windows"]["trades"] == [["2026-01-15", "2026-01-28"]]
    assert status["active_source"] == "cash"
    saved = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in saved and FAKE_URL not in saved


@pytest.mark.parametrize("source", ["trades", "cash"])
def test_window_crossing_history_floor_keeps_every_available_day(tmp_path, monkeypatch, source):
    path = configured_database(tmp_path)
    floor = import_history.oldest_flex_date()
    first = (floor - timedelta(days=7)).isoformat()
    last = (floor + timedelta(days=6)).isoformat()
    requested = []
    events = []

    def runner(_db, _account, _query, *, windows_override, on_window_start,
               on_window_complete, **_kwargs):
        requested.extend(windows_override)
        on_window_start(*windows_override[0])
        on_window_complete(*windows_override[0])

    if source == "cash":
        # Repeating the exact clamped period must pass cash overlap validation.
        db = Database(path)
        with db.conn:
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status)
                VALUES ('SYNTHETIC','flex_cash',?,?,'complete')""",
                (floor.isoformat(), last))
        db.conn.close()
    monkeypatch.setitem(import_history.RUNNERS, source, runner)
    assert import_history.main(arguments(path, first, last, source),
                               progress=lambda *event: events.append(event)) == 0
    assert requested == [(floor.isoformat(), last)]
    outside = next(event for event in events if event[0] == "windows_outside_history")
    assert outside[2] == [[first, (floor - timedelta(days=1)).isoformat()]]
