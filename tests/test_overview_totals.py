import os
from datetime import date, timedelta

import pytest

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
        assert model.headerData(column, Qt.Horizontal) == "Open basis USD"
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


def test_overview_values_remaining_short_after_partial_cover(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "short-overview.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SHORT", "ib_symbol": "SHORT"})
        db.add_trade({"position_id": position, "trade_date": "2026-01-02", "side": "SELL",
                      "quantity": 10, "price": 12, "commission_usd": 2})
        db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "BUY",
                      "quantity": 4, "price": 10, "commission_usd": 0.5})
        MatchingService(db).rebuild_auto()
        db.set_portfolio_nav(1, 1_000)
        market = MockMarketDataProvider()
        market.quotes[position] = {"mode": "Mock", "ticks": {"last": 8}, "status": "MOCK"}
        service = PortfolioService(db, market)
        row = service.portfolio_rows()[0]
        assert row["position_qty"] == -6
        assert row["average_cost"] == pytest.approx(11.8)
        assert row["total_cost_usd"] == pytest.approx(70.8)
        assert row["value"] == pytest.approx(-48)
        assert row["portfolio_pct"] == pytest.approx(-0.048)
        assert row["unrealized_pnl"] == pytest.approx(22.8)
        assert row["unrealized_pct"] == pytest.approx(22.8 / 70.8)
        assert row["data_status"] == "OPEN SHORT"

        model = ui.GridModel("Portfolio", db, service)
        model.refresh([row])
        columns = {key: i for i, (key, _, _) in enumerate(model.columns)}
        assert "proceeds from open short sales" in model.data(
            model.index(0, columns["total_cost_usd"]), Qt.ToolTipRole)
        assert "liability" in model.data(model.index(0, columns["value"]), Qt.ToolTipRole)
        model.refresh([{**row, "open_pnl_fee_pending": True}])
        tooltip = model.data(model.index(0, columns["total_cost_usd"]), Qt.ToolTipRole)
        assert "open short sales" in tooltip and "pending fees" in tooltip
    finally:
        db.conn.close()


def test_overview_short_requires_price_for_open_pnl(tmp_path):
    db = Database(tmp_path / "short-no-price.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SHORT", "ib_symbol": "SHORT"})
        db.add_trade({"position_id": position, "trade_date": "2026-01-02", "side": "SELL",
                      "quantity": 2, "price": 12, "commission_usd": 1})
        row = PortfolioService(db, MockMarketDataProvider()).portfolio_rows()[0]
        assert row["position_qty"] == -2
        assert row["total_cost_usd"] == 23
        assert row["value"] is None
        assert row["unrealized_pnl"] is None
        assert row["unrealized_pct"] is None
        assert row["data_status"] == "OPEN SHORT"
    finally:
        db.conn.close()


def test_overview_short_uses_sale_fx_and_reports_missing_basis(tmp_path):
    db = Database(tmp_path / "short-fx.sqlite3")
    try:
        position = db.add_position({"display_ticker": "SHORT", "ib_symbol": "SHORT",
                                    "currency": "EUR"})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                             "side": "SELL", "quantity": 10, "price": 12,
                             "fx_to_usd": 1.2, "commission_usd": 2})
        db.set_fx_rate("EUR", date.today().isoformat(), 1.1)
        market = MockMarketDataProvider()
        market.quotes[position] = {"mode": "Mock", "ticks": {"last": 8}, "status": "MOCK"}
        service = PortfolioService(db, market)
        row = service.portfolio_rows()[0]
        assert row["total_cost_usd"] == pytest.approx(142)
        assert row["value"] == pytest.approx(-88)
        assert row["unrealized_pnl"] == pytest.approx(54)

        db.conn.execute("UPDATE trades SET fx_to_usd=0 WHERE id=?", (sale,))
        db.conn.commit()
        row = service.portfolio_rows()[0]
        assert row["position_qty"] == -10
        assert row["total_cost_usd"] is None
        assert row["unrealized_pnl"] is None
        assert row["data_status"] == "OPEN SHORT · BASIS UNAVAILABLE"
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


def test_overview_summary_nets_short_liability_against_long_holdings(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "net-short.sqlite3"))
    window = ui.MainWindow()
    try:
        rows = [
            {"display_ticker": "LONG", "position_qty": 10, "reconciliation": "OK",
             "value": 100, "daily_pnl": 5, "unrealized_pnl": 10,
             "last_px": 10, "fx_to_usd": 1},
            {"display_ticker": "SHORT", "position_qty": -6, "reconciliation": "OK",
             "value": -48, "daily_pnl": 3, "unrealized_pnl": 22.8,
             "last_px": 8, "fx_to_usd": 1},
        ]
        window._refresh_summary(rows, [])
        assert window.metric_values["holdings"].text() == "$52.00"
        assert "short liabilities" in window.metric_values["holdings"].toolTip()
        assert window.metric_values["open"].text() == "$32.80"
    finally:
        window.close()
