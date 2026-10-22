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




def test_traded_percent_uses_position_before_each_execution(db):
    position = db.add_position({"display_ticker": "AAPL", "ib_symbol": "AAPL"})
    other = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
    later = db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                          "side": "SELL", "quantity": 100, "price": 12})
    opening = db.add_trade({"position_id": position, "trade_date": "2026-01-01",
                            "side": "BUY", "quantity": 500, "price": 10})
    addition = db.add_trade({"position_id": position, "trade_date": "2026-01-04",
                             "side": "BUY", "quantity": 100, "price": 11})
    unrelated = db.add_trade({"position_id": other, "trade_date": "2026-01-02",
                              "side": "BUY", "quantity": 50, "price": 10})
    close = db.add_trade({"position_id": position, "trade_date": "2026-01-05",
                          "side": "SELL", "quantity": 500, "price": 12})
    new_opening = db.add_trade({"position_id": position, "trade_date": "2026-01-06",
                                "side": "BUY", "quantity": 25, "price": 10})

    rows = {row["id"]: row for row in MatchingService(db).trade_rows()}
    assert rows[opening]["traded_pct"] is None
    assert rows[later]["traded_pct"] == pytest.approx(.20)
    assert rows[addition]["traded_pct"] == pytest.approx(.25)
    assert rows[unrelated]["traded_pct"] is None
    assert rows[close]["traded_pct"] == pytest.approx(1.0)
    assert rows[new_opening]["traded_pct"] is None


def test_traded_percent_uses_execution_time_and_absolute_short_position(db):
    position = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
    cover = db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                          "executed_at": "2026-01-02 10:00:00", "side": "BUY",
                          "quantity": 10, "price": 9})
    short = db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                          "executed_at": "2026-01-02 09:00:00", "side": "SELL",
                          "quantity": 50, "price": 10})

    rows = {row["id"]: row for row in MatchingService(db).trade_rows()}
    assert rows[short]["traded_pct"] is None
    assert rows[cover]["traded_pct"] == pytest.approx(.20)


def test_traded_percent_uses_imported_position_opening(db):
    position = db.add_position({"display_ticker": "AAPL", "ib_symbol": "AAPL"})
    db.set_setting("ib_account_id", "U-SYNTHETIC")
    with db.conn:
        db.conn.execute("""INSERT INTO legacy_position_openings
            (portfolio_id,account_id,position_id,as_of,symbol,currency,quantity,source_hash)
            VALUES (1,'U-SYNTHETIC',?,'2026-01-02','AAPL','USD','500','synthetic')""",
            (position,))
    sale = db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                         "side": "SELL", "quantity": 100, "price": 12})
    row = next(row for row in MatchingService(db).trade_rows() if row["id"] == sale)
    assert row["traded_pct"] == pytest.approx(.20)


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
