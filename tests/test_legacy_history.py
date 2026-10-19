"""Synthetic pre-Flex CSV import and coverage checks."""

import csv
from datetime import date
from pathlib import Path

import pytest

from app.storage.database import Database
from app.flex import flex_import
from app.sync import legacy_history
from app.finance.services import PortfolioService
from app.market.market_data import MockMarketDataProvider
from app.sync.legacy_history import LegacyHistoryError, import_legacy_history
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


def test_older_csv_cutoff_tracks_flex_window(tmp_path, monkeypatch):
    _, statement = reports(tmp_path)
    rows, _ = legacy_history._read_csv(statement)
    monkeypatch.setattr(flex_import, "flex_today", lambda: date(2024, 6, 1))
    assert flex_import.oldest_flex_date() == date(2020, 1, 1)
    with pytest.raises(LegacyHistoryError, match="before 2020-01-01"):
        legacy_history._parse_activity_statement(rows, ACCOUNT)
    monkeypatch.setattr(flex_import, "flex_today", lambda: date(2026, 6, 1))
    assert flex_import.oldest_flex_date() == date(2022, 1, 1)
    assert legacy_history._parse_activity_statement(rows, ACCOUNT)[1] == date(2021, 5, 31)






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




def test_csv_close_does_not_replace_recorded_purchase(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    db.add_trade({"display_ticker": "TEST", "trade_date": "2021-04-20",
                  "side": "BUY", "quantity": 1, "price": 7})
    pa, statement = reports(tmp_path, close_price="12")
    assert import_legacy_history(db, ACCOUNT, pa, statement)["estimated_opening_lots"] == 0
    assert len(db.rows("trades", 1)) == 1
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
