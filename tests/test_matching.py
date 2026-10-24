import pytest
from datetime import date, datetime
from app.storage.database import Database
from app.market.market_data import MarketDataProvider, MockMarketDataProvider
from app.finance.services import MatchingService, PortfolioService


@pytest.fixture
def db(tmp_path):
    store = Database(tmp_path / "test.sqlite3")
    yield store
    store.conn.close()


def trade(db, side, qty, price, day="2026-01-02", commission=0):
    return db.add_trade({"trade_date": day, "display_ticker": "XYZ", "side": side,
                         "quantity": qty, "price": price, "commission_usd": commission})












def test_match_rejects_overallocation_and_preserves_raw_trades(db):
    buy = trade(db, "BUY", 100, 10)
    sell = trade(db, "SELL", 40, 12, "2026-01-05")
    matcher = MatchingService(db)
    with pytest.raises(ValueError):
        matcher.match(sell, [(buy, 41)])
    matcher.match(sell, [(buy, 40)])
    assert db.rows("trades")[0]["quantity"] == 100
    assert db.rows("trades")[1]["quantity"] == 40
    assert matcher.remaining()[buy] == 60
    with pytest.raises(ValueError):
        matcher.match(sell, [(buy, 1)])






def test_fx_execution_is_activity_instead_of_security_holding(db):
    pid = db.add_position({"display_ticker": "USD.JPY", "ib_symbol": "USD",
                           "security_type": "CASH", "multiplier": 1})
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "SELL",
                  "quantity": 10000, "price": 150, "multiplier": 1, "commission_usd": 0})
    service = PortfolioService(db, MockMarketDataProvider())
    assert service.portfolio_rows() == []
    assert service.lots_rows() == []
    fx = service.matching.trade_rows()[0]
    assert fx["match_status"] == "FX cash activity"
    assert fx["gross_lc"] == 1_500_000  # JPY received for 10,000 USD sold.
    assert fx["gross_usd"] == 10_000  # The actual USD leg, not translated JPY.












def test_trade_pnl_uses_total_basis_and_preserves_missing_values(db):
    first = trade(db, "BUY", 4, 10)
    second = trade(db, "BUY", 6, 12, "2026-01-03")
    sell = trade(db, "SELL", 10, 13, "2026-01-05")
    matcher = MatchingService(db)
    matcher.match(sell, [(first, 4), (second, 6)])
    service = PortfolioService(db, MockMarketDataProvider())
    row = next(row for row in service.trade_rows() if row["id"] == sell)
    assert row["realized_net"] == pytest.approx(18)
    assert row["realized_pct"] == pytest.approx(18 / 112)
    assert row["net_unrealized"] is None
    rows = {row["id"]: row for row in service.trade_rows()}
    assert rows[first]["realized_net"] == pytest.approx(12)
    assert rows[second]["realized_net"] == pytest.approx(6)

    lots = service.lots_rows()
    next(lot for lot in lots if lot["buy_trade_id"] == second)["realized_net"] = None
    rows = {row["id"]: row for row in service.trade_rows(lots)}
    assert rows[sell]["realized_net"] is None
    assert rows[sell]["realized_pct"] is None
    assert rows[second]["realized_net"] is None
    assert rows[first]["realized_net"] == pytest.approx(12)
























def test_validate_manual_rejects_buy_after_sell(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "SELL",
                        "quantity": 10, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    with pytest.raises(ValueError):
        matcher.validate_manual(pid, [(buy, sell, 10)])


def test_validate_manual_rejects_over_allocated_buy(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    sell1 = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                         "quantity": 5, "price": 12, "commission_usd": 0})
    sell2 = db.add_trade({"position_id": pid, "trade_date": "2026-01-11", "side": "SELL",
                         "quantity": 6, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    with pytest.raises(ValueError):
        matcher.validate_manual(pid, [(buy, sell1, 5), (buy, sell2, 6)])


def test_validate_manual_rejects_over_allocated_sell(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy1 = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    buy2 = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 10, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    with pytest.raises(ValueError):
        matcher.validate_manual(pid, [(buy1, sell, 6), (buy2, sell, 6)])


def test_validate_manual_rejects_cross_instrument(db):
    pid1 = db.add_position({"display_ticker": "AAA"})
    pid2 = db.add_position({"display_ticker": "BBB"})
    buy = db.add_trade({"position_id": pid1, "trade_date": "2026-01-01", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid2, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 10, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    with pytest.raises(ValueError):
        matcher.validate_manual(pid1, [(buy, sell, 10)])


def test_invalid_apply_leaves_trade_matches_unchanged(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                        "quantity": 10, "price": 10, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 10, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    matcher.apply_position_matches(pid, [(buy, sell, 10)])
    before = db.rows("trade_matches")
    with pytest.raises(ValueError):
        matcher.apply_position_matches(pid, [(buy, sell, 999)])
    assert db.rows("trade_matches") == before
