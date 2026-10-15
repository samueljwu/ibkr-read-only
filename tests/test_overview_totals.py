import os
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import app.ui as ui
from app.storage.database import Database
from app.finance.services import PortfolioService
from app.market.market_data import MockMarketDataProvider


def test_overview_shows_explicit_subtotals_when_one_open_holding_lacks_price(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "overview.sqlite3"))
    window = ui.MainWindow()
    try:
        monkeypatch.setattr(window, "_refresh_model", lambda name, rows=None: None)
        priced = {"display_ticker": "ABC", "position_qty": 10,
                  "reconciliation": "OK", "value": 110,
                  "daily_pnl": 10, "unrealized_pnl": 28,
                  "last_px": 11, "fx_to_usd": 1}
        missing = {"display_ticker": "OPTION", "position_qty": 1,
                   "reconciliation": "OK", "value": None,
                   "daily_pnl": None, "unrealized_pnl": None,
                   "last_px": None, "fx_to_usd": 1}
        monkeypatch.setattr(window.service, "portfolio_rows", lambda: [priced, missing])
        monkeypatch.setattr(window.service, "lots_rows", lambda rows=None: [])
        window._refresh_models()
        assert window.metric_values["holdings"].text() == "Partial $110.00"
        assert window.metric_values["daily"].text() == "Partial $10.00"
        assert window.metric_values["open"].text() == "Partial $28.00"
        assert "OPTION" in window.data_notice.text()
        assert "Subtotal only" in window.metric_values["open"].toolTip()
    finally:
        window.close()


def test_overview_daily_pnl_includes_position_closed_today(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "closed-today.sqlite3"))
    window = ui.MainWindow()
    try:
        rows = [
            {"display_ticker": "OPEN", "position_qty": 10, "reconciliation": "OK",
             "value": 110, "daily_pnl": 10, "unrealized_pnl": 10,
             "last_px": 11, "fx_to_usd": 1},
            {"display_ticker": "CLOSED", "position_qty": 0, "traded_today": True,
             "reconciliation": "OK", "value": 0, "daily_pnl": 99,
             "unrealized_pnl": None, "last_px": None, "fx_to_usd": 1},
        ]
        window._refresh_summary(rows, [])
        assert window.metric_values["daily"].text() == "$109.00"
        assert window.metric_values["holdings"].text() == "$110.00"
    finally:
        window.close()


def test_closed_today_position_has_daily_pnl_without_current_quote(tmp_path):
    db = Database(tmp_path / "closed-position.sqlite3")
    try:
        position_id = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.add_trade({"position_id": position_id,
                      "trade_date": (date.today() - timedelta(days=1)).isoformat(),
                      "side": "BUY", "quantity": 10, "price": 100})
        db.add_trade({"position_id": position_id, "trade_date": date.today().isoformat(),
                      "side": "SELL", "quantity": 10, "price": 110, "commission_usd": 1})
        market = MockMarketDataProvider()
        market.quotes[position_id] = {"mode": "Mock", "ticks": {}, "prior_close": 100}
        row = PortfolioService(db, market).portfolio_rows()[0]
        assert row["position_qty"] == 0
        assert row["traded_today"]
        assert row["daily_pnl"] == 99
    finally:
        db.conn.close()
