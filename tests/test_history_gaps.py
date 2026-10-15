"""Synthetic Flex service scenarios; no live account or credentials are used."""

from functools import partial
from io import BytesIO
from datetime import date, timedelta

import pytest

from app.storage.database import Database
from app.flex import flex_import
from app.flex.analytics_flex import run_analytics_flex_sync
from app.flex.cash_flex import cash_import_windows, run_cash_flex_sync
from app.flex.flex_import import FlexRequestPacer, FlexServiceError
from app.platform.history_status import (format_history_details, pause_request_path,
                                         read_status, write_status)
from app.sync.sync_service import run_flex_sync
from scripts import history_worker, import_history


def report(first, last):
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="TEST" '
            f'baseCurrency="USD" fromDate="{first.replace("-", "")}" '
            f'toDate="{last.replace("-", "")}"><Trades/></FlexStatement>'
            f'</FlexStatements></FlexQueryResponse>').encode()


def setup(tmp_path, monkeypatch, fetcher):
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    monkeypatch.setattr(import_history, "WINDOW_DAYS", 14)
    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "TEST")
    db.set_setting("flex_query_id", "synthetic-query")
    db.conn.close()
    monkeypatch.setitem(import_history.RUNNERS, "trades", partial(
        run_flex_sync, token="synthetic-token", fetcher=fetcher, pacing_seconds=0))
    status_file = tmp_path / "history-import-status.json"
    write_status(status_file, {"state": "starting", "source": "trades",
                               "from_date": "2023-01-01", "to_date": "2023-02-11"})
    return path, status_file


def run(path, status_file, first="2023-01-01", last="2023-02-11"):
    return history_worker.main(["--status-file", str(status_file), "--source", "trades",
                                "--from-date", first, "--to-date", last,
                                "--database", str(path)])


def unavailable():
    return FlexServiceError(1003, "Statement is not available.", "SendRequest")


def test_import_uses_365_day_windows_for_every_source(tmp_path, monkeypatch):
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    last = date.today() - timedelta(days=1)
    last -= timedelta(days=(last.weekday() - 1) % 7)  # Tuesday keeps both window ends on weekdays.
    first = last - timedelta(days=369)
    boundary = first + timedelta(days=364)
    expected = [(first.isoformat(), boundary.isoformat()),
                ((boundary + timedelta(days=1)).isoformat(), last.isoformat())]
    assert import_history.windows(first, last) == expected

    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", "TEST")
    for key in import_history.QUERY_KEYS.values():
        db.set_setting(key, "synthetic-query")
    db.conn.close()

    requested = {source: [] for source in import_history.SOURCES}
    for source in import_history.SOURCES:
        def runner(*_args, windows_override, source=source, **_kwargs):
            requested[source].extend(windows_override)
        monkeypatch.setitem(import_history.RUNNERS, source, runner)
    assert import_history.main(["--source", "all", "--from-date", first.isoformat(),
                                "--to-date", last.isoformat(), "--database", str(db.path)]) == 0
    for spans in requested.values():
        assert spans == list(reversed(expected))


def test_cash_gap_windows_respect_365_day_limit(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    try:
        spans = cash_import_windows(db, "TEST", "2024-01-01", "2025-01-01")
        assert spans == [("2024-01-01", "2024-12-30"),
                         ("2024-12-31", "2025-01-01")]
    finally:
        db.conn.close()


def test_cash_import_combines_five_months_of_saved_two_week_periods(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    try:
        cursor = date(2026, 1, 1)
        through = date(2026, 6, 3)
        with db.conn:
            while cursor <= through:
                end = min(cursor + timedelta(days=13), through)
                db.conn.execute("""INSERT INTO broker_sync_windows
                    (account_id,source,from_day,to_day,status)
                    VALUES ('TEST','flex_cash',?,?,'complete')""",
                    (cursor.isoformat(), end.isoformat()))
                cursor = end + timedelta(days=1)
        assert cash_import_windows(db, "TEST", "2026-01-01", "2026-06-03") == [
            ("2026-01-01", "2026-06-03")]
    finally:
        db.conn.close()


def test_cash_import_splits_saved_periods_without_exceeding_365_days(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    try:
        cursor = date(2025, 1, 1)
        with db.conn:
            for _ in range(30):
                end = cursor + timedelta(days=13)
                db.conn.execute("""INSERT INTO broker_sync_windows
                    (account_id,source,from_day,to_day,status)
                    VALUES ('TEST','flex_cash',?,?,'complete')""",
                    (cursor.isoformat(), end.isoformat()))
                cursor = end + timedelta(days=1)
        assert cash_import_windows(db, "TEST", "2025-01-01",
                                   (cursor - timedelta(days=1)).isoformat()) == [
            ("2025-01-01", "2025-12-30"), ("2025-12-31", "2026-02-24")]
    finally:
        db.conn.close()


def test_pause_finishes_current_window_and_resume_skips_it(tmp_path, monkeypatch):
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    last = date.today() - timedelta(days=1)
    last -= timedelta(days=(last.weekday() - 1) % 7)
    first = last - timedelta(days=369)
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", "TEST")
    db.set_setting("flex_query_id", "synthetic-query")
    db.conn.close()
    status_file = tmp_path / "history-import-status.json"
    write_status(status_file, {"state": "starting", "source": "trades",
                               "from_date": first.isoformat(), "to_date": last.isoformat()})
    calls = []

    def runner(*_args, windows_override, on_window_start, on_window_complete, **_kwargs):
        span = windows_override[0]
        calls.append(span)
        on_window_start(*span)
        on_window_complete(*span)
        if len(calls) == 1:
            pause_request_path(status_file).touch()

    monkeypatch.setitem(import_history.RUNNERS, "trades", runner)
    args = ["--status-file", str(status_file), "--source", "trades",
            "--from-date", first.isoformat(), "--to-date", last.isoformat(),
            "--database", str(db.path)]
    assert history_worker.main(args) == 0
    paused = read_status(status_file)
    assert paused["state"] == "paused"
    assert paused["completed_windows"]["trades"] == [list(calls[0])]
    assert paused["unverified_windows"]["trades"] == [[first.isoformat(),
        (first + timedelta(days=364)).isoformat()]]
    assert "Import paused" in format_history_details(paused)
    assert not pause_request_path(status_file).exists()

    assert history_worker.main(args) == 0
    finished = read_status(status_file)
    assert finished["state"] == "completed"
    assert calls == list(reversed(import_history.windows(first, last)))
    assert len(finished["completed_windows"]["trades"]) == 2
    assert not finished["unverified_windows"]


def test_recent_history_is_saved_before_one_unavailable_request(tmp_path, monkeypatch):
    requests = []

    def fetcher(_query, _token, *, from_day, to_day):
        requests.append((from_day, to_day))
        if from_day < "2023-01-20":
            raise unavailable()
        return report(from_day, to_day)

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    assert run(path, status_file) == 0
    status = read_status(status_file)
    details = format_history_details(status)
    assert status["state"] == "completed_with_gaps"
    assert requests == [("2023-01-29", "2023-02-10"), ("2023-01-15", "2023-01-27")]
    assert status["completed_windows"]["trades"] == [["2023-01-29", "2023-02-10"]]
    assert status["unavailable_windows"]["trades"] == [["2023-01-15", "2023-01-27"]]
    assert status["weekend_windows"]["trades"] == [
        ["2023-02-11", "2023-02-11"], ["2023-01-28", "2023-01-28"]]
    assert status["unverified_windows"]["trades"] == [["2023-01-01", "2023-01-14"]]
    assert "2023-01-01 to 2023-01-14" in details
    assert "Import completed with gaps." in details
    assert "IBKR did not specify the cause" in details
    db = Database(path)
    assert db.conn.execute("SELECT MIN(from_day) FROM broker_sync_windows WHERE status='complete'").fetchone()[0] == "2023-01-29"
    db.conn.close()


def test_interior_gap_preserves_later_windows_and_repeat(tmp_path, monkeypatch):
    def fetcher(_query, _token, *, from_day, to_day):
        if from_day <= "2023-01-22" <= to_day:
            raise unavailable()
        return report(from_day, to_day)

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    assert run(path, status_file) == 0
    status = read_status(status_file)
    assert status["state"] == "completed_with_gaps"
    assert ["2023-01-29", "2023-02-10"] in status["completed_windows"]["trades"]
    db = Database(path)
    before = db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE status='complete'").fetchone()[0]
    db.conn.close()
    assert run(path, status_file) == 0
    db = Database(path)
    after = db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE status='complete'").fetchone()[0]
    assert after == before
    db.conn.close()


def test_every_window_unavailable_stops_after_first_request(tmp_path, monkeypatch):
    calls = []

    def fetcher(*_args, **_kwargs):
        calls.append(1)
        raise unavailable()

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    assert run(path, status_file) == 0
    status = read_status(status_file)
    assert status["state"] == "completed_with_gaps"
    assert not status["completed_windows"].get("trades")
    assert status["unavailable_windows"]["trades"] == [["2023-01-29", "2023-02-10"]]
    assert status["weekend_windows"]["trades"] == [["2023-02-11", "2023-02-11"]]
    assert status["unverified_windows"]["trades"] == [
        ["2023-01-15", "2023-01-28"], ["2023-01-01", "2023-01-14"]]
    assert len(calls) == 1


def test_credential_failure_stops_without_more_requests(tmp_path, monkeypatch):
    def fetcher(_query, _token, *, from_day, to_day):
        if from_day == "2023-01-01":
            raise unavailable()
        raise FlexServiceError(1012, "Token has expired.", "SendRequest")

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    assert run(path, status_file) == 1
    status = read_status(status_file)
    assert status["state"] == "failed"
    assert not status["unavailable_windows"].get("trades")
    assert status["failed_window"] == ["2023-01-29", "2023-02-10"]
    assert "1012" in status["message"] and "SendRequest" in status["message"]


def test_1025_stops_immediately_and_explains_next_step(tmp_path, monkeypatch):
    calls = []

    def fetcher(*_args, **_kwargs):
        calls.append(1)
        raise FlexServiceError(1025, "Too many failed attempts. Please review your configuration.",
                               "SendRequest")

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    assert run(path, status_file) == 1
    status = read_status(status_file)
    assert len(calls) == 1
    assert status["failed_window"] == ["2023-01-29", "2023-02-10"]
    assert "wait for IBKR access to recover" in status["message"]
    assert status["unverified_windows"].get("trades")


def test_dates_outside_standard_flex_history_are_not_requested(tmp_path, monkeypatch):
    year = date.today().year - 5
    first, last = f"{year}-01-01", f"{year}-12-31"
    calls = []
    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "TEST")
    db.set_setting("flex_query_id", "synthetic-query")
    db.conn.close()
    monkeypatch.setitem(import_history.RUNNERS, "trades", lambda *_a, **_kw: calls.append(1))
    status_file = tmp_path / "history-import-status.json"
    write_status(status_file, {"state": "starting", "source": "trades",
                               "from_date": first, "to_date": last})
    assert run(path, status_file, first, last) == 0
    status = read_status(status_file)
    assert status["state"] == "completed_with_gaps"
    assert status["outside_history_windows"]["trades"] == [[first, last]]
    assert not calls


def test_multiyear_import_stops_before_repeated_unavailable_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    year = date.today().year
    first, last = f"{year - 5}-01-01", (date.today() - timedelta(days=1)).isoformat()
    opening = f"{year - 3}-07-01"
    requested = []
    failures = []

    def runner(_db, _account, query, *, windows_override, on_window_start,
               on_window_complete, on_window_unavailable, **_kwargs):
        for start, end in windows_override:
            requested.append((query, start, end))
            on_window_start(start, end)
            if start < opening:
                failures.append((query, start, end))
                on_window_unavailable(start, end)
            else:
                on_window_complete(start, end)
        return {"windows": 0 if failures else 1}

    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "TEST")
    for key, query in (("flex_query_id", "trades"),
                       ("analytics_flex_query_id", "analytics"),
                       ("cash_flex_query_id", "cash")):
        db.set_setting(key, query)
    db.conn.close()
    for source in ("trades", "analytics", "cash"):
        monkeypatch.setitem(import_history.RUNNERS, source, runner)
    status_file = tmp_path / "history-import-status.json"
    write_status(status_file, {"state": "starting", "source": "all",
                               "from_date": first, "to_date": last})
    assert history_worker.main(["--status-file", str(status_file), "--source", "all",
                                "--from-date", first, "--to-date", last,
                                "--database", str(path)]) == 0
    status = read_status(status_file)
    assert len(failures) == 1
    assert min(start for _query, start, _end in requested) >= f"{year - 4}-01-01"
    assert all(status["completed_windows"].get(source) for source in ("trades", "analytics", "cash"))
    assert all(status["outside_history_windows"].get(source) for source in ("trades", "analytics", "cash"))
    assert status["stopped_after_unavailable"]


def test_all_sources_get_recent_reports_before_older_failure(tmp_path, monkeypatch):
    failures = []

    def fetcher(query, _token, *, from_day, to_day):
        if from_day < "2023-01-20":
            failures.append(query)
            raise unavailable()
        first, last = from_day.replace("-", ""), to_day.replace("-", "")
        if query == "trades":
            return report(from_day, to_day)
        if query == "analytics":
            day = date.fromisoformat(from_day)
            nav_rows = []
            while day <= date.fromisoformat(to_day):
                if day.weekday() < 5:
                    nav_rows.append(f'<EquitySummaryByReportDateInBase reportDate="{day:%Y%m%d}" '
                                    'total="100" cash="100"/>')
                day += timedelta(days=1)
            return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="TEST" '
                    f'baseCurrency="USD" fromDate="{first}" toDate="{last}">'
                    f'<EquitySummaryInBase>{"".join(nav_rows)}</EquitySummaryInBase><CashTransactions/><Transfers/>'
                    f'</FlexStatement></FlexStatements></FlexQueryResponse>').encode()
        return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="TEST" '
                f'fromDate="{first}" toDate="{last}"><CashReport><CashReportCurrency '
                f'accountId="TEST" currency="USD" startingCash="100" endingCash="100"/>'
                f'</CashReport><CashTransactions/></FlexStatement></FlexStatements>'
                f'</FlexQueryResponse>').encode()

    path, status_file = setup(tmp_path, monkeypatch, fetcher)
    db = Database(path)
    for key, query in (("flex_query_id", "trades"),
                       ("analytics_flex_query_id", "analytics"),
                       ("cash_flex_query_id", "cash")):
        db.set_setting(key, query)
    db.conn.close()
    monkeypatch.setitem(import_history.RUNNERS, "analytics", partial(
        run_analytics_flex_sync, token="synthetic-token", fetcher=fetcher, pacing_seconds=0))
    monkeypatch.setitem(import_history.RUNNERS, "cash", partial(
        run_cash_flex_sync, token="synthetic-token", fetcher=fetcher, pacing_seconds=0))
    write_status(status_file, {"state": "starting", "source": "all",
                               "from_date": "2023-01-01", "to_date": "2023-02-11"})
    args = ["--status-file", str(status_file), "--source", "all",
            "--from-date", "2023-01-01", "--to-date", "2023-02-11",
            "--database", str(path)]
    assert history_worker.main(args) == 0
    status = read_status(status_file)
    assert status["state"] == "completed_with_gaps"
    assert all(status["completed_windows"].get(source) for source in ("trades", "analytics", "cash"))
    assert sum(len(spans) for spans in status["unavailable_windows"].values()) == 1
    assert len(failures) == 1
    assert status["unverified_windows"].get("trades")
    assert status["unverified_windows"].get("cash")
    db = Database(path)
    before = db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE source='flex_cash'").fetchone()[0]
    db.conn.close()
    assert history_worker.main(args) == 0
    db = Database(path)
    assert db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE source='flex_cash'").fetchone()[0] == before
    db.conn.close()


def test_temporary_error_recovers_and_credentials_stop():
    calls = []

    def recovering(_query, _token, *, from_day, to_day):
        calls.append((from_day, to_day))
        if len(calls) == 1:
            raise FlexServiceError(1001, "Try again shortly.", "SendRequest")
        return report(from_day, to_day)

    pacer = FlexRequestPacer(0)
    assert pacer.fetch(recovering, "query", "synthetic", "2023-01-01", "2023-01-14")
    assert len(calls) == 2

    def expired(*_args, **_kwargs):
        raise FlexServiceError(1012, "Token has expired.", "SendRequest")

    try:
        pacer.fetch(expired, "query", "synthetic", "2023-01-01", "2023-01-14")
    except FlexServiceError as exc:
        assert exc.code == 1012 and exc.stage == "SendRequest"
    else:
        raise AssertionError("Expired token must stop the import")


def test_service_codes_are_preserved_at_both_flex_stages(monkeypatch):
    monkeypatch.setattr(flex_import.time, "sleep", lambda _seconds: None)
    class Opener:
        def __init__(self, responses):
            self.responses = iter(responses)

        def open(self, *_args, **_kwargs):
            return BytesIO(next(self.responses))

    error = b'<FlexStatementResponse><Status>Fail</Status><ErrorCode>1003</ErrorCode><ErrorMessage>Statement is not available.</ErrorMessage></FlexStatementResponse>'
    monkeypatch.setattr(flex_import, "_opener", Opener([error]))
    with pytest.raises(FlexServiceError) as send:
        flex_import.fetch_flex("query", "synthetic-token")
    assert (send.value.code, send.value.stage) == (1003, "SendRequest")

    success = b'<FlexStatementResponse><Status>Success</Status><ReferenceCode>123</ReferenceCode></FlexStatementResponse>'
    monkeypatch.setattr(flex_import, "_opener", Opener([success, error]))
    with pytest.raises(FlexServiceError) as get:
        flex_import.fetch_flex("query", "synthetic-token")
    assert (get.value.code, get.value.stage) == (1003, "GetStatement")
