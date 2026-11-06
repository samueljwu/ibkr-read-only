"""Synthetic checks for sharing ledger work without changing projections."""

from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

from app.finance import services
from app.finance.services import PortfolioService
from app.market.market_data import MarketDataProvider, MockMarketDataProvider
from app.storage.database import Database


def test_shared_refresh_reads_each_ledger_table_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "shared-projection.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SYNTH"})
        db.add_trade({"position_id": position, "trade_date": "2026-01-02", "side": "BUY",
                      "quantity": 10, "price": 100})
        db.add_trade({"position_id": position, "trade_date": "2026-01-05", "side": "SELL",
                      "quantity": 4, "price": 110})
        other = db.add_position({"display_ticker": "OTHER"})
        db.add_trade({"position_id": other, "trade_date": "2026-01-02", "side": "BUY",
                      "quantity": 3, "price": 50})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        expected_portfolio = service.portfolio_rows()
        expected_lots = service.lots_rows(expected_portfolio)
        expected_trades = service.trade_rows(expected_lots)
        reads = Counter()
        original_rows = db.rows

        def counted_rows(table, *args, **kwargs):
            reads[table] += 1
            return original_rows(table, *args, **kwargs)

        monkeypatch.setattr(db, "rows", counted_rows)
        with service.shared_rows():
            portfolio = service.portfolio_rows()
            lots = service.lots_rows(portfolio)
            trades = service.trade_rows(lots)
        assert (portfolio, lots, trades) == (expected_portfolio, expected_lots, expected_trades)
        assert reads == Counter({"positions": 1, "trades": 1, "trade_matches": 1})
        assert service.portfolio_rows({position}) == [
            row for row in expected_portfolio if row["id"] == position]
        assert service.portfolio_rows(set()) == []
    finally:
        db.conn.close()


def test_quote_projection_reuses_inputs_and_matches_full_calculation(tmp_path, monkeypatch):
    db = Database(tmp_path / "quote-projection.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SYNTH", "ib_symbol": "SYNTH"})
        db.cache_contract(position, 123, multiplier=1)
        db.add_trade({"position_id": position, "trade_date": "2026-01-02", "side": "BUY",
                      "quantity": 10, "price": 100})
        bars = [{"day": (date.today() - timedelta(days=offset)).isoformat(),
                 "close": 100 + offset / 10, "volume": 1000}
                for offset in range(30, 0, -1)]
        db.save_history(123, bars)
        market = MarketDataProvider()
        market.quotes[position] = {"ticks": {"last": 110, "close": 105},
                                   "mode": "Live", "status": "OK", "updated": "2026-11-06T12:00:00"}
        service = PortfolioService(db, market)
        reads = Counter()
        original_rows, original_history = db.rows, db.history

        def counted_rows(table, *args, **kwargs):
            reads[table] += 1
            return original_rows(table, *args, **kwargs)

        def counted_history(con_id):
            reads["history"] += 1
            return original_history(con_id)

        monkeypatch.setattr(db, "rows", counted_rows)
        monkeypatch.setattr(db, "history", counted_history)
        assert service.quote_rows({position}) == service.portfolio_rows({position})
        reads.clear()
        for price in (111, 108, 115):
            market.quotes[position]["ticks"]["last"] = price
            cached = service.quote_rows({position})
            assert cached == service.portfolio_rows({position})
            assert cached[0]["last_px"] == price
        assert reads["history"] == 3  # Full reference calculations only.
        assert reads["trades"] == 3

        db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "BUY",
                      "quantity": 2, "price": 105})
        db.save_history(123, bars + [{"day": date.today().isoformat(),
                                      "close": 109, "volume": 1000}])
        service.invalidate_quote_cache()
        assert service.quote_rows({position}) == service.portfolio_rows({position})
        assert service.quote_rows({position})[0]["position_qty"] == 12

        external = Database(db.path)
        try:
            external.add_trade({"position_id": position, "trade_date": "2026-01-04",
                                "side": "BUY", "quantity": 1, "price": 106})
        finally:
            external.conn.close()
        assert service.quote_rows({position}) == service.portfolio_rows({position})
        assert service.quote_rows({position})[0]["position_qty"] == 13
    finally:
        db.conn.close()
