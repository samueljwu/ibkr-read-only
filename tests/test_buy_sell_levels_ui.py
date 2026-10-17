"""Synthetic stock history and executions for the Analytics chart."""

import os
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCharts import QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.storage.database import Database
from app.finance.services import PortfolioService
from app.market.market_data import MockMarketDataProvider
from app.views.analytics_ui import AnalyticsTab
from app.views.buy_sell_levels_ui import BuySellLevelsPanel, _marker_size
from app.views.chart_ui import price_scale
from app.views.ui_theme import COLORS


class FixedSplitHistory:
    def __init__(self, events=None, state="ready"):
        self._events = events
        self._state = state

    def events(self, _position, _first_trade_day, _changed):
        return self._events, self._state


def test_price_scale_uses_nonnegative_mental_intervals():
    assert price_scale([16, 91]) == (0, 100, 20, 0)
    assert price_scale([95, 115]) == (95, 115, 5, 0)
    low, high, step, decimals = price_scale([.12, .19])
    assert (low, high, step, decimals) == (0.12, 0.2, 0.02, 2)


def test_marker_size_tracks_notional_with_readable_bounds():
    assert _marker_size(100, 1000) == 8
    assert 8 < _marker_size(500, 1000) < 18
    assert _marker_size(1000, 1000) == 18


def test_levels_table_reuses_trade_and_lot_pnl_rows(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "levels-pnl.sqlite3")
    position_id = db.add_position({"display_ticker": "SYN", "ib_symbol": "SYN",
                                   "position_qty": 60, "currency": "USD"})
    buy_id = db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                           "side": "BUY", "quantity": 100, "price": 10,
                           "commission_usd": 2, "trade_id": "pnl-buy"})
    sell_id = db.add_trade({"position_id": position_id, "trade_date": "2026-01-05",
                            "side": "SELL", "quantity": 40, "price": 12,
                            "commission_usd": 1, "trade_id": "pnl-sell"})
    market = MockMarketDataProvider()
    market.quotes[position_id] = {"ticks": {"last": 9}, "mode": "Live"}
    service = PortfolioService(db, market)
    service.matching.match(sell_id, [(buy_id, 40)])
    portfolio = service.portfolio_rows()
    lots = service.lots_rows(portfolio)
    trades = service.trade_rows(lots)
    expected = {row["id"]: row for row in trades}
    calls = []
    panel = BuySellLevelsPanel(
        db, 1, portfolio_rows_provider=service.portfolio_rows,
        trade_rows_provider=lambda: calls.append(True) or service.trade_rows(),
        split_history=FixedSplitHistory({}))
    panel.refresh(portfolio_rows=portfolio, trade_rows=trades)
    assert calls == []
    for table_row, trade_id in ((0, sell_id), (1, buy_id)):
        row = expected[trade_id]
        for column, key in ((5, "realized_net"), (6, "realized_pct"),
                            (7, "net_unrealized"), (8, "unrealized_pct")):
            item = panel.table.item(table_row, column)
            assert item.raw_value == row[key]
            if key.endswith("pct") and row[key] is not None:
                assert item.text() == f"{row[key]:+,.2%}"
            if row[key] is not None:
                tone = "positive" if row[key] > 0 else "negative" if row[key] < 0 else "grid_neutral_text"
                assert item.foreground().color().name() == COLORS["light"][tone]
    assert panel.table.item(0, 7).text() == "—"
    assert panel.table.item(1, 7).raw_value is not None
    panel.set_theme("dark")
    assert panel.table.item(0, 5).foreground().color().name() == COLORS["dark"]["positive"]
    assert panel.table.item(1, 7).foreground().color().name() == COLORS["dark"]["negative"]
    panel.refresh()
    assert calls == [True]
    panel.close()
    db.conn.close()
    app.processEvents()


def test_stock_chart_uses_execution_prices_and_filters_positions(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "levels.sqlite3")
    stock_id = db.add_position({"display_ticker": "SYN", "ib_symbol": "SYN",
                                "position_qty": 5, "currency": "USD"})
    option_id = db.add_position({"display_ticker": "SYN OPT", "ib_symbol": "SYN",
                                 "security_type": "OPT", "position_qty": 1})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=12345 WHERE id=?", (stock_id,))
    db.save_history(12345, [{"day": "2026-10-01", "close": 100, "volume": 1000},
                            {"day": "2026-10-02", "close": 110, "volume": 900}])
    db.add_trade({"position_id": stock_id, "trade_date": "2026-10-01", "side": "BUY",
                  "quantity": 10, "price": 95, "trade_id": "synthetic-buy"})
    db.add_trade({"position_id": stock_id, "trade_date": "2026-10-02", "side": "SELL",
                  "quantity": 5, "price": 115, "trade_id": "synthetic-sell"})
    db.add_trade({"position_id": option_id, "trade_date": "2026-10-02", "side": "BUY",
                  "quantity": 1, "price": 4, "trade_id": "synthetic-option"})

    panel = BuySellLevelsPanel(db, 1, split_history=FixedSplitHistory({}))
    panel.refresh()
    assert panel.stock_combo.count() == 1
    assert panel.stock_combo.currentText() == "SYN"
    assert panel.table.rowCount() == 2
    assert panel.table.item(0, 1).text() == "SELL"
    series = panel.chart_view.chart().series()
    closes = next(item for item in series if isinstance(item, QLineSeries))
    assert [point.y() for point in closes.points()] == [100, 110]
    markers = [item for item in series if isinstance(item, QScatterSeries)]
    assert {item.name(): item.points()[0].y() for item in markers} == {"Buys": 95, "Sells": 115}
    sizes = {item.name(): item.pointConfiguration(0)[QScatterSeries.PointConfiguration.Size]
             for item in markers}
    assert sizes["Buys"] > sizes["Sells"] >= 8
    assert "sized by trade notional" in panel.note.text()
    axis = next(axis for axis in panel.chart_view.chart().axes() if isinstance(axis, QValueAxis))
    assert axis.tickCount() == 5
    assert (axis.min(), axis.max()) == (95, 115)
    assert panel.chart_view.chart().localizeNumbers()
    assert panel.chart_view.chart().locale().name() == "en_US"
    panel.axis_decimals_combo.setCurrentText("0")
    axis = next(axis for axis in panel.chart_view.chart().axes() if isinstance(axis, QValueAxis))
    assert axis.labelFormat() == "%.0f"
    assert db.setting("analytics_levels_axis_decimals_1") == "0"
    panel.resize(900, 600)
    panel.show()
    app.processEvents()
    chart = panel.chart_view.chart()
    x_axis = next(axis for axis in chart.axes(Qt.Horizontal))
    full_min, full_max = x_axis.min(), x_axis.max()
    panel.zoom_in_button.click()
    assert x_axis.max().toMSecsSinceEpoch() - x_axis.min().toMSecsSinceEpoch() < (
        full_max.toMSecsSinceEpoch() - full_min.toMSecsSinceEpoch())
    panel.zoom_reset_button.click()
    assert (x_axis.min(), x_axis.max()) == (full_min, full_max)
    for _ in range(12):
        panel.zoom_out_button.click()
    axis = next(axis for axis in panel.chart_view.chart().axes() if isinstance(axis, QValueAxis))
    assert axis.min() >= 0
    assert axis.tickAnchor() == 0
    assert axis.tickInterval() in (20, 50, 100)
    panel.zoom_reset_button.click()
    panel.zoom_out_button.click()
    assert x_axis.max().toMSecsSinceEpoch() - x_axis.min().toMSecsSinceEpoch() > (
        full_max.toMSecsSinceEpoch() - full_min.toMSecsSinceEpoch())
    panel.zoom_reset_button.click()
    assert (x_axis.min(), x_axis.max()) == (full_min, full_max)
    center = chart.plotArea().center().toPoint()
    wheel = QWheelEvent(QPointF(center), QPointF(panel.chart_view.viewport().mapToGlobal(center)),
                        QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                        Qt.ScrollUpdate, False)
    app.sendEvent(panel.chart_view.viewport(), wheel)
    assert x_axis.max().toMSecsSinceEpoch() - x_axis.min().toMSecsSinceEpoch() < (
        full_max.toMSecsSinceEpoch() - full_min.toMSecsSinceEpoch())
    panel.zoom_reset_button.click()
    QTest.mousePress(panel.chart_view.viewport(), Qt.LeftButton, pos=center)
    QTest.mouseMove(panel.chart_view.viewport(), center + QPoint(40, 0))
    QTest.mouseRelease(panel.chart_view.viewport(), Qt.LeftButton,
                       pos=center + QPoint(40, 0))
    assert x_axis.min() < full_min
    panel.zoom_reset_button.click()
    assert (x_axis.min(), x_axis.max()) == (full_min, full_max)
    service = SimpleNamespace(portfolio_rows=lambda: [{**db.rows("positions", 1)[0],
                                                        "position_qty": 5}],
                              matching=SimpleNamespace(trade_rows=lambda: db.rows("trades", 1)))
    analytics = AnalyticsTab(db, service, 1)
    analytics.sections.setCurrentWidget(analytics.levels_page)
    assert analytics.sections.tabText(analytics.sections.currentIndex()) == "Buy / sell levels"
    assert analytics.levels_page.chart_view.chart().series()
    analytics.levels_page.layout_combo.setCurrentText("Side by side")
    assert analytics.levels_page.splitter.orientation() == Qt.Horizontal
    assert analytics.view_state()["levels"]["orientation"] == "horizontal"
    analytics.close()
    panel.close()
    db.conn.close()
    app.processEvents()


def test_levels_layout_persists_both_orientations(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "layout.sqlite3")
    panel = BuySellLevelsPanel(db, 1)
    panel.resize(900, 600)
    panel.layout_combo.setCurrentText("Side by side")
    panel.axis_decimals_combo.setCurrentText("3")
    panel.show()
    app.processEvents()
    assert panel.splitter.orientation() == Qt.Horizontal
    assert panel.table.width() > 200
    assert panel.chart_view.width() > 300
    panel.splitter.moveSplitter(380, 1)
    horizontal_state = db.setting("analytics_levels_splitter_1_horizontal")
    assert horizontal_state
    panel.layout_combo.setCurrentText("Top and bottom")
    assert panel.splitter.orientation() == Qt.Vertical
    panel.layout_combo.setCurrentText("Side by side")
    assert bytes(panel.splitter.saveState().toBase64()).decode("ascii") == horizontal_state
    panel.close()
    reopened = BuySellLevelsPanel(db, 1)
    assert reopened.layout_combo.currentText() == "Side by side"
    assert reopened.splitter.orientation() == Qt.Horizontal
    assert reopened.axis_decimals_combo.currentText() == "3"
    reopened.apply_view_state({"axis_decimals": None})
    assert reopened.axis_decimals_combo.currentText() == "Auto"
    reopened.close()
    db.conn.close()
    app.processEvents()


def test_executions_and_closes_use_the_same_axis(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "split-basis.sqlite3")
    position_id = db.add_position({"display_ticker": "SPLIT", "ib_symbol": "SPLIT",
                                   "position_qty": 0, "currency": "USD"})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=24680 WHERE id=?", (position_id,))
    db.save_history(24680, [{"day": "2026-10-01", "close": 30, "volume": 1000},
                            {"day": "2026-10-02", "close": 31, "volume": 900}])
    db.add_trade({"position_id": position_id, "trade_date": "2026-10-01", "side": "BUY",
                  "quantity": 10, "price": 120, "trade_id": "split-buy"})
    db.add_trade({"position_id": position_id, "trade_date": "2026-10-02", "side": "SELL",
                  "quantity": 10, "price": 60, "trade_id": "split-sell"})
    panel = BuySellLevelsPanel(db, 1, split_history=FixedSplitHistory({}))
    panel.refresh()
    assert panel.stock_combo.currentText() == "SPLIT (closed)"
    panel.resize(900, 600)
    panel.show()
    app.processEvents()
    chart = panel.chart_view.chart()
    [price_axis] = chart.axes(Qt.Vertical)
    assert price_axis.max() >= 120
    buys = next(item for item in chart.series() if item.name().startswith("Buys"))
    assert chart.axes(Qt.Vertical, buys) == [price_axis]
    assert buys.points()[0].y() == 120
    full_range = (price_axis.min(), price_axis.max())
    panel.zoom_in_button.click()
    assert price_axis.max() - price_axis.min() < full_range[1] - full_range[0]
    panel.zoom_reset_button.click()
    assert (price_axis.min(), price_axis.max()) == full_range
    panel.close()
    unavailable = BuySellLevelsPanel(
        db, 1, split_history=FixedSplitHistory(None, "unavailable"))
    unavailable.refresh()
    assert not any(isinstance(item, QScatterSeries)
                   for item in unavailable.chart_view.chart().series())
    assert "markers are hidden" in unavailable.note.text()
    assert {unavailable.table.item(row, 3).raw_value
            for row in range(unavailable.table.rowCount())} == {120, 60}
    unavailable.close()
    db.conn.close()
    app.processEvents()


def test_recorded_split_events_align_markers_with_closes(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "split-adjusted.sqlite3")
    position_id = db.add_position({"display_ticker": "SPLIT", "ib_symbol": "SPLIT",
                                   "position_qty": 0, "currency": "USD"})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=24681 WHERE id=?", (position_id,))
    db.save_history(24681, [{"day": "2024-05-01", "close": 30, "volume": 1000},
                            {"day": "2025-01-01", "close": 31, "volume": 900}])
    db.add_trade({"position_id": position_id, "trade_date": "2024-05-01", "side": "BUY",
                  "quantity": 10, "price": 120, "trade_id": "split-buy"})
    db.add_trade({"position_id": position_id, "trade_date": "2024-06-11", "side": "BUY",
                  "quantity": 10, "price": 0, "trade_id": "split-event",
                  "source": "IBKR corporate action", "notes": "SPLIT 2 FOR 1"})
    db.add_trade({"position_id": position_id, "trade_date": "2025-01-01", "side": "SELL",
                  "quantity": 20, "price": 62, "trade_id": "split-sell"})
    db.set_setting(f"analytics_levels_splits_1_{position_id}", "2026-09-03 100:1")
    panel = BuySellLevelsPanel(db, 1, split_history=FixedSplitHistory({
        "2024-06-12": Decimal(2), "2026-09-03": Decimal(2)}))
    panel.refresh()
    assert not hasattr(panel, "split_button")
    chart = panel.chart_view.chart()
    assert len(chart.axes(Qt.Vertical)) == 1
    markers = [item for item in chart.series() if isinstance(item, QScatterSeries)]
    assert {item.name(): item.points()[0].y() for item in markers} == {
        "Buys": 30, "Sells": 31}
    assert {panel.table.item(row, 3).raw_value for row in range(panel.table.rowCount())
            if panel.table.item(row, 3)} >= {120, 62}
    stored_prices = {row["trade_id"]: row["price"] for row in db.rows("trades", 1)}
    assert stored_prices["split-buy"] == 120
    assert stored_prices["split-sell"] == 62
    assert "Yahoo split history verified" in panel.note.text()
    panel.close()
    db.conn.close()
    app.processEvents()


def test_same_side_marker_sizes_use_original_notional_after_split(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "notional-markers.sqlite3")
    position_id = db.add_position({"display_ticker": "SIZE", "ib_symbol": "SIZE",
                                   "position_qty": 11, "currency": "USD"})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=24682 WHERE id=?", (position_id,))
    db.save_history(24682, [{"day": "2024-05-01", "close": 50, "volume": 1000},
                            {"day": "2025-01-01", "close": 50, "volume": 900}])
    db.add_trade({"position_id": position_id, "trade_date": "2024-05-01", "side": "BUY",
                  "quantity": 1, "price": 100, "trade_id": "small-buy"})
    db.add_trade({"position_id": position_id, "trade_date": "2025-01-01", "side": "BUY",
                  "quantity": 10, "price": 50, "trade_id": "large-buy"})
    panel = BuySellLevelsPanel(
        db, 1, split_history=FixedSplitHistory({"2024-06-12": Decimal(2)}))
    panel.refresh()
    buys = next(item for item in panel.chart_view.chart().series()
                if isinstance(item, QScatterSeries) and item.name() == "Buys")
    assert [point.y() for point in buys.points()] == [50, 50]
    sizes = [buys.pointConfiguration(index)[QScatterSeries.PointConfiguration.Size]
             for index in range(2)]
    assert sizes[0] == 18
    assert sizes[1] < sizes[0]
    assert {panel.table.item(row, 3).raw_value for row in range(2)} == {100, 50}
    panel.close()
    db.conn.close()
    app.processEvents()


def test_stock_uses_overview_quantity_and_can_request_older_prices(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "older-levels.sqlite3")
    position_id = db.add_position({"display_ticker": "OLD", "ib_symbol": "OLD",
                                   "position_qty": 0})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=67890 WHERE id=?", (position_id,))
    db.save_history(67890, [{"day": "2025-10-01", "close": 100, "volume": 100}])
    db.add_trade({"position_id": position_id, "trade_date": "2021-12-01", "side": "BUY",
                  "quantity": 2, "price": 50, "trade_id": "synthetic-old-buy"})
    requests = []
    panel = BuySellLevelsPanel(
        db, 1, request_price_history=lambda position, first_saved_day:
        requests.append((position["con_id"], first_saved_day)) or True,
        portfolio_rows_provider=lambda: [{**db.rows("positions", 1)[0], "position_qty": 2}],
        split_history=FixedSplitHistory({}))
    panel.refresh()
    assert panel.stock_combo.currentText() == "OLD"
    assert panel.load_button.isEnabled()
    assert "earlier trades have no matching price line" in panel.note.text()
    panel.load_button.click()
    assert requests == [(67890, "2025-10-01")]
    assert "Requesting older daily prices" in panel.note.text()
    panel.close()
    db.conn.close()
    app.processEvents()


def test_recent_trade_does_not_hide_five_year_history_button(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "five-year-levels.sqlite3")
    position_id = db.add_position({"display_ticker": "NEW", "ib_symbol": "NEW",
                                   "position_qty": 1})
    with db.conn:
        db.conn.execute("UPDATE positions SET con_id=67891 WHERE id=?", (position_id,))
    first_saved = (date.today() - timedelta(days=365)).isoformat()
    db.save_history(67891, [{"day": first_saved, "close": 100, "volume": 100}])
    db.add_trade({"position_id": position_id, "trade_date": first_saved,
                  "side": "BUY", "quantity": 1, "price": 100,
                  "trade_id": "synthetic-recent-buy"})
    requests = []
    panel = BuySellLevelsPanel(
        db, 1, request_price_history=lambda position, first_day:
        requests.append((position["con_id"], first_day)) or True,
        split_history=FixedSplitHistory({}))
    panel.refresh()
    assert panel.load_button.isEnabled()
    panel.load_button.click()
    assert requests == [(67891, first_saved)]
    panel.set_history_complete(67891, False)
    assert not panel.load_button.isEnabled()
    assert "no earlier daily prices" in panel.note.text()
    panel.close()
    db.conn.close()
    app.processEvents()
