import pytest
from datetime import date, datetime

import app.finance.services as services_module
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


def test_partial_sell_creates_closed_and_open_lots(db):
    buy = trade(db, "BUY", 100, 10)
    sell = trade(db, "SELL", 40, 12, "2026-01-05")
    MatchingService(db).match(sell, [(buy, 40)])
    service = PortfolioService(db, MockMarketDataProvider())
    lots = service.lots_rows()
    assert len(lots) == 2
    assert [lot["buy_qty"] for lot in lots] == [40, 60]
    assert lots[0]["sell_qty"] == 40
    assert lots[1].get("sell_qty") is None
    assert service.matching.trade_rows()[1]["match_status"] == "Matched"


def test_commission_refund_changes_trade_fee_and_lot_pnl_once(db):
    buy = trade(db, "BUY", 100, 10, commission=2)
    sell = trade(db, "SELL", 100, 12, "2026-01-05", commission=3)
    MatchingService(db).match(sell, [(buy, 100)])
    service = PortfolioService(db, MockMarketDataProvider())
    assert service.lots_rows()[0]["realized_net"] == pytest.approx(195)
    with db.conn:
        db.conn.execute("""INSERT INTO trade_fee_adjustments
            (portfolio_id,account_id,source_key,trade_row_id,reported_day,refund_usd,
             description,source_hash) VALUES (1,'U_TEST','refund-1',?,'2026-01-06','1',
             'Refund (XYZ, -100, 2026-01-05)','test')""", (sell,))
    displayed = next(row for row in db.rows("trades", 1) if row["id"] == sell)
    assert displayed["raw_commission_usd"] == 3
    assert displayed["commission_refund_usd"] == 1
    assert displayed["commission_usd"] == 2
    assert service.lots_rows()[0]["realized_net"] == pytest.approx(196)
    assert db.cash_movement_rows(1) == []


def test_fifo_two_buys_for_one_sell(db):
    trade(db, "BUY", 50, 10, "2026-01-02")
    trade(db, "BUY", 50, 11, "2026-01-03")
    trade(db, "SELL", 100, 12, "2026-01-05")
    service = PortfolioService(db, MockMarketDataProvider())
    service.matching.auto_fifo()
    assert [lot["buy_qty"] for lot in service.lots_rows()] == [50, 50]
    assert all(lot["sell_qty"] == 50 for lot in service.lots_rows())


def test_lifo_matches_newest_available_buy_first(db):
    early = trade(db, "BUY", 50, 10, "2026-01-02")
    late = trade(db, "BUY", 50, 11, "2026-01-03")
    sell = trade(db, "SELL", 60, 12, "2026-01-05")

    matcher = MatchingService(db)
    matcher.auto_lifo()

    assert [(match["buy_trade_id"], match["matched_qty"])
            for match in db.rows("trade_matches")] == [(late, 50), (early, 10)]
    assert matcher.remaining()[sell] == 0
    assert matcher.remaining()[early] == 40


def test_max_unrealized_gain_leaves_lowest_usd_cost_lot_open(db):
    low_cost = trade(db, "BUY", 100, 10, "2026-01-02")
    # Its lower trade price is outweighed by its allocated USD commission.
    high_cost = trade(db, "BUY", 100, 9, "2026-01-03", commission=200)
    sell = trade(db, "SELL", 50, 12, "2026-01-05")

    matcher = MatchingService(db)
    matcher.auto_max_unrealized_gain()

    assert [(match["buy_trade_id"], match["matched_qty"])
            for match in db.rows("trade_matches")] == [(high_cost, 50)]
    assert matcher.remaining()[sell] == 0
    assert matcher.remaining()[low_cost] == 100
    assert matcher.remaining()[high_cost] == 50


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


def test_position_is_derived_from_trades(db):
    db.add_position({"display_ticker": "XYZ", "position_qty": 110})
    trade(db, "BUY", 100, 10)
    service = PortfolioService(db, MockMarketDataProvider())
    row = service.portfolio_rows()[0]
    assert row["ledger_qty"] == 100
    assert row["position_qty"] == 100
    assert row["recorded_qty"] == 110
    assert row["reconciliation"] == "NOT CHECKED"
    assert db.rows("positions")[0]["position_qty"] == 110


def test_short_option_sale_is_closed_by_later_zero_price_cover(db):
    pid = db.add_position({"display_ticker": "ZZZ   240111P00050000", "ib_symbol": "ZZZ",
                           "security_type": "OPT", "multiplier": 100})
    db.add_trade({"position_id": pid, "trade_date": "2024-01-01", "side": "SELL",
                  "quantity": 1, "price": 10, "multiplier": 100, "commission_usd": 0})
    db.add_trade({"position_id": pid, "trade_date": "2024-01-11", "side": "BUY",
                  "quantity": 1, "price": 0, "multiplier": 100, "commission_usd": 0})
    service = PortfolioService(db, MockMarketDataProvider())
    service.matching.rebuild_auto([pid])
    match = db.rows("trade_matches")[0]
    assert match["origin"] == "auto_short"
    lot = service.lots_rows()[0]
    assert lot["lot_state"] == "Closed short"
    assert lot["realized_net"] == pytest.approx(1000)
    assert lot["duration"] == 10
    assert service.portfolio_rows()[0]["data_status"] != "UNMATCHED SALE"
    rows = {row["side"]: row for row in service.trade_rows()}
    assert rows["BUY"]["realized_net"] == pytest.approx(1000)
    assert rows["SELL"]["realized_net"] == pytest.approx(1000)


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


def test_open_cost_and_pnl_use_surviving_lot_fees(db):
    buy = trade(db, "BUY", 100, 10, commission=2)
    sell = trade(db, "SELL", 40, 12, "2026-01-05", commission=1)
    MatchingService(db).match(sell, [(buy, 40)])
    market = MockMarketDataProvider()
    position_id = db.rows("positions")[0]["id"]
    market.quotes[position_id] = {"ticks": {"last": 11}, "mode": "Live"}
    service = PortfolioService(db, market)
    row = service.portfolio_rows()[0]
    assert row["average_cost"] == pytest.approx(10.02)
    assert row["unrealized_pnl"] == pytest.approx(58.8)
    assert next(lot for lot in service.lots_rows() if lot["lot_state"] == "Closed")["realized_net"] == pytest.approx(78.2)


def test_trade_pnl_shows_realized_on_both_trades_and_open_on_remaining_buy(db):
    buy = trade(db, "BUY", 100, 10, commission=2)
    sell = trade(db, "SELL", 40, 12, "2026-01-05", commission=1)
    MatchingService(db).match(sell, [(buy, 40)])
    market = MockMarketDataProvider()
    position_id = db.rows("positions")[0]["id"]
    market.quotes[position_id] = {"ticks": {"last": 11}, "mode": "Live"}
    service = PortfolioService(db, market)

    rows = {row["id"]: row for row in service.trade_rows()}
    assert rows[buy]["realized_net"] == pytest.approx(78.2)
    assert rows[buy]["realized_pct"] == pytest.approx(78.2 / 400.8)
    assert rows[buy]["net_unrealized"] == pytest.approx(58.8)
    assert rows[buy]["unrealized_pct"] == pytest.approx(58.8 / 601.2)
    assert rows[sell]["realized_net"] == pytest.approx(78.2)
    assert rows[sell]["realized_pct"] == pytest.approx(78.2 / 400.8)
    assert rows[sell]["net_unrealized"] is None


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


def test_tiny_closed_quantity_does_not_count_as_open_holding(db):
    pid = db.add_position({"display_ticker": "ROUND", "ib_symbol": "ROUND"})
    first = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                          "quantity": 0.1, "price": 10})
    second = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                           "quantity": 0.2, "price": 10})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-03", "side": "SELL",
                         "quantity": 0.3, "price": 11})
    MatchingService(db).match(sell, [(first, 0.1), (second, 0.2)])
    row = PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]
    assert row["position_qty"] == 0
    assert row["average_cost"] is None
    assert row["unrealized_pnl"] is None


def test_saved_close_is_labeled_when_gateway_quote_is_missing(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
    db.cache_contract(pid, 12345)
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                  "quantity": 10, "price": 8, "commission_usd": 0})
    db.save_history(12345, [{"day": "2026-10-06", "close": 11, "volume": 100}])
    service = PortfolioService(db, MarketDataProvider())
    monkeypatch.setattr(service, "_as_of", lambda position: date(2026, 10, 7))
    row = service.portfolio_rows()[0]
    assert row["price_source"] == "CACHED CLOSE 2026-10-06"
    assert row["data_status"] == "CACHED CLOSE"
    assert row["value"] == 110
    assert row["unrealized_pnl"] == 30
    assert row["daily_pnl"] is None


def test_saved_current_session_bar_values_daily_and_open_lots_at_same_mark(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ",
                           "exchange": "NYSE"})
    db.cache_contract(pid, 12345)
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                  "quantity": 10, "price": 8, "commission_usd": 2})
    db.save_history(12345, [{"day": "2026-10-05", "close": 10, "volume": 100},
                            {"day": "2026-10-06", "close": 11, "volume": 100}])
    service = PortfolioService(db, MarketDataProvider())
    monkeypatch.setattr(service, "_as_of", lambda position: date(2026, 10, 6))
    row = service.portfolio_rows()[0]
    lot = next(lot for lot in service.lots_rows() if lot["lot_state"] == "Open")
    assert row["price_source"] == "SAVED SESSION BAR 2026-10-06"
    assert row["daily_pnl"] == 10
    assert row["unrealized_pnl"] == lot["net_unrealized"] == 28
    assert lot["lot_note"] == row["price_source"]


def test_previous_close_tick_does_not_imply_zero_daily_pnl(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                  "quantity": 10, "price": 8})
    market = MockMarketDataProvider()
    market.quotes[pid] = {"ticks": {"close": 10}, "mode": "Live"}
    service = PortfolioService(db, market)
    monkeypatch.setattr(service, "_as_of", lambda position: date(2026, 10, 6))
    row = service.portfolio_rows()[0]
    assert row["price_source"] == "CLOSE"
    assert row["daily_pnl"] is None
    assert row["one_day"] is None


def test_close_tick_supplies_prior_close_when_saved_history_is_missing_or_stale(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ",
                           "exchange": "NYSE"})
    db.cache_contract(pid, 12345)
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                  "quantity": 10, "price": 80})
    market = MockMarketDataProvider()
    market.quotes[pid] = {"ticks": {"last": 102, "close": 100},
                          "mode": "Live", "status": "OK"}
    service = PortfolioService(db, market)
    monkeypatch.setattr(service, "_as_of", lambda position: date(2026, 10, 6))
    row = service.portfolio_rows()[0]
    assert row["one_day"] == pytest.approx(0.02)
    assert row["daily_pnl"] == 20
    db.save_history(12345, [{"day": "2026-09-01", "close": 80, "volume": 100}])
    row = service.portfolio_rows()[0]
    assert row["one_day"] == pytest.approx(0.02)
    assert row["daily_pnl"] == 20
    db.save_history(12345, [{"day": "2026-10-05", "close": 95, "volume": 100}])
    row = service.portfolio_rows()[0]
    assert row["one_day"] == pytest.approx(0.02)
    assert row["daily_pnl"] == 20


def test_old_last_tick_cannot_report_a_new_session_move(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ",
                           "exchange": "NYSE"})
    db.cache_contract(pid, 12345, time_zone="America/New_York")
    db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                  "quantity": 10, "price": 80})
    market = MockMarketDataProvider()
    market.quotes[pid] = {"ticks": {"last": 102, "close": 100},
                          "tick_updated": {
                              "last": "2026-10-05T16:00:00-04:00",
                              "close": "2026-10-06T09:30:00-04:00"},
                          "mode": "Live", "status": "OK"}
    service = PortfolioService(db, market)
    monkeypatch.setattr(service, "_as_of", lambda position: date(2026, 10, 6))
    row = service.portfolio_rows()[0]
    assert row["one_day"] is None
    assert row["daily_pnl"] is None
    assert row["data_status"] == "STALE QUOTE"
    assert row["value"] == 1020


def test_quote_feed_keeps_each_tick_timestamp_until_disconnect():
    market = MarketDataProvider()
    market.events.put(("quote", [1], "last", 102, "Live"))
    market.events.put(("quote", [1], "close", 100, "Live"))
    market.drain()
    assert set(market.quote(1)["tick_updated"]) == {"last", "close"}
    assert datetime.fromisoformat(market.quote(1)["tick_updated"]["last"]).tzinfo is not None
    market.events.put(("connected", False))
    market.drain()
    assert market.quote(1)["tick_updated"] == {}


def test_manual_match_moves_conflicting_auto_allocation(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    early = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                          "quantity": 50, "price": 10, "commission_usd": 0})
    late = db.add_trade({"position_id": pid, "trade_date": "2026-01-05", "side": "BUY",
                         "quantity": 50, "price": 9, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 50, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    matcher.rebuild_auto([pid])
    # Cheaper (higher-gain) buy auto-matched first.
    assert [(m["buy_trade_id"], m["origin"]) for m in db.rows("trade_matches")] == [(late, "auto")]
    matcher.apply_position_matches(pid, [(early, sell, 50)])
    matches = db.rows("trade_matches")
    assert [(m["buy_trade_id"], m["sell_trade_id"], m["matched_qty"], m["origin"]) for m in matches] == [
        (early, sell, 50, "manual")]
    assert matcher.remaining()[late] == 50


def test_partial_manual_match_auto_fills_remainder(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    # buy2 has the worse price (lower gain) so the auto-fill remainder prefers
    # buy1's leftover quantity before reaching into buy2.
    buy1 = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                         "quantity": 30, "price": 10, "commission_usd": 0})
    buy2 = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                        "quantity": 30, "price": 11, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 40, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    matcher.apply_position_matches(pid, [(buy1, sell, 20)])
    matches = [m for m in db.rows("trade_matches")]
    buy1_matches = [m for m in matches if m["buy_trade_id"] == buy1]
    buy2_matches = [m for m in matches if m["buy_trade_id"] == buy2]
    assert any(m["origin"] == "manual" and m["matched_qty"] == 20 for m in buy1_matches)
    assert sum(m["matched_qty"] for m in buy1_matches) == 30  # Fully auto-filled remainder too.
    assert sum(m["matched_qty"] for m in buy2_matches) == 10
    assert all(m["origin"] == "auto" for m in buy2_matches)
    assert matcher.remaining()[sell] == 0


def test_clearing_manual_restores_auto_matching(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy1 = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                         "quantity": 50, "price": 10, "commission_usd": 0})
    buy2 = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                        "quantity": 50, "price": 9, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 50, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    matcher.apply_position_matches(pid, [(buy1, sell, 50)])
    assert db.rows("trade_matches")[0]["origin"] == "manual"
    matcher.apply_position_matches(pid, [])
    matches = db.rows("trade_matches")
    assert len(matches) == 1
    assert matches[0]["origin"] == "auto"
    assert matches[0]["buy_trade_id"] == buy2  # Cheaper buy wins on gain once unlocked.


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


def test_rebuild_auto_after_apply_preserves_manual_rows(db):
    pid = db.add_position({"display_ticker": "XYZ"})
    buy1 = db.add_trade({"position_id": pid, "trade_date": "2026-01-01", "side": "BUY",
                        "quantity": 50, "price": 10, "commission_usd": 0})
    buy2 = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                        "quantity": 50, "price": 9, "commission_usd": 0})
    sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-10", "side": "SELL",
                        "quantity": 50, "price": 12, "commission_usd": 0})
    matcher = MatchingService(db)
    matcher.apply_position_matches(pid, [(buy1, sell, 50)])
    matcher.rebuild_auto([pid])
    matches = db.rows("trade_matches")
    assert len(matches) == 1
    assert matches[0]["buy_trade_id"] == buy1
    assert matches[0]["origin"] == "manual"
    assert matcher.remaining()[buy2] == 50


def test_us_listing_without_contract_timezone_uses_market_calendar(db, monkeypatch):
    pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ",
                           "exchange": "NYSE", "currency": "USD"})
    db.cache_contract(pid, 12345)

    class FixedDatetime:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 6, 13, tzinfo=tz)

    monkeypatch.setattr(services_module, "datetime", FixedDatetime)
    service = PortfolioService(db, MarketDataProvider())
    assert service._as_of(db.rows("positions")[0]) == date(2026, 10, 6)
