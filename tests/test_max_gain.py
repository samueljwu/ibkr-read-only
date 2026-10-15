import pytest

from app.storage.database import Database
from app.market.market_data import MockMarketDataProvider
from app.finance.services import PortfolioService


def test_partial_sale_maximizes_net_gain_and_preserves_buy(tmp_path):
    db = Database(tmp_path / "gain.sqlite3")
    try:
        early = db.add_trade({"display_ticker": "AAPL", "trade_date": "2026-01-01", "side": "BUY",
                              "quantity": 100, "price": 150, "commission_usd": 2})
        later = db.add_trade({"display_ticker": "AAPL", "trade_date": "2026-01-15", "side": "BUY",
                              "quantity": 100, "price": 140, "commission_usd": 20})
        sell = db.add_trade({"display_ticker": "AAPL", "trade_date": "2026-01-31", "side": "SELL",
                             "quantity": 50, "price": 170, "commission_usd": 1})
        service = PortfolioService(db, MockMarketDataProvider())
        assert service.matching.suggested_max_gain(sell) == [(later, 50)]
        service.matching.auto_max_gain()
        lots = service.lots_rows()
        assert len([lot for lot in lots if lot.get("sell_qty")]) == 1
        assert lots[0]["realized_net"] == pytest.approx(1489)
        assert service.matching.remaining()[early] == 100
        assert service.matching.remaining()[later] == 50
        assert db.rows("trades")[1]["quantity"] == 100
        assert service.portfolio_rows()[0]["position_qty"] == 150
        service.matching.replace_sell_matches(sell, [(early, 50)])
        assert service.lots_rows()[0]["realized_net"] == pytest.approx(998)
        assert service.matching.remaining()[early] == 50
        assert service.matching.remaining()[later] == 100
    finally:
        db.conn.close()


def test_five_field_trade_inherits_instrument_and_missing_fx_stays_blank(tmp_path):
    db = Database(tmp_path / "entry.sqlite3")
    try:
        instrument = db.add_position({"display_ticker": "ABC HK Equity", "ib_symbol": "ABC",
                                      "currency": "HKD"})
        trade_id = db.add_trade({"position_id": instrument, "trade_date": "2026-01-01",
                                 "side": "BUY", "quantity": 10, "price": 20, "commission_usd": 1})
        trade = db.rows("trades")[0]
        assert trade["id"] == trade_id
        assert trade["display_ticker"] == "ABC HK Equity"
        assert trade["currency"] == "HKD"
        assert trade["fx_to_usd"] == 0
        assert PortfolioService(db, MockMarketDataProvider()).matching.trade_rows()[0]["gross_usd"] is None
        db.save_fx_history("HKD", [{"day": "2025-12-31", "close": 0.128}])
        assert db.rows("trades")[0]["fx_to_usd"] == 0.128
    finally:
        db.conn.close()


def test_same_day_broker_time_can_precede_import_order(tmp_path):
    db = Database(tmp_path / "order.sqlite3")
    try:
        sell = db.add_trade({"display_ticker": "XYZ", "trade_date": "2026-01-02",
                             "executed_at": "110000", "side": "SELL", "quantity": 5,
                             "price": 12, "commission_usd": 0})
        buy = db.add_trade({"display_ticker": "XYZ", "trade_date": "2026-01-02",
                            "executed_at": "100000", "side": "BUY", "quantity": 5,
                            "price": 10, "commission_usd": 0})
        matcher = PortfolioService(db, MockMarketDataProvider()).matching
        assert matcher.suggested_max_gain(sell) == [(buy, 5)]
    finally:
        db.conn.close()


def test_backdated_buy_reoptimizes_auto_matches_but_keeps_manual_choice(tmp_path):
    db = Database(tmp_path / "rebuild.sqlite3")
    try:
        first = db.add_trade({"display_ticker": "ABC", "trade_date": "2026-01-01", "side": "BUY",
                              "quantity": 100, "price": 150, "commission_usd": 0})
        sell = db.add_trade({"display_ticker": "ABC", "trade_date": "2026-01-31", "side": "SELL",
                             "quantity": 50, "price": 170, "commission_usd": 0})
        matcher = PortfolioService(db, MockMarketDataProvider()).matching
        matcher.rebuild_auto()
        assert db.rows("trade_matches")[0]["buy_trade_id"] == first
        cheaper = db.add_trade({"display_ticker": "ABC", "trade_date": "2026-01-10", "side": "BUY",
                                "quantity": 100, "price": 140, "commission_usd": 0})
        matcher.rebuild_auto()
        assert db.rows("trade_matches")[0]["buy_trade_id"] == cheaper
        matcher.replace_sell_matches(sell, [(first, 50)])
        db.add_trade({"display_ticker": "ABC", "trade_date": "2026-01-20", "side": "BUY",
                      "quantity": 100, "price": 130, "commission_usd": 0})
        matcher.rebuild_auto()
        assert db.rows("trade_matches")[0]["buy_trade_id"] == first
        assert db.rows("trade_matches")[0]["origin"] == "manual"
    finally:
        db.conn.close()
