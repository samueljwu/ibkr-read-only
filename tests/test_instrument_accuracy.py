import pytest
import sqlite3

from app.finance.calculations import closed_lot, position_metrics
from app.storage.database import Database
from app.market.market_data import MockMarketDataProvider
from app.finance.services import PortfolioService


def test_option_multiplier_applies_to_all_position_values():
    result = position_metrics(2, 10, 1, 12, 11, 10000, multiplier=100)
    assert result["value"] == 2400
    assert result["daily_pnl"] == 200
    assert result["unrealized_pnl"] == 400
    assert result["portfolio_pct"] == pytest.approx(0.24)


def test_option_closed_lot_allocates_commission_once():
    buy = {"quantity": 2, "price": 10, "fx_to_usd": 1, "multiplier": 100, "commission_usd": 5}
    sell = {"quantity": 2, "price": 12, "fx_to_usd": 1, "multiplier": 100, "commission_usd": 5}
    result = closed_lot(buy, sell, 2)
    assert result["realized_gross"] == 400
    assert result["realized_net"] == 390


def test_future_notional_is_not_reported_as_holdings_value(tmp_path):
    db = Database(tmp_path / "future.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "FUT", "ib_symbol": "FUT",
                                       "security_type": "FUT", "multiplier": 50})
        db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                      "side": "BUY", "quantity": 1, "price": 100,
                      "security_type": "FUT", "multiplier": 50})
        market = MockMarketDataProvider()
        market.quotes[position_id] = {"ticks": {"last": 101}, "prior_close": 100,
                                      "mode": "Live", "status": "OK"}
        service = PortfolioService(db, market)
        row = service.portfolio_rows()[0]
        assert row["last_px"] == 101
        assert row["value"] is None
        assert row["daily_pnl"] is None
        assert row["data_status"] == "FUTURES VALUATION UNAVAILABLE"
        assert service.lots_rows([row])[0]["net_unrealized"] is None
    finally:
        db.conn.close()


def test_fx_to_usd_is_multiplicative_and_invalid_fx_is_blank():
    assert position_metrics(1, 900, 0.5, 1000, 950, 1000)["value"] == 500
    assert position_metrics(1, 900, 0, 1000, 950, 1000)["value"] is None


def test_option_in_mock_mode_never_uses_underlying_stock_quote(tmp_path):
    db = Database(tmp_path / "instrument.sqlite3")
    try:
        db.add_position({"display_ticker": "QQQ US 12/18/26 C800 Equity", "ib_symbol": "QQQ",
                         "security_type": "OPT", "multiplier": 100, "position_qty": 2})
        market = MockMarketDataProvider()
        market.refresh(db.rows("positions"))
        row = PortfolioService(db, market).portfolio_rows()[0]
        assert row["last_px"] is None
        assert row["value"] is None
        assert row["data_status"] == "NO TRADES"
        db.update("positions", row["id"], "security_type", "STK")
        assert db.rows("positions")[0]["multiplier"] == 1
    finally:
        db.conn.close()


def test_option_like_ticker_is_not_silently_treated_as_stock(tmp_path):
    db = Database(tmp_path / "safety.sqlite3")
    try:
        db.add_position({"display_ticker": "QQQ US 12/18/26 C800 Equity", "ib_symbol": "QQQ",
                         "position_qty": 2})
        market = MockMarketDataProvider()
        market.refresh(db.rows("positions"))
        row = PortfolioService(db, market).portfolio_rows()[0]
        assert row["last_px"] is None
        assert row["value"] is None
        assert row["data_status"] == "VERIFY SECURITY TYPE"
    finally:
        db.conn.close()


def test_option_match_requires_same_instrument_multiplier(tmp_path):
    db = Database(tmp_path / "matching.sqlite3")
    try:
        buy = db.add_trade({"display_ticker": "OPT", "side": "BUY", "trade_date": "2026-01-02",
                            "quantity": 2, "price": 10, "security_type": "OPT", "multiplier": 100})
        sell = db.add_trade({"display_ticker": "OPT", "side": "SELL", "trade_date": "2026-01-03",
                             "quantity": 2, "price": 12, "security_type": "OPT", "multiplier": 10})
        with pytest.raises(ValueError, match="multiplier"):
            PortfolioService(db, MockMarketDataProvider()).matching.match(sell, [(buy, 2)])
    finally:
        db.conn.close()


def test_existing_pilot_database_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY, display_ticker TEXT, ib_symbol TEXT,
          security_type TEXT, exchange TEXT, primary_exchange TEXT, currency TEXT,
          position_qty REAL, average_cost REAL, fx_to_usd REAL, notes TEXT, con_id INTEGER,
          updated_at TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, trade_id TEXT UNIQUE, trade_date TEXT,
          display_ticker TEXT, ib_symbol TEXT, side TEXT, quantity REAL, price REAL,
          currency TEXT, fx_to_usd REAL, commission_usd REAL, notes TEXT, updated_at TEXT);
        INSERT INTO positions VALUES (1,'XYZ','XYZ','STK','SMART','','USD',10,12,1,'',NULL,'2026-01-01');
        INSERT INTO positions VALUES (2,'OPT','XYZ','OPT','SMART','','USD',2,10,1,'',12345,'2026-01-01');
        INSERT INTO trades VALUES (1,'T1','2026-01-01','XYZ','XYZ','BUY',10,12,'USD',1,0,'','2026-01-01');
        INSERT INTO trades VALUES (2,'T2','2026-01-01','OPT','XYZ','BUY',2,10,'USD',1,0,'','2026-01-01');
    """)
    conn.close()
    db = Database(path)
    try:
        assert db.rows("positions")[0]["multiplier"] == 1
        assert db.rows("trades")[0]["multiplier"] == 1
        assert db.rows("positions")[0]["display_ticker"] == "XYZ"
        assert db.rows("positions")[1]["multiplier"] is None
        assert db.rows("positions")[1]["con_id"] is None
        assert db.rows("trades")[1]["multiplier"] is None
        db.update("positions", 1, "security_type", "OPT")
        assert db.rows("positions")[0]["multiplier"] is None
    finally:
        db.conn.close()
