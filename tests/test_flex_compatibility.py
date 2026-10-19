"""Synthetic IBKR XML shapes and atomic replay checks; no live requests."""

import pytest

from app.flex.analytics_flex import parse_analytics_flex
from app.flex.cash_flex import run_cash_flex_sync
from app.flex.flex_import import FlexValidationError
from app.platform.history_status import read_status
from app.storage.database import Database
from app.sync.sync_service import _validate_report_window
from scripts import history_worker, import_history


def analytics_day(day, previous, *, prior="100", current="100", extra=""):
    return (f'<FlexStatement accountId="SYNTHETIC" fromDate="{day}" toDate="{day}">'
            '<AccountInformation accountId="SYNTHETIC" currency="USD"/>'
            '<EquitySummaryInBase>'
            f'<EquitySummaryByReportDateInBase reportDate="{previous}" total="{prior}" currency="USD"/>'
            f'<EquitySummaryByReportDateInBase reportDate="{day}" total="{current}" currency="USD"/>'
            '</EquitySummaryInBase>' + (extra or '<CashTransactions/><Transfers/>') + '</FlexStatement>')


def analytics_report(*statements):
    return ('<FlexQueryResponse><FlexStatements>' + ''.join(statements) +
            '</FlexStatements></FlexQueryResponse>').encode()










def test_weekend_edges_require_adjacent_reported_nav_and_never_fill_values():
    monday = analytics_report(analytics_day("20260112", "20260109"))
    assert set(parse_analytics_flex(monday, "SYNTHETIC", "2026-01-10", "2026-01-12")[0]) == {"2026-01-12"}
    friday = analytics_report(analytics_day("20260109", "20260108"))
    assert set(parse_analytics_flex(friday, "SYNTHETIC", "2026-01-09", "2026-01-11")[0]) == {"2026-01-09"}
    for xml, first, last in ((monday, "2026-01-09", "2026-01-12"),
                             (friday, "2026-01-09", "2026-01-12")):
        with pytest.raises(FlexValidationError, match="cover the requested dates"):
            parse_analytics_flex(xml, "SYNTHETIC", first, last)


@pytest.mark.parametrize("before,after", [
    (b'currency="USD"', b'currency="HKD"'),
    (b'accountId="SYNTHETIC" currency="USD"', b'accountId="OTHER" currency="USD"'),
    (b'total="100" currency="USD"', b'total="100" currency="HKD"'),
])
def test_analytics_rejects_conflicting_base_or_child_evidence(before, after):
    xml = analytics_report(analytics_day("20260112", "20260109")).replace(before, after)
    with pytest.raises(FlexValidationError):
        parse_analytics_flex(xml, "SYNTHETIC", "2026-01-12", "2026-01-12")


@pytest.mark.parametrize("extra", [
    '<CashTransactions><CashTransaction reportDate="20260112" type="Deposit" amount="1"/></CashTransactions><Transfers/>',
    '<CashTransactions/><Transfers><Transfer reportDate="20260112" assetCategory="CASH" direction="IN" cashTransfer="1"/></Transfers>',
    '<CashTransactions><CashTransaction reportDate="20260112" type="Deposit" currency="EUR" amount="1"/></CashTransactions><Transfers/>',
    '<CashTransactions><CashTransaction accountId="OTHER" reportDate="20260112" type="Deposit" currency="USD" amount="1"/></CashTransactions><Transfers/>',
    '<CashTransactions><CashTransaction reportDate="20260113" type="Deposit" currency="USD" amount="1"/></CashTransactions><Transfers/>',
])
def test_analytics_never_guesses_cash_currency_rate_account_or_date(extra):
    with pytest.raises(FlexValidationError):
        parse_analytics_flex(analytics_report(analytics_day("20260112", "20260109", extra=extra)),
                             "SYNTHETIC", "2026-01-12", "2026-01-12")


def cash_report(usd="2", aud="3", *, include_aud=True):
    codes = [("USD", usd)] + ([("AUD", aud)] if include_aud else [])
    bridges = ''.join(f'<CashReportCurrency currency="{code}" startingCash="0" endingCash="{amount}" dividends="{amount}"/>'
                      for code, amount in codes)
    rows = ''.join(f'<CashTransaction currency="{code}" reportDate="20260112" type="Dividends" amount="{amount}"/>'
                   for code, amount in codes)
    return (f'<FlexStatement accountId="SYNTHETIC" fromDate="20260112" toDate="20260112">'
            f'<CashReport>{bridges}</CashReport><CashTransactions>{rows}</CashTransactions></FlexStatement>').encode()


def sync_cash(db, xml):
    return run_cash_flex_sync(db, "SYNTHETIC", "query", token="synthetic", pacing_seconds=0,
                             windows_override=[("2026-01-12", "2026-01-12")],
                             fetcher=lambda *a, **k: xml)




def test_multicurrency_replacement_rolls_back_if_transaction_is_invalid(tmp_path):
    db = Database(tmp_path / "rollback.sqlite3")
    try:
        sync_cash(db, cash_report())
        before = [tuple(row) for row in db.conn.execute("SELECT * FROM cash_movements ORDER BY currency,source_key")]
        windows = [tuple(row) for row in db.conn.execute("SELECT * FROM broker_sync_windows")]
        invalid = cash_report(usd="4", aud="5").replace(b'type="Dividends"', b'type="Unknown Type"')
        with pytest.raises(FlexValidationError, match="Unsupported Cash Flex transaction type"):
            sync_cash(db, invalid)
        assert before == [tuple(row) for row in db.conn.execute("SELECT * FROM cash_movements ORDER BY currency,source_key")]
        assert windows == [tuple(row) for row in db.conn.execute("SELECT * FROM broker_sync_windows")]
    finally:
        db.conn.close()






def trades_report(detail="EXECUTIONS"):
    return (f'<FlexStatement accountId="SYNTHETIC" fromDate="2026-01-12" toDate="20260112">'
            '<AccountInformation accountId="SYNTHETIC" currency="USD"/>'
            f'<Trades><Trade levelOfDetailCode="{detail}" tradeID="T1" tradeDate="2026-01-12"/></Trades></FlexStatement>').encode()




@pytest.mark.parametrize("xml", [
    trades_report("SUMMARY"), trades_report("CLOSED_LOT"), trades_report(""),
    trades_report().replace(b'<Trades>', b'<Missing>').replace(b'</Trades>', b'</Missing>'),
    trades_report().replace(b'2026-01-12"/>', b'2026-99-12"/>'),
    trades_report().replace(b'currency="USD"', b'currency="HKD"'),
])
def test_trades_rejects_missing_or_wrong_detail_invalid_dates_and_non_usd(xml):
    with pytest.raises(FlexValidationError):
        _validate_report_window(xml, "SYNTHETIC", "2026-01-12", "2026-01-12")




def test_execution_outside_trades_section_is_rejected():
    xml = trades_report().replace(b'</Trades>', b'</Trades><Trade levelOfDetail="EXECUTION" tradeDate="20260112"/>')
    with pytest.raises(FlexValidationError, match="execution count mismatch"):
        _validate_report_window(xml, "SYNTHETIC", "2026-01-12", "2026-01-12")


@pytest.mark.parametrize("unexpected", [False, True])
def test_worker_validation_failure_keeps_source_window_and_remaining_requests(tmp_path, monkeypatch, unexpected):
    path = tmp_path / "history.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "SYNTHETIC")
    db.set_setting("flex_query_id", "query")
    db.conn.close()
    secret = "synthetic-secret"

    def fail(_db, _account, _query, *, windows_override, on_window_start, **kwargs):
        on_window_start(*windows_override[0])
        if unexpected:
            raise RuntimeError(f"https://example.invalid/?t={secret}")
        _validate_report_window(trades_report("SUMMARY"), "SYNTHETIC", "2026-01-12", "2026-01-12")

    monkeypatch.setitem(import_history.RUNNERS, "trades", fail)
    status_path = tmp_path / "status.json"
    assert history_worker.main(["--status-file", str(status_path), "--source", "trades",
                                "--from-date", "2026-01-01", "--to-date", "2026-01-28",
                                "--database", str(path)]) == 1
    status = read_status(status_path)
    assert status["state"] == "failed" and status["active_source"] == "trades"
    assert status["failed_window"] == ["2026-01-01", "2026-01-28"]
    assert status["unverified_windows"] == {}
    assert secret not in status_path.read_text() and 'https://' not in status_path.read_text()
    if not unexpected:
        assert "Executions detail" in status["message"]
