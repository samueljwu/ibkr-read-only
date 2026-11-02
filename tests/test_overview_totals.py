import os
from datetime import date, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

import app.ui as ui
from app.storage.database import Database
from app.finance.services import MatchingService, PortfolioService
from app.market.market_data import MockMarketDataProvider


def test_overview_marks_estimated_csv_cost_in_tooltip(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "estimated-overview.sqlite3")
    try:
        model = ui.GridModel("Portfolio", db, PortfolioService(db, MockMarketDataProvider()))
        model.refresh([{"display_ticker": "TEST", "estimated_baseline": "2021-05-31",
                        "average_cost": 12, "unrealized_pct": 0.25,
                        "data_status": "OK · ESTIMATED CSV COST"}])
        column = next(i for i, (key, _, _) in enumerate(model.columns) if key == "average_cost")
        assert "not the original purchase cost or tax basis" in model.data(
            model.index(0, column), Qt.ToolTipRole)
        pct_column = next(i for i, (key, _, _) in enumerate(model.columns) if key == "unrealized_pct")
        assert model.data(model.index(0, pct_column), Qt.DisplayRole) == "25.00%"
        assert "not the original purchase cost or tax basis" in model.data(
            model.index(0, pct_column), Qt.ToolTipRole)
        total_column = next(i for i, (key, _, _) in enumerate(model.columns)
                            if key == "total_cost_usd")
        assert "not the original purchase cost or tax basis" in model.data(
            model.index(0, total_column), Qt.ToolTipRole)
    finally:
        db.conn.close()


def test_overview_total_cost_includes_remaining_buy_fees_at_acquisition_fx(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "total-cost.sqlite3")
    try:
        position = db.add_position({"display_ticker": "TEST", "ib_symbol": "TEST"})
        first = db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                              "side": "BUY", "quantity": 5, "price": 10,
                              "fx_to_usd": 0.5, "commission_usd": 3})
        db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                      "side": "BUY", "quantity": 2, "price": 20,
                      "fx_to_usd": 1.2, "commission_usd": 4})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-04",
                             "side": "SELL", "quantity": 2, "price": 30,
                             "fx_to_usd": 1, "commission_usd": 1})
        MatchingService(db).match(sale, [(first, 2)])
        service = PortfolioService(db, MockMarketDataProvider())
        row = service.portfolio_rows()[0]
        assert round(row["total_cost_usd"], 2) == 68.8  # 63 purchase notional + 5.8 allocated buy fees
        model = ui.GridModel("Portfolio", db, service)
        column = next(i for i, (key, _, _) in enumerate(model.columns)
                      if key == "total_cost_usd")
        assert model.headerData(column, Qt.Horizontal) == "Total cost USD"
        assert model.data(model.index(0, column)) == "68.80"
        assert "allocated buy fees" in model.data(model.index(0, column), Qt.ToolTipRole)
        model.refresh([{**row, "open_pnl_fee_pending": True}])
        assert "pending fees" in model.data(model.index(0, column), Qt.ToolTipRole)
    finally:
        db.conn.close()


def test_overview_total_cost_is_blank_when_open_lot_fx_is_missing(tmp_path):
    db = Database(tmp_path / "missing-cost-fx.sqlite3")
    try:
        position = db.add_position({"display_ticker": "TEST", "ib_symbol": "TEST"})
        db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                      "side": "BUY", "quantity": 2, "price": 20, "fx_to_usd": 0})
        assert PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]["total_cost_usd"] is None
    finally:
        db.conn.close()


def test_overview_places_total_cost_beside_open_cost(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "cost-column.sqlite3"))
    window = ui.MainWindow()
    try:
        view = window.views["Portfolio"]
        header = view.horizontalHeader()
        column = next(i for i, (key, _, _) in enumerate(ui.PORTFOLIO)
                      if key == "total_cost_usd")
        assert header.visualIndex(column) == header.visualIndex(2) + 1
        assert not view.isColumnHidden(column)
    finally:
        window.close()




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




def test_overview_daily_pnl_shows_percent_of_current_nav(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "daily-percent.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_portfolio_nav(window.portfolio_id, 1_000)
        rows = [{"display_ticker": "ABC", "position_qty": 10,
                 "reconciliation": "OK", "value": 200,
                 "daily_pnl": 25, "unrealized_pnl": 10,
                 "last_px": 20, "fx_to_usd": 1}]
        window._refresh_summary(rows, [])
        assert window.metric_values["daily"].text() == "$25.00 (2.50%)"
        assert "divided by current NAV" in window.metric_values["daily"].toolTip()

        rows.append({"display_ticker": "MISSING", "position_qty": 1,
                     "reconciliation": "OK", "value": None,
                     "daily_pnl": None, "unrealized_pnl": None,
                     "last_px": None, "fx_to_usd": 1})
        window._refresh_summary(rows, [])
        assert window.metric_values["daily"].text() == "Partial $25.00 (2.50%)"
    finally:
        window.close()
