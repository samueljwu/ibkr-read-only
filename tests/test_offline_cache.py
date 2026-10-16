"""Offline quotes stay identifiable and never turn into current-session returns."""

import os
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app.ui as ui
import app.market.market_data as market_data
from app.storage.database import Database
from app.storage.encryption import open_database
from app.market.market_data import MarketDataProvider
from app.finance.services import PortfolioService


def test_quote_cache_survives_restart_and_is_scoped_to_account_and_contract(tmp_path):
    path = tmp_path / "offline.sqlite3"
    db = Database(path)
    position_id = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    db.cache_contract(position_id, 123)
    db.save_quotes(1, "ACCOUNT-A", [(position_id, 123, 42.5, "LIVE LAST", "2026-10-06T15:00:00")])
    db.conn.close()

    db = Database(path)
    try:
        assert db.cached_quotes(1, "ACCOUNT-A")[position_id]["price"] == 42.5
        assert db.cached_quotes(1, "ACCOUNT-B") == {}
        db.cache_contract(position_id, 456)
        assert db.cached_quotes(1, "ACCOUNT-A") == {}
    finally:
        db.conn.close()


def test_quote_cache_does_not_wait_for_another_writer(tmp_path):
    path = tmp_path / "locked.sqlite3"
    db = Database(path)
    position_id = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    other = open_database(path)
    try:
        other.execute("BEGIN IMMEDIATE")
        db.save_quotes(1, "ACCOUNT-A", [(position_id, None, 12, "LIVE LAST", "2026-10-06T15:00:00")])
        # The optional quote cache must be skipped rather than waiting on the
        # other writer, which still holds the lock.
        assert other.in_transaction
    finally:
        other.rollback()
        other.close()
        db.conn.close()


def test_disconnect_clears_live_ticks_and_keeps_last_observation():
    market = MarketDataProvider()
    market.connected = True
    market.events.put(("quote", [1], "last", 42.5, "Live"))
    market.drain()
    market.events.put(("connected", False))
    assert market.drain() == {1}
    quote = market.quote(1)
    assert quote["ticks"] == {}
    assert quote["cached_price"] == 42.5
    assert quote["mode"] == "Cached"


def test_market_data_mode_does_not_clear_subscription_error_without_a_price():
    market = MarketDataProvider()
    market.events.put(("status", [1], "IBKR ERROR 354: market data unavailable"))
    market.events.put(("quote", [1], None, None, "Live"))
    market.drain()
    assert market.quote(1)["status"] == "IBKR ERROR 354: market data unavailable"
    assert market.quote(1)["ticks"] == {}

    market.events.put(("quote", [1], "last", 13, "Live"))
    market.drain()
    assert market.quote(1)["status"] == "OK"


def test_stock_and_underlying_price_requests_do_not_require_fundamentals(monkeypatch):
    from ibapi.contract import Contract
    from app.market.market_data import IBKRMarketDataProvider

    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17)
    provider.positions = {1: {"con_id": 301}}
    provider.stale_con_ids = set()
    client = provider.client
    requests = []
    monkeypatch.setattr(client, "reqMktData", lambda *args: requests.append(args))
    monkeypatch.setattr(client, "reqContractDetails", lambda *args: None)
    contract = Contract()
    contract.conId = 301
    contract.symbol = "ABC"
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    provider._subscribe(1, contract)
    provider._subscribe_exposure_underlying(contract, 401)
    assert len(requests) == 2
    assert all(request[2] == "" for request in requests)

    monkeypatch.setattr(client, "reqMarketDataType", lambda mode: None)
    monkeypatch.setattr(client, "cancelMktData", lambda request_id: None)
    client.error(next(iter(client.ticker_ids)), 354, "Live data unavailable")
    assert len(requests) == 4
    assert all(request[2] == "" for request in requests)


def test_cached_quote_marks_value_but_not_daily_return(tmp_path):
    db = Database(tmp_path / "valuation.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                      "side": "BUY", "quantity": 2, "price": 10, "commission_usd": 0})
        market = MarketDataProvider()
        market.quotes[position_id] = {"ticks": {}, "mode": "Cached", "status": "CACHED QUOTE",
                                      "cached_price": 12, "cached_as_of": "2026-10-06T15:00:00"}
        row = PortfolioService(db, market).portfolio_rows()[0]
        assert row["value"] == 24
        assert row["price_source"] == "CACHED QUOTE 2026-10-06T15:00:00"
        assert row["data_status"] == "CACHED QUOTE"
        assert row["daily_pnl"] is None
        assert row["one_day"] is None
    finally:
        db.conn.close()


def test_ibkr_mode_without_api_uses_cache_and_never_mock(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "ui.sqlite3"
    seed = Database(path)
    position_id = seed.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    seed.cache_contract(position_id, 123)
    seed.set_setting("provider", "IBKR")
    seed.set_setting("ib_account_id", "ACCOUNT-A")
    seed.save_quotes(1, "ACCOUNT-A", [(position_id, 123, 42.5, "LIVE LAST", "2026-10-06T15:00:00")])
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(path))

    monkeypatch.setattr(market_data, "EWrapper", None)
    window = ui.MainWindow()
    try:
        row = window.models["Portfolio"].rows[0]
        assert row["last_px"] == 42.5
        assert row["data_status"] in ("CACHED QUOTE", "NO TRADES")
        assert not window.market.connected
        assert "Offline" in window.statusBar().currentMessage()
        window.refresh_market()
        assert window.models["Portfolio"].rows[0]["last_px"] == 42.5
    finally:
        window.close()


def test_live_tick_is_saved_and_disconnection_uses_cached_price(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "ticks.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(path))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.cache_contract(position_id, 123)
        window.db.set_setting("ib_account_id", "ACCOUNT-A")
        window._refresh_models()
        provider = ui.IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id="ACCOUNT-A")
        provider.connected = True
        window.market = provider
        window.service.market = provider
        provider.events.put(("quote", [position_id], "last", 42.5, "Live"))
        window._poll()
        assert window.db.cached_quotes(1, "ACCOUNT-A")[position_id]["price"] == 42.5

        provider.events.put(("connected", False))
        window._poll()
        row = window.models["Portfolio"].rows[0]
        assert row["last_px"] == 42.5
        assert row["price_source"].startswith("CACHED QUOTE")
    finally:
        window.close()


def test_live_label_and_daily_summary_wait_for_actual_quote(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "quote-state.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.cache_contract(position_id, 123)
        window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 1, "price": 10, "commission_usd": 0})
        window.db.save_history(123, [{"day": (date.today() - timedelta(days=1)).isoformat(),
                                      "close": 12, "volume": 100}])
        monkeypatch.setattr(window.service, "_as_of", lambda position: date.today())
        provider = MarketDataProvider()
        provider.connected = True
        provider.quotes[position_id] = {"ticks": {}, "mode": "Cached", "status": "CACHED QUOTE",
                                        "cached_price": 12, "cached_as_of": "2026-10-06T15:00:00"}
        window.market = window.service.market = provider
        window._refresh_models()
        assert "Saved prices; waiting for current quotes" in window.statusBar().currentMessage()
        assert window.metric_values["daily"].text() == "—"

        provider.events.put(("quote", [position_id], None, None, "Live"))
        provider.events.put(("status", [position_id], "IBKR ERROR 354: market data unavailable"))
        window._poll()
        assert "Saved prices; waiting for current quotes" in window.statusBar().currentMessage()
        assert "Quote issues: 1" in window.statusBar().currentMessage()
        assert "IBKR ERROR 354" in window.models["Portfolio"].rows[0]["data_status"]
        assert window.models["Portfolio"].rows[0]["one_day"] is None

        provider.events.put(("quote", [position_id], "last", 13, "Live"))
        window._poll()
        window._refresh_summary()
        assert "Data: Live (1/1)" in window.statusBar().currentMessage()
        assert window.models["Portfolio"].rows[0]["one_day"] == 13 / 12 - 1
        assert window.metric_values["daily"].text() == "$1.00"
    finally:
        window.close()


def test_historical_close_refreshes_daily_summary(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "history-summary.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.cache_contract(position_id, 123)
        window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 1, "price": 10, "commission_usd": 0})
        monkeypatch.setattr(window.service, "_as_of", lambda position: date.today())
        provider = MarketDataProvider()
        provider.connected = True
        provider.quotes[position_id] = {"ticks": {"last": 13}, "mode": "Live", "status": "OK"}
        window.market = window.service.market = provider
        window._refresh_models()
        assert window.metric_values["daily"].text() == "—"

        provider.events.put(("history", 123, [{"day": (date.today() - timedelta(days=1)).isoformat(),
                                                "close": 12, "volume": 100}]))
        window._poll()
        window._refresh_summary()
        assert window.metric_values["daily"].text() == "$1.00"
    finally:
        window.close()
