from datetime import date
import xml.etree.ElementTree as ET

import pytest

from app.flex.analytics_flex import parse_analytics_flex, run_analytics_flex_sync
from app.storage.database import Database


def _report(deposit=500, *, account="DU123", include_flows=True):
    cash = (f'<CashTransactions><CashTransaction type="Deposits/Withdrawals" '
            f'reportDate="20260106" currency="USD" amount="{deposit}" '
            f'description="Bank deposit"/></CashTransactions>') if include_flows else ""
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="{account}" '
            'fromDate="20260105" toDate="20260107" baseCurrency="USD">'
            '<EquitySummaryInBase>'
            '<EquitySummaryByReportDateInBase reportDate="20260105" total="1000" cash="200"/>'
            '<EquitySummaryByReportDateInBase reportDate="20260106" total="1600" cash="700"/>'
            '<EquitySummaryByReportDateInBase reportDate="20260107" total="1500" cash="600"/>'
            '</EquitySummaryInBase>' + cash +
            '<Transfers><Transfer reportDate="20260107" assetCategory="STK" '
            'positionAmountInBase="100" direction="IN" transactionID="asset-1"/>'
            '</Transfers></FlexStatement></FlexStatements></FlexQueryResponse>').encode()


def test_flex_nav_and_external_flows_exclude_deposits_and_transfers_from_pnl():
    valuations, flows = parse_analytics_flex(_report(), "DU123", "2026-01-05", "2026-01-07")
    assert len(valuations) == 3
    assert [(flow[1], flow[2]) for flow in flows] == [("2026-01-06", 500), ("2026-01-07", 100)]




def test_flex_analytics_sync_replays_corrections_and_replaces_csv_rows(tmp_path, monkeypatch):
    db = Database(tmp_path / "account.sqlite3")
    db.save_account_history(1, "2026-01-06", 1700, 800, "imported", True)
    db.save_external_flow(1, "history-csv:2026-01-06", "2026-01-06", 700, "net external")
    monkeypatch.setattr("app.flex.analytics_flex.planned_analytics_windows",
                        lambda *_: [("2026-01-05", "2026-01-07")])
    source = {"deposit": 500}

    def fetcher(query_id, token, *, from_day, to_day):
        assert (query_id, token, from_day, to_day) == (
            "analytics-query", "secret", "2026-01-05", "2026-01-07")
        return _report(source["deposit"])

    for amount in (500, 550):
        source["deposit"] = amount
        result = run_analytics_flex_sync(db, "DU123", "analytics-query", today=date(2026, 1, 8),
                                         token="secret", fetcher=fetcher, pacing_seconds=0)
        assert result == {"windows": 1, "valuations": 3}
    snapshots, flows = db.analytics_history(1)
    assert snapshots[1]["source"] == "flex"
    assert snapshots[1]["nav_usd"] == 1600
    assert len(flows) == 2
    assert sum(row["amount_usd"] for row in flows) == 650
    assert db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows WHERE source='flex_analytics'").fetchone()[0] == 1
    db.conn.close()


def test_flex_coverage_supersedes_cash_statement_flows_in_either_import_order(tmp_path, monkeypatch):
    db = Database(tmp_path / "account.sqlite3")
    db.set_setting("ib_account_id", "DU123")
    db.save_external_flow(1, "cash:prior-statement", "2026-01-06", 500, "deposit")
    monkeypatch.setattr("app.flex.analytics_flex.planned_analytics_windows",
                        lambda *_: [("2026-01-05", "2026-01-07")])
    run_analytics_flex_sync(db, "DU123", "analytics-query", today=date(2026, 1, 8),
                            token="secret", fetcher=lambda *_args, **_kwargs: _report(),
                            pacing_seconds=0)
    _, flows = db.analytics_history(1)
    assert [(row["day"], row["amount_usd"]) for row in flows] == [
        ("2026-01-06", 500), ("2026-01-07", 100)]
    # Cash history can be imported later without changing Analytics attribution.
    db.save_external_flow(1, "cash:later-statement", "2026-01-06", 500, "deposit")
    _, flows = db.analytics_history(1)
    assert len(flows) == 2
    assert db.conn.execute("SELECT COUNT(*) FROM external_flows").fetchone()[0] == 4
    db.conn.close()


def test_flex_analytics_rejects_wrong_account_or_missing_flow_section():
    with pytest.raises(ValueError, match="account or date range"):
        parse_analytics_flex(_report(), "DU999", "2026-01-05", "2026-01-07")
    with pytest.raises(ValueError, match="needs NAV"):
        parse_analytics_flex(_report(include_flows=False), "DU123", "2026-01-05", "2026-01-07")




def test_analytics_flex_rejects_partial_coverage_and_unverified_base():
    report = _report()
    with pytest.raises(ValueError, match="cover the requested dates"):
        parse_analytics_flex(report.replace(b'toDate="20260107"', b'toDate="20260106"'),
                             "DU123", "2026-01-05", "2026-01-07")
    with pytest.raises(ValueError, match="USD base"):
        parse_analytics_flex(report.replace(b'baseCurrency="USD"', b'baseCurrency="HKD"'),
                             "DU123", "2026-01-05", "2026-01-07")
    with pytest.raises(ValueError, match="identify USD"):
        parse_analytics_flex(report.replace(b' baseCurrency="USD"', b''),
                             "DU123", "2026-01-05", "2026-01-07")


def _without_nav(*days):
    root = ET.fromstring(_report())
    section = root.find(".//EquitySummaryInBase")
    for node in list(section):
        if not days or node.get("reportDate") in days:
            section.remove(node)
    return ET.tostring(root)


@pytest.mark.parametrize("missing", [("20260105",), ("20260106",), ("20260107",),
                                     ("20260105", "20260106")])
def test_multi_day_statement_requires_weekday_nav_inside_its_declared_bounds(missing):
    with pytest.raises(ValueError, match="include every weekday NAV date"):
        parse_analytics_flex(_without_nav(*missing), "DU123", "2026-01-05", "2026-01-07")


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("prior_source", ["flex", "imported"])
def test_incomplete_or_empty_replay_preserves_existing_nav_flows_and_coverage(tmp_path, empty, prior_source):
    db = Database(tmp_path / "incomplete-analytics.sqlite3")
    try:
        db.save_account_history(1, "2026-01-06", 1700, 800, prior_source, True)
        db.save_external_flow(1, "flex-analytics:DU123:cash:id:prior", "2026-01-06", 700, "cash")
        before = ([tuple(row) for row in db.conn.execute("SELECT * FROM account_history")],
                  [tuple(row) for row in db.conn.execute("SELECT * FROM external_flows")])
        xml = _without_nav() if empty else _without_nav("20260106")
        with pytest.raises(ValueError, match="does not cover the requested dates"):
            run_analytics_flex_sync(db, "DU123", "query", token="synthetic", pacing_seconds=0,
                                    windows_override=[("2026-01-05", "2026-01-07")],
                                    fetcher=lambda *a, **k: xml)
        after = ([tuple(row) for row in db.conn.execute("SELECT * FROM account_history")],
                 [tuple(row) for row in db.conn.execute("SELECT * FROM external_flows")])
        assert after == before
        assert db.conn.execute("SELECT COUNT(*) FROM broker_sync_windows").fetchone()[0] == 0
        assert db.metadata("ibkr_analytics_sync_completed") == ""
    finally:
        db.conn.close()


@pytest.mark.parametrize("older_history", [False, True])
def test_empty_early_window_can_complete_only_without_prior_nav_in_that_window(tmp_path, older_history):
    db = Database(tmp_path / "empty-early-analytics.sqlite3")
    try:
        if older_history:
            db.save_account_history(1, "2026-01-01", 1000)
        result = run_analytics_flex_sync(db, "DU123", "query", token="synthetic", pacing_seconds=0,
                                         windows_override=[("2026-01-05", "2026-01-07")],
                                         fetcher=lambda *a, **k: _without_nav())
        assert result == {"windows": 1, "valuations": 0}
        assert db.conn.execute("SELECT COUNT(*) FROM account_history").fetchone()[0] == int(older_history)
        assert db.conn.execute("SELECT status FROM broker_sync_windows").fetchone()[0] == "complete"
    finally:
        db.conn.close()
