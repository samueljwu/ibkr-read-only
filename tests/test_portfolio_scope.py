import sqlite3

import pytest

from app.storage.database import Database
from app.market.market_data import MockMarketDataProvider
from app.finance.services import PortfolioService


def test_portfolio_values_ledger_and_lots_stay_separate(tmp_path):
    db = Database(tmp_path / "scoped.sqlite3")
    try:
        dw = db.add_portfolio("DW")
        db.set_portfolio_nav(1, 1000)
        db.set_portfolio_nav(dw, 2000)
        db.add_position({"portfolio_id": 1, "display_ticker": "XYZ", "position_qty": 10})
        db.add_position({"portfolio_id": dw, "display_ticker": "XYZ", "position_qty": 20})
        sw_buy = db.add_trade({"portfolio_id": 1, "display_ticker": "XYZ", "side": "BUY",
                               "trade_date": "2026-01-01", "quantity": 10, "price": 10})
        sw_sell = db.add_trade({"portfolio_id": 1, "display_ticker": "XYZ", "side": "SELL",
                                "trade_date": "2026-01-02", "quantity": 2, "price": 12})
        dw_buy = db.add_trade({"portfolio_id": dw, "display_ticker": "XYZ", "side": "BUY",
                               "trade_date": "2026-01-01", "quantity": 20, "price": 10})
        market = MockMarketDataProvider()
        market.refresh(db.rows("positions"))
        sw = PortfolioService(db, market, 1)
        other = PortfolioService(db, market, dw)
        assert len(sw.portfolio_rows()) == len(other.portfolio_rows()) == 1
        assert sw.portfolio_rows()[0]["ledger_qty"] == 8
        assert other.portfolio_rows()[0]["ledger_qty"] == 20
        assert sw.portfolio_rows()[0]["portfolio_pct"] == pytest.approx(
            sw.portfolio_rows()[0]["value"] / 1000)
        assert other.portfolio_rows()[0]["portfolio_pct"] == pytest.approx(
            other.portfolio_rows()[0]["value"] / 2000)
        with pytest.raises(ValueError, match="outside this portfolio"):
            sw.matching.match(sw_sell, [(dw_buy, 2)])
        sw.matching.match(sw_sell, [(sw_buy, 2)])
        assert len(db.rows("trade_matches", 1)) == 1
        assert db.rows("trade_matches", dw) == []
        assert len(other.lots_rows()) == 1
    finally:
        db.conn.close()


def test_legacy_rows_and_nav_migrate_to_sw_once(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY, display_ticker TEXT, ib_symbol TEXT,
          security_type TEXT, exchange TEXT, primary_exchange TEXT, currency TEXT,
          position_qty REAL, average_cost REAL, fx_to_usd REAL, notes TEXT, con_id INTEGER,
          updated_at TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_id TEXT UNIQUE, trade_date TEXT,
          display_ticker TEXT, ib_symbol TEXT, side TEXT, quantity REAL, price REAL,
          currency TEXT, fx_to_usd REAL, commission_usd REAL, notes TEXT, updated_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('portfolio_nav_usd','12345');
        INSERT INTO positions VALUES (1,'XYZ','XYZ','STK','SMART','','USD',5,10,1,'',NULL,'2026-01-01');
        INSERT INTO trades VALUES (1,'T1','2026-01-01','XYZ','XYZ','BUY',5,10,'USD',1,0,'','2026-01-01');
    """)
    conn.close()
    db = Database(path)
    assert db.portfolio_nav(1) == 12345
    assert db.rows("positions", 1)[0]["display_ticker"] == "XYZ"
    assert db.rows("trades", 1)[0]["trade_id"] == "T1"
    db.set_portfolio_nav(1, None)
    db.conn.close()
    db = Database(path)
    try:
        assert db.portfolio_nav(1) is None
    finally:
        db.conn.close()


def test_ibkr_nav_uses_only_broker_snapshot(tmp_path):
    db = Database(tmp_path / "broker-nav.sqlite3")
    try:
        db.set_portfolio_nav(1, 1000)
        assert db.effective_nav(1) == 1000  # Mock mode still supports test data.
        db.set_setting("provider", "IBKR")
        assert db.effective_nav(1) is None
        db.set_account_value(1, "NetLiquidation", 1250, "USD", "2026-10-07T09:00:00")
        assert db.effective_nav(1) == 1250
    finally:
        db.conn.close()
