"""Synthetic report corrections and semantic replay; no IBKR calls."""

from decimal import Decimal
from functools import partial
from sqlite3.dump import _iterdump
from sqlcipher3 import dbapi2 as sqlite3
import xml.etree.ElementTree as ET

import pytest

from app.flex.analytics_flex import run_analytics_flex_sync
from app.flex.cash_flex import cash_import_windows, parse_cash_flex, run_cash_flex_sync
from app.flex.flex_import import FlexServiceError, FlexValidationError
from app.platform.history_status import format_history_details, read_status
from app.storage.database import Database
from app.sync.sync_service import run_flex_sync
from scripts import history_worker, import_history


ACCOUNT = "SYNTHETIC"
DAY = "2026-01-06"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "synthetic.sqlite3")
    database.set_setting("ib_account_id", ACCOUNT)
    for key in import_history.QUERY_KEYS.values():
        database.set_setting(key, "synthetic-query")
    yield database
    database.conn.close()


def cash_report(first=DAY, last=DAY, opening="100", closing="100", amounts=(), aud=False):
    total = sum(map(Decimal, amounts), Decimal(0))
    sales = Decimal(closing) - Decimal(opening) - total
    extra = '<CashReportCurrency currency="AUD" startingCash="0" endingCash="0"/>' if aud else ""
    movements = "".join(f'<CashTransaction currency="USD" reportDate="{first}" '
        f'type="Deposit" amount="{amount}" description="Synthetic deposit"/>' for amount in amounts)
    if aud:
        movements += f'<CashTransaction currency="AUD" reportDate="{first}" type="Deposit" amount="1"/>'
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="{ACCOUNT}" '
        f'fromDate="{first}" toDate="{last}"><CashReport><CashReportCurrency currency="USD" '
        f'startingCash="{opening}" endingCash="{closing}" netTradesSales="{sales}"/>{extra}'
        f'</CashReport><CashTransactions>{movements}</CashTransactions></FlexStatement>'
        '</FlexStatements></FlexQueryResponse>').encode()


def analytics_report(nav="100", amount="10", empty=False):
    nav_row = "" if empty else f'<EquitySummaryByReportDateInBase reportDate="{DAY}" total="{nav}" cash="50"/>'
    flow = "" if empty else (f'<CashTransaction reportDate="{DAY}" type="Deposit" '
                            f'currency="USD" amount="{amount}"/>')
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="{ACCOUNT}" '
        f'baseCurrency="USD" fromDate="{DAY}" toDate="{DAY}"><EquitySummaryInBase>{nav_row}'
        f'</EquitySummaryInBase><CashTransactions>{flow}</CashTransactions><Transfers/>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>').encode()


def trade_report(price="10", quantity="5", empty=False, execution="EXEC.01"):
    row = "" if empty else (f'<Trade accountId="{ACCOUNT}" levelOfDetail="EXECUTION" '
        f'tradeID="T1" ibExecID="{execution}" tradeDate="{DAY}" tradeTime="101500" '
        f'buySell="BUY" quantity="{quantity}" tradePrice="{price}" conid="123" symbol="SYN" '
        'assetCategory="STK" currency="USD" multiplier="1" fxRateToBase="1" '
        'ibCommission="-0.5" ibCommissionCurrency="USD"/>')
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="{ACCOUNT}" '
        f'baseCurrency="USD" fromDate="{DAY}" toDate="{DAY}"><Trades>{row}</Trades>'
        '</FlexStatement></FlexStatements></FlexQueryResponse>').encode()


def reordered(xml):
    root = ET.fromstring(xml)
    for node in root.iter():
        node.attrib.update(generatedAt="synthetic-generation") if node.tag == "FlexStatement" else None
        attrs = list(node.attrib.items())
        node.attrib.clear()
        node.attrib.update(reversed(attrs))
        node[:] = list(reversed(list(node)))
    return ET.tostring(root)


def sync(runner, db, xml, **kwargs):
    return runner(db, ACCOUNT, "synthetic-query", token="synthetic-token", pacing_seconds=0,
                  windows_override=[(DAY, DAY)], fetcher=lambda *a, **k: xml,
                  compare_existing=True, **kwargs)


@pytest.mark.parametrize("runner,report", [(run_cash_flex_sync, cash_report),
    (run_analytics_flex_sync, analytics_report), (run_flex_sync, trade_report)])
def test_equivalent_reports_preserve_rows_timestamps_and_backups(db, monkeypatch, runner, report):
    sync(runner, db, report())
    before = list(_iterdump(db.conn))
    changes = db.conn.total_changes
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Unchanged report created a backup"))
    # Numeric formatting, attribute/row order, and generation metadata are irrelevant.
    xml = report().replace(b'="100"', b'="100.000"').replace(b'="10"', b'="10.00"')
    outcomes = []
    result = sync(runner, db, reordered(xml), on_window_result=lambda *args: outcomes.append(args))
    assert result["unchanged_windows"] == 1
    assert outcomes == [(DAY, DAY, "unchanged")]
    assert list(_iterdump(db.conn)) == before
    assert db.conn.total_changes == changes


def test_cash_compares_movement_multiset_including_duplicate_multiplicity(db):
    sync(run_cash_flex_sync, db, cash_report(amounts=("1", "1", "2")))
    # Same totals and bridge, different financial rows.
    result = sync(run_cash_flex_sync, db, cash_report(amounts=("1.00", "3")))
    assert result["updated_windows"] == 1
    rows = db.conn.execute("SELECT amount FROM cash_movements").fetchall()
    assert sorted(Decimal(row[0]) for row in rows) == [Decimal(1), Decimal(3)]


def test_cash_selection_rechecks_whole_30_day_period_and_splits_gaps(db, monkeypatch):
    first, last = "2026-01-01", "2026-01-30"
    run_cash_flex_sync(db, ACCOUNT, "synthetic", token="synthetic", pacing_seconds=0,
        windows_override=[(first, last)], fetcher=lambda *a, **k: cash_report(first, last))
    assert cash_import_windows(db, ACCOUNT, "2026-01-10", "2026-01-23") == [(first, last)]
    assert cash_import_windows(db, ACCOUNT, "2026-01-10", "2026-02-20") == [
        (first, last), ("2026-01-31", "2026-02-20")]
    calls, events = [], []
    def runner(*a, windows_override, **kwargs):
        calls.extend(windows_override)
    monkeypatch.setitem(import_history.RUNNERS, "cash", runner)
    assert import_history.main(["--source", "cash", "--from-date", "2026-01-10",
        "--to-date", "2026-01-23", "--database", str(db.path)],
        progress=lambda *args: events.append(args)) == 0
    assert calls == [(first, last)]
    assert ("cash_boundary_periods", "cash", [[first, last]], None) in events


def seed_adjacent(db):
    for day, opening, closing, amount in [("2026-01-05", "100", "110", "1"),
                                         (DAY, "110", "120", "2")]:
        run_cash_flex_sync(db, ACCOUNT, "synthetic", token="synthetic", pacing_seconds=0,
            windows_override=[(day, day)],
            fetcher=lambda *a, **k: cash_report(day, day, opening, closing, amounts=(amount,)))


@pytest.mark.parametrize("failure", [None, "validation", "write"])
def test_adjacent_cash_corrections_stage_and_commit_together_or_roll_back(db, failure):
    seed_adjacent(db)
    if failure == "write":
        with db.conn:
            db.conn.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON cash_movements
                WHEN NEW.reported_day='2026-01-06' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    before = list(_iterdump(db.conn))
    calls = []
    def fetcher(*a, from_day, to_day):
        assert not db.conn.in_transaction
        calls.append((from_day, to_day))
        if from_day == DAY:
            return cash_report(DAY, DAY, "111", "120", amounts=("4",))
        xml = cash_report(from_day, to_day, "100", "111", amounts=("3",))
        return xml.replace(b'type="Deposit"', b'type="Unsupported Category"') if failure == "validation" else xml
    def run():
        return run_cash_flex_sync(db, ACCOUNT, "synthetic", token="synthetic", pacing_seconds=0,
            windows_override=[(DAY, DAY), ("2026-01-05", "2026-01-05")], fetcher=fetcher,
            compare_existing=True)
    if failure:
        with pytest.raises((FlexValidationError, sqlite3.IntegrityError)):
            run()
        assert list(_iterdump(db.conn)) == before
    else:
        assert run()["updated_windows"] == 2
        assert sorted(row[0] for row in db.conn.execute("SELECT amount FROM cash_movements")) == ["3", "4"]
    assert len(calls) == 2


def test_cash_missing_currency_requires_review_without_writes(db, monkeypatch):
    sync(run_cash_flex_sync, db, cash_report(aud=True))
    before = list(_iterdump(db.conn))
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Unverified report created backup"))
    with pytest.raises(FlexValidationError, match="Needs review.*omits an existing currency"):
        sync(run_cash_flex_sync, db, cash_report())
    assert list(_iterdump(db.conn)) == before


def test_cash_correction_does_not_change_analytics_nav(db):
    sync(run_cash_flex_sync, db, cash_report())
    db.save_account_history(1, DAY, 250, source="flex", flow_covered=True)
    sync(run_cash_flex_sync, db, cash_report(amounts=("1",)))
    assert db.analytics_history(1)[0][0]["nav_usd"] == 250


def test_same_revision_trade_correction_updates_stable_row_and_missing_fill_requires_review(db):
    sync(run_flex_sync, db, trade_report())
    row_id = db.conn.execute("SELECT id FROM trades").fetchone()[0]
    result = sync(run_flex_sync, db, trade_report(price="12", quantity="6"))
    assert result["updated"] == 1
    row = db.conn.execute("SELECT id,quantity,price FROM trades").fetchone()
    assert tuple(row) == (row_id, 6, 12)
    before = list(_iterdump(db.conn))
    with pytest.raises(FlexValidationError, match="Needs review.*omits a previously imported execution"):
        sync(run_flex_sync, db, trade_report(empty=True))
    assert list(_iterdump(db.conn)) == before


def test_lower_revision_trade_cannot_replace_correction(db):
    sync(run_flex_sync, db, trade_report(execution="EXEC.02", price="12"))
    result = sync(run_flex_sync, db, trade_report(execution="EXEC.01", price="10"))
    assert result["unchanged_windows"] == 1
    assert db.conn.execute("SELECT price FROM trades").fetchone()[0] == 12


def test_same_revision_contract_only_change_requires_review_and_preserves_trade(db):
    sync(run_flex_sync, db, trade_report())
    tables = ("trades", "positions", "broker_executions", "trade_matches")
    before = {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
              for table in tables}
    outcomes = []
    xml = trade_report().replace(b'conid="123"', b'conid="456"')
    result = sync(run_flex_sync, db, xml, on_window_result=lambda *args: outcomes.append(args))
    assert result["review"] == 1
    assert result["updated"] == result["inserted"] == result["duplicates"] == 0
    assert outcomes == [(DAY, DAY, "needs_review")]
    assert {table: [tuple(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
            for table in tables} == before
    window = db.conn.execute("SELECT status,issue_count FROM broker_sync_windows").fetchone()
    assert tuple(window) == ("review", 1)
    source = db.conn.execute("SELECT status,issue FROM broker_source_records WHERE source='flex'").fetchone()
    assert source["status"] == "review"
    assert "changed contract" in source["issue"]


def test_identical_unsupported_broker_event_keeps_review_without_writes_or_backup(db, monkeypatch):
    root = ET.fromstring(trade_report())
    events = ET.SubElement(root.find('.//FlexStatement'), 'CorporateActions')
    ET.SubElement(events, 'CorporateAction', accountId=ACCOUNT, type='SYNTHETIC',
                  dateTime=DAY, actionDescription='Synthetic unsupported event')
    xml = ET.tostring(root)
    assert sync(run_flex_sync, db, xml)["review"] == 1
    before = list(_iterdump(db.conn))
    changes = db.conn.total_changes
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Unchanged report created backup"))
    outcomes = []
    assert sync(run_flex_sync, db, xml, on_window_result=lambda *args: outcomes.append(args))["review"] == 1
    assert outcomes == [(DAY, DAY, "needs_review")]
    assert list(_iterdump(db.conn)) == before
    assert db.conn.total_changes == changes


def test_pending_execution_replay_keeps_review_without_writes_or_backup(db, monkeypatch):
    xml = trade_report().replace(b'ibCommission="-0.5"', b'')
    assert sync(run_flex_sync, db, xml)["review"] == 1
    before = list(_iterdump(db.conn))
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Unchanged report created backup"))
    assert sync(run_flex_sync, db, xml)["review"] == 1
    assert list(_iterdump(db.conn)) == before
    with pytest.raises(FlexValidationError, match="Needs review.*incomplete execution correction"):
        sync(run_flex_sync, db, xml.replace(b'tradePrice="10"', b'tradePrice="12"'))
    assert list(_iterdump(db.conn)) == before


def test_manual_trade_is_preserved_for_review(db):
    sync(run_flex_sync, db, trade_report())
    with db.conn:
        db.conn.execute("UPDATE trades SET source='manual',price=11")
    result = sync(run_flex_sync, db, trade_report(price="12"))
    assert result["review"] == 1
    assert db.conn.execute("SELECT price FROM trades").fetchone()[0] == 11


def test_analytics_corrects_nav_and_flows_and_preserves_empty_correction(db):
    sync(run_analytics_flex_sync, db, analytics_report())
    db.save_external_flow(1, "history-csv:synthetic", DAY, 20, "cash")
    result = sync(run_analytics_flex_sync, db, analytics_report(nav="120", amount="20"))
    assert result["updated_windows"] == 1
    assert db.conn.execute("SELECT nav_usd FROM account_history").fetchone()[0] == 120
    assert db.conn.execute("SELECT COUNT(*) FROM external_flows").fetchone()[0] == 1
    before = list(_iterdump(db.conn))
    with pytest.raises(FlexValidationError, match="Needs review.*empty or incomplete"):
        sync(run_analytics_flex_sync, db, analytics_report(empty=True))
    assert list(_iterdump(db.conn)) == before


def test_worker_preserves_safe_review_and_outcome_labels(db, tmp_path, monkeypatch):
    def runner(*a, windows_override, on_window_start, on_window_result, on_window_complete, **kwargs):
        first, last = windows_override[0]
        on_window_start(first, last)
        on_window_result(first, last, "unchanged")
        on_window_complete(first, last)
        raise FlexValidationError("Needs review: synthetic omitted execution")
    monkeypatch.setitem(import_history.RUNNERS, "trades", runner)
    path = tmp_path / "history-import-synthetic.json"
    assert history_worker.main(["--status-file", str(path), "--source", "trades",
        "--from-date", DAY, "--to-date", DAY, "--database", str(db.path)]) == 1
    status = read_status(path)
    assert status["state"] == "needs_review"
    # A validation error after a completed window has no current span to classify.
    assert "Unchanged:" in format_history_details(status)
    assert "synthetic omitted execution" in status["message"]


def test_importer_combines_adjacent_existing_cash_periods(db, monkeypatch):
    seed_adjacent(db)
    calls = []
    monkeypatch.setitem(import_history.RUNNERS, "cash",
        lambda *a, windows_override, **kwargs: calls.append(windows_override))
    assert import_history.main(["--source", "cash", "--from-date", "2026-01-05",
        "--to-date", DAY, "--database", str(db.path)]) == 0
    assert calls == [[("2026-01-05", DAY)]]


def test_cash_import_consolidates_old_periods_and_preserves_movements(db, monkeypatch):
    first = "2026-01-05"
    for day, opening, closing, amount in [(first, "100", "105", "5"),
                                          (DAY, "105", "112", "7")]:
        run_cash_flex_sync(db, ACCOUNT, "synthetic", token="synthetic", pacing_seconds=0,
            windows_override=[(day, day)],
            fetcher=lambda *a, day=day, opening=opening, closing=closing, amount=amount,
                           **k: cash_report(day, day, opening, closing, amounts=(amount,)))
    assert db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE source='flex_cash'").fetchone()[0] == 2
    xml = ET.fromstring(cash_report(first, DAY, "100", "112", amounts=("5", "7")))
    transactions = list(xml.iter("CashTransaction"))
    transactions[1].set("reportDate", DAY)
    combined = ET.tostring(xml)
    calls = []

    def fetcher(*a, from_day, to_day):
        calls.append((from_day, to_day))
        return combined

    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    monkeypatch.setitem(import_history.RUNNERS, "cash", partial(run_cash_flex_sync,
        token="synthetic", fetcher=fetcher))
    args = ["--source", "cash", "--from-date", first, "--to-date", DAY,
            "--database", str(db.path)]
    assert import_history.main(args) == 0
    assert calls == [(first, DAY)]
    rows = db.conn.execute("""SELECT from_day,to_day FROM broker_sync_windows
        WHERE source='flex_cash'""").fetchall()
    assert [tuple(row) for row in rows] == [(first, DAY)]
    movements = db.conn.execute("SELECT reported_day,amount FROM cash_movements "
        "ORDER BY reported_day").fetchall()
    assert [tuple(row) for row in movements] == [(first, "5"), (DAY, "7")]
    before = list(_iterdump(db.conn))
    with monkeypatch.context() as patch:
        patch.setattr(db, "backup", lambda: pytest.fail("Unchanged combined Cash report created backup"))
        assert import_history.main(args) == 0
    assert list(_iterdump(db.conn)) == before


def test_cash_import_rejects_unknown_type_without_replacing_old_periods(db, tmp_path, monkeypatch):
    seed_adjacent(db)
    before = list(_iterdump(db.conn))
    invalid = cash_report("2026-01-05", DAY, "100", "120", amounts=("1",))
    invalid = invalid.replace(b'type="Deposit"', b'type="Synthetic Adjustment"')
    monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Invalid Cash report created backup"))
    monkeypatch.setitem(import_history.RUNNERS, "cash", partial(run_cash_flex_sync,
        token="synthetic", fetcher=lambda *a, **k: invalid))
    status_file = tmp_path / "history-import-unsupported-cash.json"
    assert history_worker.main(["--status-file", str(status_file), "--source", "cash",
        "--from-date", "2026-01-05", "--to-date", DAY,
        "--database", str(db.path)]) == 1
    status = read_status(status_file)
    assert status["failed_window"] == ["2026-01-05", DAY]
    assert status["message"] == "Unsupported Cash Flex transaction type: Synthetic Adjustment"
    assert list(_iterdump(db.conn)) == before


def test_cash_unknown_type_error_shows_only_safe_category():
    xml = cash_report(amounts=("1",)).replace(b'type="Deposit"',
        b'type="Synthetic Adjustment"')
    with pytest.raises(FlexValidationError, match="transaction type: Synthetic Adjustment"):
        parse_cash_flex(xml, ACCOUNT, DAY, DAY)
    unsafe = xml.replace(b'Synthetic Adjustment', b'7-SECRET-123456')
    with pytest.raises(FlexValidationError, match="review Cash Transactions types"):
        parse_cash_flex(unsafe, ACCOUNT, DAY, DAY)


def test_combined_cash_period_unavailable_marks_its_full_span(db, monkeypatch):
    seed_adjacent(db)
    events = []
    def runner(*a, windows_override, on_window_start, on_window_unavailable, **kwargs):
        on_window_start(*windows_override[0])
        on_window_unavailable(*windows_override[0])
    monkeypatch.setitem(import_history.RUNNERS, "cash", runner)
    assert import_history.main(["--source", "cash", "--from-date", "2026-01-05",
        "--to-date", DAY, "--database", str(db.path)],
        progress=lambda *args: events.append(args)) == 0
    assert ("window_unavailable", "cash", "2026-01-05", DAY) in events
    assert not any(event == "windows_unverified" for event, *_ in events)


@pytest.mark.parametrize("entry", ["runner", "worker"])
def test_cash_group_unavailable_discards_changed_fetched_prefix(db, tmp_path, monkeypatch, entry):
    seed_adjacent(db)
    before = list(_iterdump(db.conn))
    changes = db.conn.total_changes
    calls = []
    def fetcher(*a, from_day, to_day):
        calls.append((from_day, to_day))
        if from_day == "2026-01-05":
            raise FlexServiceError(1003, "Synthetic unavailable report", "SendRequest")
        # This would independently join the stale prior period and previously committed early.
        return cash_report(DAY, DAY, "110", "121")
    monkeypatch.setattr(Database, "backup", lambda self: pytest.fail("Incomplete Cash group created backup"))
    if entry == "runner":
        completed = []
        result = run_cash_flex_sync(db, ACCOUNT, "synthetic", token="synthetic", pacing_seconds=0,
            windows_override=[(DAY, DAY), ("2026-01-05", "2026-01-05")], fetcher=fetcher,
            compare_existing=True, stop_on_unavailable=True, on_window_unavailable=lambda *a: None,
            on_window_complete=lambda *a: completed.append(a))
        assert result["windows"] == result["updated_windows"] == 0
        assert result["unavailable_windows"] == 1
        assert result["unverified_windows"] == [(DAY, DAY)]
        assert not completed
        assert calls == [(DAY, DAY), ("2026-01-05", "2026-01-05")]
    else:
        monkeypatch.setattr(import_history, "REQUEST_PACING_SECONDS", 0)
        monkeypatch.setitem(import_history.RUNNERS, "cash", partial(run_cash_flex_sync,
            token="synthetic", fetcher=fetcher))
        path = tmp_path / "history-import-unavailable.json"
        assert history_worker.main(["--status-file", str(path), "--source", "cash",
            "--from-date", "2026-01-05", "--to-date", DAY, "--database", str(db.path)]) == 0
        status = read_status(path)
        assert status["state"] == "completed_with_gaps"
        assert status["unavailable_windows"]["cash"] == [["2026-01-05", DAY]]
        assert not status["unverified_windows"].get("cash")
        assert not status["updated_windows"] and not status["completed_windows"]
        assert calls == [("2026-01-05", DAY)]
    assert list(_iterdump(db.conn)) == before
    assert db.conn.total_changes == changes


def test_omitted_source_only_reviewed_execution_requires_review(db, monkeypatch):
    invalid = trade_report().replace(b'conid="123"', b'')
    assert sync(run_flex_sync, db, invalid)["review"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM broker_executions").fetchone()[0] == 0
    before = list(_iterdump(db.conn))
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Omitted execution created backup"))
    with pytest.raises(FlexValidationError, match="Needs review.*omits a previously reviewed execution"):
        sync(run_flex_sync, db, trade_report(empty=True))
    assert list(_iterdump(db.conn)) == before


def test_unchanged_source_only_review_retains_review_without_writes_and_can_be_resolved(db, monkeypatch):
    invalid = trade_report().replace(b'conid="123"', b'')
    assert sync(run_flex_sync, db, invalid)["review"] == 1
    before = list(_iterdump(db.conn))
    changes = db.conn.total_changes
    outcomes = []
    with monkeypatch.context() as patch:
        patch.setattr(db, "backup", lambda: pytest.fail("Unchanged reviewed execution created backup"))
        equivalent = invalid.replace(b'tradePrice="10"', b'tradePrice="10.00"').replace(
            b'tradeDate="2026-01-06"', b'tradeDate="20260106"')
        result = sync(run_flex_sync, db, reordered(equivalent),
                      on_window_result=lambda *args: outcomes.append(args))
    assert result["review"] == result["unchanged_windows"] == 1
    assert outcomes == [(DAY, DAY, "needs_review")]
    assert list(_iterdump(db.conn)) == before
    assert db.conn.total_changes == changes
    resolved = sync(run_flex_sync, db, trade_report())
    assert resolved["inserted"] == 1 and resolved["review"] == 0
    assert sync(run_flex_sync, db, trade_report())["unchanged_windows"] == 1


def test_analytics_empty_report_preserves_existing_flows_without_nav(db, monkeypatch):
    db.save_external_flow(1, "history-csv:synthetic", DAY, 10, "cash")
    before = list(_iterdump(db.conn))
    monkeypatch.setattr(db, "backup", lambda: pytest.fail("Unverified report created backup"))
    with pytest.raises(FlexValidationError, match="Needs review"):
        sync(run_analytics_flex_sync, db, analytics_report(empty=True))
    assert list(_iterdump(db.conn)) == before


def test_unchanged_trade_in_changed_report_keeps_its_timestamps(db):
    first = trade_report()
    second = first.replace(b'T1', b'T2').replace(b'EXEC.01', b'OTHER.01')
    root = ET.fromstring(first)
    root.find('.//Trades').append(ET.fromstring(second).find('.//Trade'))
    sync(run_flex_sync, db, ET.tostring(root))
    with db.conn:
        db.conn.execute("UPDATE trades SET updated_at='synthetic-before' WHERE price=10")
        db.conn.execute("UPDATE broker_source_records SET seen_at='synthetic-before'")
    root.find('.//Trade').set('tradePrice', '12')
    sync(run_flex_sync, db, ET.tostring(root))
    assert db.conn.execute("SELECT updated_at FROM trades WHERE price=10").fetchone()[0] == "synthetic-before"
    assert db.conn.execute("SELECT seen_at FROM broker_source_records WHERE source_id='T2'").fetchone()[0] == "synthetic-before"
