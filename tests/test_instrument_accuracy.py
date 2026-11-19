import pytest
import sqlite3

from app.finance.calculations import closed_lot, position_metrics
from app.storage.database import Database
from app.market.market_data import MockMarketDataProvider
from app.finance.services import PortfolioService






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


def test_option_price_source_labels_prior_close_and_unknown_cached_quote(tmp_path):
    db = Database(tmp_path / "option-close.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "SYN 270115 P420", "ib_symbol": "SYN",
                                       "security_type": "OPT", "option_right": "P",
                                       "multiplier": 100})
        db.add_trade({"position_id": position_id, "trade_date": "2026-01-02", "side": "SELL",
                      "quantity": 1, "price": 10, "security_type": "OPT", "multiplier": 100})
        market = MockMarketDataProvider()
        market.quotes[position_id] = {"ticks": {"close": 9.45}, "mode": "Live",
                                      "status": "OK", "cached_price": 9.45,
                                      "cached_source": "CACHED QUOTE",
                                      "cached_as_of": "2026-01-03T10:00:00"}
        service = PortfolioService(db, market)
        row = service.portfolio_rows()[0]
        assert row["last_px"] == 9.45
        assert row["value"] == pytest.approx(-945)
        assert row["daily_pnl"] is None
        assert row["price_source"] == "PRIOR CLOSE"

        del market.quotes[position_id]["ticks"]["close"]
        row = service.portfolio_rows()[0]
        assert row["last_px"] == 9.45
        assert "SOURCE UNKNOWN" in row["price_source"]
        assert row["daily_pnl"] is None

        market.quotes[position_id]["cached_source"] = "LIVE LAST"
        row = service.portfolio_rows()[0]
        assert row["last_px"] == 9.45
        assert "LIVE LAST" in row["price_source"]

        market.quotes[position_id]["ticks"]["last"] = 8.8
        row = service.portfolio_rows()[0]
        assert row["last_px"] == 8.8
        assert row["price_source"] == "LIVE LAST"
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
