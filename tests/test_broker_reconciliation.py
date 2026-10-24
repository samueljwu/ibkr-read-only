from app.storage.database import Database
from app.market.market_data import MockMarketDataProvider
from app.finance.services import PortfolioService


def test_broker_snapshot_checks_derived_quantity_and_nav(tmp_path):
    db = Database(tmp_path / "broker.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "AAPL", "ib_symbol": "AAPL"})
        db.cache_contract(position_id, 900000001, multiplier=1)
        db.add_trade({"position_id": position_id, "trade_date": "2026-01-01", "side": "BUY",
                      "quantity": 100, "price": 150, "commission_usd": 1})
        db.save_broker_positions(1, [{"con_id": 900000001, "quantity": 95, "symbol": "AAPL", "currency": "USD"}],
                                 "2026-10-06T12:00:00")
        db.set_metadata("broker_positions_refresh_1", "2026-10-06T12:00:00")
        db.set_account_value(1, "NetLiquidation", 100000, "USD", "2026-10-06T12:00:00")
        db.set_account_value(1, "TotalCashValue", 10000, "USD", "2026-10-06T12:00:00")
        market = MockMarketDataProvider()
        market.refresh(db.rows("positions", 1))
        row = PortfolioService(db, market).portfolio_rows()[0]
        assert row["position_qty"] == 100
        assert row["broker_qty"] == 95
        assert row["qty_difference"] == 5
        assert row["reconciliation"] == "DIFFERENCE"
        assert row["value"] is None
        open_lot = PortfolioService(db, market).lots_rows()[0]
        assert open_lot["net_unrealized"] is None
        assert open_lot["lot_note"] == "IBKR qty differs"
        db.save_broker_positions(1, [{"con_id": 900000001, "quantity": 100,
                                      "symbol": "AAPL", "currency": "USD"}], "2026-10-06T12:01:00")
        matched = PortfolioService(db, market).portfolio_rows()[0]
        assert matched["portfolio_pct"] == matched["value"] / 100000
        assert PortfolioService(db, market).lots_rows()[0]["net_unrealized"] is not None
    finally:
        db.conn.close()




def test_broker_position_delta_updates_and_removes_holding(tmp_path):
    db = Database(tmp_path / "delta.sqlite3")
    try:
        row = {"con_id": 123, "quantity": 5, "symbol": "ABC", "currency": "USD"}
        db.save_broker_positions(1, [row], "2026-10-06T12:00:00")
        db.save_broker_position_delta(1, {**row, "quantity": 3}, "2026-10-06T12:01:00")
        assert db.broker_positions(1)[0]["quantity"] == 3
        db.save_broker_position_delta(1, {**row, "quantity": 0}, "2026-10-06T12:02:00")
        assert db.broker_positions(1) == []
    finally:
        db.conn.close()
