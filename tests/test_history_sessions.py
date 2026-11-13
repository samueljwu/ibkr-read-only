"""Daily bars must finish in the instrument's trading timezone."""

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app.ui as ui
from app.finance.services import PortfolioService
from app.market.market_data import IBKRMarketDataProvider, MockMarketDataProvider
from app.market.session import completed_history_day
from app.storage.database import Database


def test_us_bar_is_not_complete_when_hong_kong_has_reached_tomorrow():
    position = {"currency": "USD", "exchange": "NASDAQ", "primary_exchange": ""}
    now = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)  # 02:00 HKT, 13:00 ET
    assert completed_history_day(position, now=now).isoformat() == "2026-11-08"
    assert completed_history_day(position, "Invalid/Zone", now).isoformat() == "2026-11-08"
    assert completed_history_day(position, now=datetime(
        2026, 11, 10, 14, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-09"
    assert completed_history_day({"currency": "JPY", "exchange": "SMART"}, now=now).isoformat() == "2026-11-07"


def test_broker_cache_does_not_erase_resolved_timezone(tmp_path):
    db = Database(tmp_path / "portfolio.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "TEST", "ib_symbol": "TEST"})
        db.cache_contract(position_id, 123, "America/New_York")
        db.cache_contract(position_id, 123, multiplier=1)
        assert db.conn.execute("SELECT time_zone FROM contracts WHERE position_id=?",
                               (position_id,)).fetchone()[0] == "America/New_York"
        db.cache_contract(position_id, 456)
        assert db.conn.execute("SELECT time_zone FROM contracts WHERE position_id=?",
                               (position_id,)).fetchone()[0] == ""
    finally:
        db.conn.close()


def test_known_contract_still_requests_missing_timezone(monkeypatch):
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17)
    provider.connected = True
    position = {"id": 1, "display_ticker": "TEST", "ib_symbol": "TEST",
                "security_type": "STK", "exchange": "NASDAQ",
                "primary_exchange": "", "currency": "USD",
                "con_id": 123, "multiplier": 1}
    requested = []
    subscribed = []
    monkeypatch.setattr(provider, "_subscribe", lambda position_id, contract:
                        subscribed.append((position_id, contract.conId)))
    monkeypatch.setattr(provider.client, "reqContractDetails", lambda request_id, contract:
                        requested.append((request_id, contract.conId)))
    provider.add_positions([position], missing_timezones={1})
    assert subscribed == [(1, 123)]
    assert len(requested) == 1 and requested[0][1] == 123
    request_id = requested[0][0]
    contract = SimpleNamespace(conId=123, multiplier="1", symbol="TEST", secType="STK",
                               currency="USD", exchange="NASDAQ", primaryExchange="",
                               localSymbol="", lastTradeDateOrContractMonth="", right="",
                               strike=0, tradingClass="")
    provider.client.found_details[request_id] = [SimpleNamespace(
        contract=contract, timeZoneId="America/New_York")]
    provider.client.contractDetailsEnd(request_id)
    assert provider.events.get()[3] == "America/New_York"
    provider._request_all({1})
    failed_request_id = requested[-1][0]
    provider.client.error(failed_request_id, 200, "Synthetic details unavailable")
    assert provider.events.empty()


def test_new_broker_position_requests_daily_history_on_existing_connection(monkeypatch):
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17)
    provider.connected = True
    position = {"id": 1, "display_ticker": "TEST", "ib_symbol": "TEST",
                "security_type": "STK", "exchange": "NASDAQ",
                "primary_exchange": "", "currency": "USD",
                "con_id": 123, "multiplier": 1}
    monkeypatch.setattr(provider.client, "reqMktData", lambda *_args: None)
    monkeypatch.setattr(provider.client, "reqContractDetails", lambda *_args: None)
    history_requests = []
    monkeypatch.setattr(provider.client, "reqHistoricalData",
                        lambda *args: history_requests.append(args))

    provider.add_positions([position])
    assert len(history_requests) == 1
    assert history_requests[0][1].conId == 123
    assert history_requests[0][2:5] == ("", "1 Y", "1 day")
    provider.add_positions([position])
    assert len(history_requests) == 1


def test_new_position_daily_pnl_uses_execution_without_prior_close(tmp_path):
    db = Database(tmp_path / "new-position.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "TEST", "ib_symbol": "TEST"})
        db.cache_contract(position_id, 123)
        market = MockMarketDataProvider()
        service = PortfolioService(db, market)
        session_day = service._as_of(db.rows("positions")[0])
        db.add_trade({"position_id": position_id, "trade_date": session_day.isoformat(),
                      "side": "BUY", "quantity": 10, "price": 105,
                      "fx_to_usd": 1, "commission_usd": 1})
        market.quotes[position_id] = {"ticks": {"last": 107}, "mode": "Live",
                                      "status": "OK"}
        row = service.portfolio_rows()[0]
        assert row["daily_pnl"] == 19
        assert row["one_day"] is None
        db.save_history(123, [{"day": (session_day - timedelta(days=1)).isoformat(),
                               "close": 100}])
        row = service.portfolio_rows()[0]
        assert row["one_day"] == pytest.approx(0.07)
    finally:
        db.conn.close()


def test_history_event_filters_incomplete_bar_and_tracks_session(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "portfolio.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "TEST", "ib_symbol": "TEST",
                                              "currency": "USD", "exchange": "NASDAQ"})
        window.db.cache_contract(position_id, 123)
        target = ["2026-11-08"]
        monkeypatch.setattr(ui, "completed_history_day", lambda *_args: date.fromisoformat(target[0]))
        window.market.events.put(("history", 123, [
            {"day": "2026-11-06", "close": 100},
            {"day": "2026-11-09", "close": 105}]))
        window._poll()
        assert [(r["day"], r["close"]) for r in window.db.history(123)] == [("2026-11-06", 100)]
        assert window.db.metadata("history_refresh_123") == "2026-11-08"
        assert window._stale_contracts() == {}
        target[0] = "2026-11-09"
        assert window._stale_contracts() == {123: "2026-11-09"}
    finally:
        window.close()
