"""Synthetic pre-Flex CSV import and coverage checks."""

import csv
from pathlib import Path

import pytest

from app.storage.database import Database
from app.platform.history_status import read_status
from app.finance.services import PortfolioService
from app.finance.position_returns import position_return_rows
from app.broker.broker_sync import BrokerIngestor
from app.market.market_data import MockMarketDataProvider
from app.sync.legacy_history import LegacyHistoryError, import_legacy_history, remove_legacy_history
from scripts.legacy_history_worker import main as worker_main


ACCOUNT = "SYNTHETIC"


def write_csv(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as target:
        csv.writer(target).writerows(rows)
    return path


def reports(tmp_path, *, account=ACCOUNT, ending_nav="120", ending_cash="20",
            closing_qty="1", close_price=None):
    pa = write_csv(tmp_path / "portfolio.csv", [
        ["Introduction", "Header", "Account", "BaseCurrency", "AnalysisPeriod", "PerformanceMeasure"],
        ["Introduction", "Data", account, "USD",
         "April 13, 2021 to May 31, 2021 (Monthly)", "TWR"],
        ["Key Statistics", "Header", "CumulativeReturn", "EndingNAV"],
        ["Key Statistics", "Data", "4.0", ending_nav],
        ["Allocation by Asset Class", "Header", "Date", "NAV", "Cash"],
        ["Allocation by Asset Class", "Data", "202104", "100", "10"],
        ["Allocation by Asset Class", "Data", "202105", ending_nav, ending_cash],
        ["Time Period Benchmark Comparison", "Header", "Date", account,
         account + "Return"],
        ["Time Period Benchmark Comparison", "Data", "Apr 2021", "100", "1.5"],
        ["Time Period Benchmark Comparison", "Data", "May 2021", ending_nav, "2.5"],
    ])
    statement = write_csv(tmp_path / "statement.csv", [
        ["Statement", "Data", "Period", "April 13, 2021 - May 31, 2021"],
        ["Account Information", "Data", "Account", ACCOUNT],
        ["Account Information", "Data", "Base Currency", "USD"],
        ["Net Asset Value", "Header", "Asset Class", "Current Total"],
        ["Net Asset Value", "Data", "Cash", "19"],
        ["Net Asset Value", "Data", "Stock", "100"],
        ["Net Asset Value", "Data", "Dividend Accruals", "1"],
        ["Net Asset Value", "Data", "Total", "120"],
        ["Change in NAV", "Header", "Field Name", "Field Value"],
        ["Change in NAV", "Data", "Starting Value", "0"],
        ["Change in NAV", "Data", "Deposits", "120"],
        ["Change in NAV", "Data", "Ending Value", "120"],
        ["Trades", "Header", "Asset Category", "Currency", "Symbol", "Date/Time", "Quantity"],
        ["Trades", "Data", "Stocks", "USD", "TEST", "2021-04-20 12:00:00", "1"],
        ["Open Positions", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Quantity", "Close Price"],
        ["Open Positions", "Data", "Summary", "Stocks", "USD", "TEST", closing_qty, close_price or ""],
    ])
    return pa, statement


def test_monthly_history_and_evidence_are_separate_from_daily_data(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    result = import_legacy_history(db, ACCOUNT, pa, statement)
    assert result["new_months"] == 2
    assert result["evidence_rows"] == 2
    monthly = db.monthly_history(1)
    assert [row["month"] for row in monthly] == ["2021-04", "2021-05"]
    assert monthly[0]["period_from"] == "2021-04-13"
    assert monthly[-1]["return_pct"] == "2.5"
    assert len(db.legacy_statement_periods(1)) == 1
    assert db.reported_account_performance(1)["cumulative_return_pct"] == "4.0"
    openings = db.legacy_opening_quantities(1)
    assert len(openings) == 1
    assert list(openings.values())[0]["quantity"] == "1"
    assert db.conn.execute("SELECT COUNT(*) FROM legacy_statement_rows").fetchone()[0] == 2
    assert db.conn.execute("SELECT COUNT(*) FROM account_history").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert import_legacy_history(db, ACCOUNT, pa, statement)["new_months"] == 0
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 1
    db.conn.close()


def test_statement_quantity_anchors_later_trade_ledger(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    import_legacy_history(db, ACCOUNT, pa, statement)
    service = PortfolioService(db, MockMarketDataProvider())
    position = service.portfolio_rows()[0]
    assert position["position_qty"] == 1
    db.add_trade({"position_id": position["id"], "trade_id": "synthetic-2022-buy",
                  "trade_date": "2022-01-10", "side": "BUY", "quantity": 2,
                  "price": 1, "security_type": "STK"})
    assert service.portfolio_rows()[0]["position_qty"] == 3
    db.conn.close()


def test_existing_pre_statement_trade_is_not_counted_twice(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    db.add_trade({"display_ticker": "TEST", "ib_symbol": "TEST", "trade_id": "manual-2021",
                  "trade_date": "2021-04-20", "side": "BUY", "quantity": 1,
                  "price": 1, "security_type": "STK"})
    pa, statement = reports(tmp_path)
    import_legacy_history(db, ACCOUNT, pa, statement)
    service = PortfolioService(db, MockMarketDataProvider())
    assert service.portfolio_rows()[0]["position_qty"] == 1
    assert len(db.rows("positions", 1)) == 1
    db.conn.close()


def test_csv_close_creates_estimated_lot_without_doubling_position(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path, close_price="12")
    result = import_legacy_history(db, ACCOUNT, pa, statement)
    assert result["estimated_opening_lots"] == 1
    position = db.rows("positions", 1)[0]
    opening = db.rows("trades", 1)[0]
    assert (opening["source"], opening["trade_date"], opening["quantity"], opening["price"]) == (
        "CSV market baseline", "2021-05-31", 1, 12)
    market = MockMarketDataProvider()
    market.quotes[position["id"]] = {"ticks": {"last": 15}, "mode": "Live"}
    service = PortfolioService(db, market)
    overview = service.portfolio_rows()[0]
    assert overview["position_qty"] == 1
    assert overview["average_cost"] == pytest.approx(12)
    assert overview["unrealized_pnl"] == pytest.approx(3)
    assert overview["estimated_baseline"] == "2021-05-31"
    assert "ESTIMATED CSV COST" in overview["data_status"]
    sell = db.add_trade({"position_id": position["id"], "trade_date": "2022-01-10",
                         "side": "SELL", "quantity": 1, "price": 15})
    service.matching.rebuild_auto({position["id"]})
    assert service.portfolio_rows()[0]["position_qty"] == 0
    assert db.rows("trade_matches", 1)[0]["sell_trade_id"] == sell
    valuation = {"id": position["id"], "position_qty": 0, "reconciliation": "OK"}
    lots = service.lots_rows([valuation])
    assert lots[0]["realized_net"] == 3
    assert "Estimated CSV closing value" in lots[0]["lot_note"]
    with db.conn:
        db.conn.execute("""INSERT INTO broker_sync_windows
            (account_id,source,from_day,to_day,status) VALUES
            (?, 'flex_cash', '2021-06-01', '2022-01-10', 'complete')""", (ACCOUNT,))
    returns, _ = position_return_rows(db, 1, [valuation], lots)
    assert returns[0]["total"] == 3
    assert returns[0]["estimated_baseline"] == "2021-05-31"
    assert import_legacy_history(db, ACCOUNT, pa, statement)["estimated_opening_lots"] == 0
    remove_legacy_history(db, ACCOUNT)
    assert [trade["id"] for trade in db.rows("trades", 1)] == [sell]
    assert db.rows("trade_matches", 1) == []
    db.conn.close()


def test_csv_close_does_not_replace_recorded_purchase(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    db.add_trade({"display_ticker": "TEST", "trade_date": "2021-04-20",
                  "side": "BUY", "quantity": 1, "price": 7})
    pa, statement = reports(tmp_path, close_price="12")
    assert import_legacy_history(db, ACCOUNT, pa, statement)["estimated_opening_lots"] == 0
    assert len(db.rows("trades", 1)) == 1
    db.conn.close()


def test_csv_contract_id_links_renamed_holding_to_existing_position(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    target = db.add_position({"display_ticker": "RENAMED", "ib_symbol": "RENAMED"})
    db.cache_contract(target, 270480)
    pa, statement = reports(tmp_path, close_price="12")
    with Path(statement).open("a", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["Financial Instrument Information", "Header",
                         "Asset Category", "Symbol", "Conid"])
        writer.writerow(["Financial Instrument Information", "Data",
                         "Stocks", "TEST", "270480"])
    import_legacy_history(db, ACCOUNT, pa, statement)
    assert len(db.rows("positions", 1)) == 1
    assert db.legacy_opening_quantities(1)[target]["quantity"] == "1"
    assert db.rows("trades", 1)[0]["position_id"] == target
    db.conn.close()


def test_reimport_relinks_unedited_legacy_identity_to_contract(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path, close_price="12")
    with Path(statement).open("a", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["Financial Instrument Information", "Header",
                         "Asset Category", "Symbol", "Conid"])
        writer.writerow(["Financial Instrument Information", "Data",
                         "Stocks", "TEST", "270480"])
    import_legacy_history(db, ACCOUNT, pa, statement)
    old_id = db.rows("positions", 1)[0]["id"]
    with db.conn:
        db.conn.execute("DELETE FROM contracts WHERE position_id=?", (old_id,))
        db.conn.execute("UPDATE positions SET con_id=NULL WHERE id=?", (old_id,))
    target = db.add_position({"display_ticker": "RENAMED", "ib_symbol": "RENAMED"})
    db.cache_contract(target, 270480)
    import_legacy_history(db, ACCOUNT, pa, statement)
    assert [p["id"] for p in db.rows("positions", 1)] == [target]
    assert db.legacy_opening_quantities(1)[target]["quantity"] == "1"
    assert [t["position_id"] for t in db.rows("trades", 1)] == [target]
    db.conn.close()


def test_broker_transfer_replaces_csv_market_estimate(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path, close_price="12")
    with Path(statement).open("a", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["Financial Instrument Information", "Header",
                         "Asset Category", "Symbol", "Conid"])
        writer.writerow(["Financial Instrument Information", "Data",
                         "Stocks", "TEST", "270480"])
    import_legacy_history(db, ACCOUNT, pa, statement)
    assert db.rows("trades", 1)[0]["source"] == "CSV market baseline"
    transfer = {"kind": "Transfer", "raw": {
        "accountId": ACCOUNT, "transactionID": "SYN-TRANSFER", "direction": "IN",
        "assetCategory": "STK", "conid": "270480", "symbol": "TEST",
        "currency": "USD", "quantity": "1", "cost": "7", "multiplier": "1",
        "date": "20210414"}}
    BrokerIngestor(db, ACCOUNT).ingest_many([], "flex", events=[transfer])
    trades = db.rows("trades", 1)
    assert len(trades) == 1
    assert (trades[0]["source"], trades[0]["price"]) == ("IBKR legacy transfer", 7)
    overview = PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]
    assert overview["average_cost"] == pytest.approx(7)
    assert overview["estimated_baseline"] is None
    db.conn.close()


def test_reimport_keeps_original_opening_identity_after_symbol_edit(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    import_legacy_history(db, ACCOUNT, pa, statement)
    position = db.rows("positions", 1)[0]
    db.update("positions", position["id"], "ib_symbol", "RENAMED")
    result = import_legacy_history(db, ACCOUNT, pa, statement)
    assert result["new_opening_positions"] == 0
    assert len(db.rows("positions", 1)) == 1
    db.conn.close()


def test_existing_opening_table_gains_symbol_and_currency_without_losing_anchor(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    import_legacy_history(db, ACCOUNT, pa, statement)
    opening = db.conn.execute("SELECT * FROM legacy_position_openings").fetchone()
    with db.conn:
        db.conn.execute("DROP TABLE legacy_position_openings")
        db.conn.execute("""CREATE TABLE legacy_position_openings (
            portfolio_id INTEGER NOT NULL REFERENCES portfolios(id), account_id TEXT NOT NULL,
            position_id INTEGER NOT NULL REFERENCES positions(id), as_of TEXT NOT NULL,
            quantity TEXT NOT NULL, source_hash TEXT NOT NULL,
            PRIMARY KEY(portfolio_id,account_id,position_id,as_of))""")
        db.conn.execute("""INSERT INTO legacy_position_openings
            (portfolio_id,account_id,position_id,as_of,quantity,source_hash)
            VALUES (?,?,?,?,?,?)""",
            tuple(opening[key] for key in (
                "portfolio_id", "account_id", "position_id", "as_of", "quantity", "source_hash")))
    db.conn.close()

    db = Database(tmp_path / "portfolio.sqlite3")
    migrated = db.conn.execute("SELECT symbol,currency,quantity FROM legacy_position_openings").fetchone()
    assert tuple(migrated) == ("TEST", "USD", "1")
    assert import_legacy_history(db, ACCOUNT, pa, statement)["new_opening_positions"] == 0
    assert PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]["position_qty"] == 1
    db.conn.close()


def test_report_order_is_detected_from_contents(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    result = import_legacy_history(db, ACCOUNT, statement, pa)
    assert result["new_months"] == 2
    db.conn.close()


def test_two_reports_of_same_kind_are_rejected(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, _ = reports(tmp_path)
    with pytest.raises(LegacyHistoryError, match="one PortfolioAnalyst Detailed CSV"):
        import_legacy_history(db, ACCOUNT, pa, pa)
    assert db.monthly_history(1) == []
    db.conn.close()


@pytest.mark.parametrize("change, message", [
    ({"account": "OTHER"}, "account"),
    ({"ending_nav": "121"}, "NAV"),
    ({"ending_cash": "21"}, "cash"),
    ({"closing_qty": "2"}, "does not reconcile"),
])
def test_rejects_mismatch_without_partial_import(tmp_path, change, message):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path, **change)
    with pytest.raises(LegacyHistoryError, match=message):
        import_legacy_history(db, ACCOUNT, pa, statement)
    assert db.monthly_history(1) == []
    assert db.legacy_statement_periods(1) == []
    db.conn.close()


def test_hidden_worker_reports_monthly_result_without_daily_claim(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    db.conn.close()
    pa, statement = reports(tmp_path)
    status_path = tmp_path / "history-status.json"
    assert worker_main([
        "--status-file", str(status_path), "--database", str(db.path),
        "--reports", str(statement), str(pa)]) == 0
    status = read_status(status_path)
    assert status["state"] == "completed"
    assert status["legacy_result"]["months"] == 2
    assert ACCOUNT not in status_path.read_text(encoding="utf-8")


def test_unexpected_worker_error_logs_type_and_location_without_report_details(tmp_path, monkeypatch):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    db.conn.close()
    pa, statement = reports(tmp_path)
    import scripts.legacy_history_worker as worker
    monkeypatch.setattr(worker, "import_legacy_history",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("SYNTHETIC PRIVATE REPORT")))
    status_path = tmp_path / "history-status.json"
    assert worker_main(["--status-file", str(status_path), "--database", str(db.path),
                        "--reports", str(statement), str(pa)]) == 1
    log = (tmp_path / "logs" / "history-import.log").read_text(encoding="utf-8")
    assert "error=RuntimeError" in log and "location=" in log
    assert "SYNTHETIC PRIVATE REPORT" not in log
    assert "SYNTHETIC PRIVATE REPORT" not in status_path.read_text(encoding="utf-8")


def test_remove_older_csv_history_keeps_later_trades_and_allows_replacement(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    pa, statement = reports(tmp_path)
    import_legacy_history(db, ACCOUNT, pa, statement)
    position_id = db.rows("positions", 1)[0]["id"]
    db.add_trade({"position_id": position_id, "trade_id": "synthetic-later-buy",
                  "trade_date": "2022-01-10", "side": "BUY", "quantity": 2,
                  "price": 1, "security_type": "STK"})
    with db.conn:
        db.conn.execute(
            """INSERT INTO account_history
               (portfolio_id,day,nav_usd,source) VALUES (1,'2022-01-10',125,'flex')""")
    assert PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]["position_qty"] == 3
    db.conn.close()

    status_path = tmp_path / "remove-status.json"
    assert worker_main(["--status-file", str(status_path), "--database", str(tmp_path / "portfolio.sqlite3"),
                        "--remove"]) == 0
    status = read_status(status_path)
    assert status["state"] == "completed"
    assert status["action"] == "remove"
    assert ACCOUNT not in status_path.read_text(encoding="utf-8")
    db = Database(tmp_path / "portfolio.sqlite3")
    assert not db.has_legacy_history(1)
    assert db.reported_account_performance(1) is None
    assert db.monthly_history(1) == []
    assert db.legacy_opening_quantities(1) == {}
    assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM account_history").fetchone()[0] == 1
    assert PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]["position_qty"] == 2
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == 2

    with Path(statement).open("a", encoding="utf-8", newline="") as target:
        csv.writer(target).writerow(["Notes", "Data", "Synthetic correction"])
    assert import_legacy_history(db, ACCOUNT, pa, statement)["new_statement"]
    assert db.has_legacy_history(1)
    assert PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]["position_qty"] == 3
    db.conn.close()
