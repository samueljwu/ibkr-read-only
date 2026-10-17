"""Synthetic checks for sharing ledger work without changing projections."""

from collections import Counter
from unittest.mock import patch

from app.finance import services
from app.finance.services import PortfolioService
from app.market.market_data import MockMarketDataProvider
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


def test_open_lots_share_instrument_return_window(tmp_path):
    db = Database(tmp_path / "lot-returns.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SYNTH"})
        for number in range(10):
            db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                          "trade_id": f"synthetic-{number}", "side": "BUY",
                          "quantity": 1, "price": 100})
        service = PortfolioService(db, MockMarketDataProvider())
        portfolio = service.portfolio_rows()
        with patch.object(services, "window_returns", wraps=services.window_returns) as returns:
            lots = service.lots_rows(portfolio)
        assert len(lots) == 10
        assert returns.call_count == 1
        assert all(lot["mtd"] == lots[0]["mtd"] and lot["ytd"] == lots[0]["ytd"]
                   for lot in lots)
    finally:
        db.conn.close()
