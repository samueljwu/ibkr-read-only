import json
import os
import sys
from types import SimpleNamespace
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QItemSelectionModel, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QKeyEvent, QKeySequence, QPainter, QPalette, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QStyle, QStyleOptionViewItem, QTableWidget, QToolButton, QVBoxLayout, QWidget

import app.ui as ui
from app.storage.database import Database
from app.views.ui_theme import COLORS, TOOLBAR_GROUP_SPACING
from app.views.shortcuts import native_text, shortcut


def test_grid_filter_reuses_sorted_source_until_data_or_order_changes(tmp_path, monkeypatch):
    db = Database(tmp_path / "grid-filter-cache.sqlite3")
    rows = [{"id": 1, "display_ticker": "ALFA", "position_qty": 10},
            {"id": 2, "display_ticker": "BETA", "position_qty": 0}]
    model = ui.GridModel("Portfolio", db, SimpleNamespace(portfolio_rows=lambda: rows))
    calls = []
    original_sort = model._sorted_rows

    def counted_sort(source):
        calls.append(True)
        return original_sort(source)

    monkeypatch.setattr(model, "_sorted_rows", counted_sort)
    try:
        model.set_filter("B")
        model.set_filter("BE")
        model.set_hide_closed(True)
        assert calls == []
        assert model.rows == []
        model.sort(1, Qt.DescendingOrder)
        assert len(calls) == 1
        model.set_filter("")
        assert [row["id"] for row in model.rows] == [1]
        assert len(calls) == 1
        model.update_positions({1}, [{**rows[0], "position_qty": 20}])
        model.set_filter("AL")
        assert len(calls) == 2
        assert model.rows[0]["position_qty"] == 20
    finally:
        db.conn.close()


def test_trades_grid_displays_position_traded_percent(tmp_path):
    db = Database(tmp_path / "traded-percent.sqlite3")
    try:
        rows = [{"id": 1, "display_ticker": "AAPL", "traded_pct": .2},
                {"id": 2, "display_ticker": "AAPL", "traded_pct": None}]
        model = ui.GridModel("Trades", db, SimpleNamespace(trade_rows=lambda: rows))
        column = next(i for i, (key, _, _) in enumerate(ui.TRADES) if key == "traded_pct")
        assert model.headerData(column, Qt.Horizontal) == "Position traded %"
        assert model.data(model.index(0, column)) == "20.00%"
        assert model.data(model.index(1, column)) == "—"
    finally:
        db.conn.close()


def test_trade_fee_refreshes_after_another_process_updates_shared_database(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db_path = tmp_path / "shared-fee.sqlite3"
    seed = Database(db_path)
    try:
        trade_id = seed.add_trade({"display_ticker": "ABC", "ib_symbol": "ABC",
                                   "trade_date": "2026-10-06", "side": "BUY",
                                   "quantity": 5, "price": 10, "commission_usd": 0})
        with seed.conn:
            seed.conn.execute("""INSERT INTO broker_executions
                (account_id,execution_key,trade_row_id,status)
                VALUES (?,?,?,'pending commission')""", ("U-SYNTHETIC", "EX.01", trade_id))
    finally:
        seed.conn.close()

    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    writer = Database(db_path)
    try:
        assert window.models["Trades"].rows[0]["commission_usd"] is None
        with writer.conn:
            writer.conn.execute("UPDATE trades SET commission_usd=? WHERE id=?", (0.5, trade_id))
            writer.conn.execute("UPDATE broker_executions SET status='complete' WHERE trade_row_id=?",
                                (trade_id,))
        assert window.models["Trades"].rows[0]["commission_usd"] is None
        window._periodic_refresh()
        assert window.models["Trades"].rows[0]["commission_usd"] == pytest.approx(0.5)
    finally:
        writer.conn.close()
        window.close()


def test_live_quote_refreshes_grid_pnl_when_tab_is_accessed(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "live-trade-pnl.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 10, "price": 10, "commission_usd": 0})
        window.market.quotes[position_id] = {"mode": "Mock", "ticks": {"last": 11}, "status": "MOCK"}
        window._refresh_models()
        assert window.models["Trades"].rows[0]["net_unrealized"] == pytest.approx(10)
        assert window.metric_values["open"].text() == "$10.00"

        window.market.events.put(("quote", [position_id], "last", 12, "Mock"))
        window._poll()
        assert window.lots_refresh_timer.isActive()
        def unexpected_lot_projection(*_args):
            raise AssertionError("Hidden lots tab rebuilt on a quote")

        with monkeypatch.context() as patch:
            patch.setattr(window.service, "lots_rows", unexpected_lot_projection)
            window.lots_refresh_timer.timeout.emit()
        assert window.metric_values["open"].text() == "$20.00"
        assert window.models["Trades"].rows[0]["net_unrealized"] == pytest.approx(10)
        window.tabs.setCurrentWidget(window.grid_tabs["Trades"])
        assert window.models["Trades"].rows[0]["net_unrealized"] == pytest.approx(20)

        window.market.events.put(("quote", [position_id], "last", 13, "Mock"))
        window._poll()
        window.lots_refresh_timer.timeout.emit()
        assert window.models["Trades"].rows[0]["net_unrealized"] == pytest.approx(30)
        assert window.metric_values["open"].text() == "$30.00"
        window.tabs.setCurrentWidget(window.grid_tabs["Lots"])
        assert window.models["Lots"].rows[0]["net_unrealized"] == pytest.approx(30)
        window.tabs.setCurrentWidget(window.grid_tabs["Portfolio"])
        assert window.models["Portfolio"].rows[0]["unrealized_pnl"] == pytest.approx(30)
    finally:
        window.close()


def test_header_actions_use_icons_with_accessible_labels(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "header_icons.sqlite3"))
    window = ui.MainWindow()
    try:
        buttons = (window.sync_button, window.pause_sync_button, window.theme_button,
                   window.help_button, window.settings_button)
        assert all(button.text() == "" and not button.icon().isNull() for button in buttons)
        assert [button.accessibleName() for button in buttons] == [
            "Sync IBKR", "Pause sync", "Switch to dark mode", "Help", "Settings"]
        assert all(button.toolTip() for button in buttons)
        image = window.settings_button.icon().pixmap(QSize(20, 20)).toImage()
        pixels = [(x, y) for y in range(image.height()) for x in range(image.width())
                  if image.pixelColor(x, y).alpha() > 0]
        assert abs((max(x for x, _ in pixels) - min(x for x, _ in pixels)) -
                   (max(y for _, y in pixels) - min(y for _, y in pixels))) <= 2
        window.theme_button.click()
        assert window.theme_button.accessibleName() == "Switch to light mode"
        assert all(not button.icon().isNull() for button in buttons)
        window._history_process = object()
        window._update_sync_button()
        assert window.sync_button.text() == ""
        assert window.sync_button.accessibleName() == "Importing Flex history…"
        assert not window.sync_button.isEnabled()
        window._history_process = None
    finally:
        window.close()


def test_zoom_menu_shortcuts_and_saved_level(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "zoom.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        window.show()
        app.processEvents()
        table = window.views["Portfolio"]
        base_row_height = table.verticalHeader().defaultSectionSize()
        base_font_size = window.font().pointSizeF()
        table.setFocus()
        QTest.keyClick(table, Qt.Key_Equal, Qt.ControlModifier)
        assert window.zoom_percent == 110
        assert table.verticalHeader().defaultSectionSize() > base_row_height
        assert window.font().pointSizeF() > base_font_size
        QTest.keyClick(table, Qt.Key_Minus, Qt.ControlModifier)
        assert window.zoom_percent == 100
        window.zoom_in_action.trigger()
        assert window.zoom_level_action.text() == "Zoom: 110%"
        QTest.keyClick(table, Qt.Key_0, Qt.ControlModifier)
        assert window.zoom_percent == 100
        window.tabs.setCurrentWidget(window.calcs_tab)
        window.calcs_tab.require_ibkr_ideas = False
        window.calcs_tab.idea_input.setText("NEW")
        window.calcs_tab.add_idea()
        assert "NEW" in window.calcs_tab.ideas
        idea_row = window.calcs_tab._row_for_position("idea:NEW")
        window.calcs_tab.table.select_cell(idea_row, 4, focus=True)
        QTest.keyClick(window.calcs_tab.table, Qt.Key_Space, Qt.ShiftModifier)
        assert len(window.calcs_tab.table.selectionModel().selectedRows()) == 1
        QTest.keyClick(window.calcs_tab.table, Qt.Key_Minus, Qt.ControlModifier)
        assert "NEW" not in window.calcs_tab.ideas
        assert window.zoom_percent == 100
        for ticker in ("ONE", "TWO"):
            window.calcs_tab.idea_input.setText(ticker)
            window.calcs_tab.add_idea()
        calcs_table = window.calcs_tab.table
        first = window.calcs_tab._row_for_position("idea:ONE")
        second = window.calcs_tab._row_for_position("idea:TWO")
        calcs_table.selectionModel().select(
            calcs_table.model().index(first, 0), QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        calcs_table.selectionModel().select(
            calcs_table.model().index(second, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows)
        assert len(calcs_table.selectionModel().selectedRows()) == 2
        calcs_table.setFocus()
        QTest.keyClick(calcs_table, Qt.Key_Minus, Qt.ControlModifier)
        assert not {"ONE", "TWO"} & window.calcs_tab.ideas.keys()
        assert window.zoom_percent == 100
        window.calcs_tab.idea_input.setText("PART")
        window.calcs_tab.add_idea()
        partial_row = window.calcs_tab._row_for_position("idea:PART")
        calcs_table.select_cell(partial_row, 4, focus=True)
        assert not calcs_table.selectionModel().selectedRows()
        QTest.keyClick(calcs_table, Qt.Key_Minus, Qt.ControlModifier)
        assert "PART" in window.calcs_tab.ideas
        assert window.zoom_percent == 90
        window.set_zoom(100)
        QTest.keyClick(calcs_table, Qt.Key_Space, Qt.ShiftModifier)
        assert len(calcs_table.selectionModel().selectedRows()) == 1
        window.zoom_out_action.trigger()
        assert "PART" in window.calcs_tab.ideas
        assert window.zoom_percent == 90
        window.set_zoom(100)
        window.calcs_tab.table.clearSelection()
        window.calcs_tab.table.setFocus()
        app.processEvents()
        QTest.keyClick(window.calcs_tab.table, Qt.Key_Minus, Qt.ControlModifier)
        assert window.zoom_percent == 90
        window.set_zoom(100)
        QTest.keyClick(window.calcs_tab.table, Qt.Key_Minus, Qt.ControlModifier | Qt.ShiftModifier)
        assert window.zoom_percent == 90
        window.set_zoom(150)
        assert not window.zoom_in_action.isEnabled()
        window.set_zoom(70)
        window.zoom_out_action.trigger()
        assert window.zoom_percent == 70
        window.set_zoom(120)
    finally:
        window.close()
    reopened = ui.MainWindow()
    try:
        assert reopened.zoom_percent == 120
        assert reopened.views["Portfolio"].verticalHeader().defaultSectionSize() == round(base_row_height * 1.2)
    finally:
        reopened.close()


def test_pause_button_pauses_active_job_and_resumes_pending_queue(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "pause_sync.sqlite3"))
    window = ui.MainWindow()
    class Process:
        code = None

        def poll(self):
            return self.code

    process = Process()
    launched = []
    try:
        window._sync_process = process
        window._sync_all_queue = [lambda **kwargs: launched.append("next")]
        window._sync_all_active = True
        pause_icon = window.pause_sync_button.icon().cacheKey()
        assert window.pause_sync_button.menu() is None
        window.pause_sync_button.click()
        assert window._sync_paused
        assert window.pause_sync_button.accessibleName() == "Resume sync"
        assert window.pause_sync_button.icon().cacheKey() != pause_icon
        assert window.pause_sync_button.isEnabled()
        assert not window.sync_button.isEnabled()
        assert window.sync_progress_label.text() == "Pausing sync…"
        window._poll()
        window._periodic_refresh()
        window.sync_all()
        assert launched == []
        process.code = 0
        window._poll()
        assert window.sync_progress_label.text() == "Sync paused"
        assert launched == []
        window.pause_sync_button.click()
        assert not window._sync_paused
        assert window.pause_sync_button.accessibleName() == "Pause sync"
        assert launched == ["next"]
        assert window.sync_button.isEnabled()
    finally:
        window.close()


def test_sync_pause_stops_live_provider_and_blocks_reconnect(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "pause_live.sqlite3"))
    window = ui.MainWindow()
    provider = ui.IBKRMarketDataProvider("127.0.0.1", 4001, 27)
    stopped = []
    restarted = []
    try:
        window.db.set_setting("provider", "IBKR")
        window.market = provider
        window.service.market = provider
        provider.connected = True
        monkeypatch.setattr(provider, "stop", lambda: stopped.append(True))
        monkeypatch.setattr(window, "_start_provider", lambda **kwargs: restarted.append(True))
        window.pause_sync_button.click()
        assert stopped == [True]
        assert not provider.connected
        window._periodic_refresh()
        window.refresh_market(force=True)
        assert restarted == []
        window.pause_sync_button.click()
        assert restarted == [True]
    finally:
        window.market = ui.MarketDataProvider()
        window.close()


def test_window_text_copy_menu_and_settings_paste(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "clipboard.sqlite3"))
    window = ui.MainWindow()
    try:
        assert window.metric_values["nav"].textInteractionFlags() & Qt.TextSelectableByMouse
        window.heading.setText("Sample heading")
        window.heading.setSelection(0, 6)
        window.heading.setFocus()
        QTest.keyClick(window.heading, Qt.Key_C, Qt.ControlModifier)
        assert app.clipboard().text() == "Sample"
        window.db.add_position({"display_ticker": "SAMPLE", "ib_symbol": "SAMPLE"})
        window._refresh_models()
        view = window.views["Portfolio"]
        view.setCurrentIndex(view.model().index(0, 0))
        menu = window._build_table_menu("Portfolio", QPoint(4, 4))
        copy = next(action for action in menu.actions() if action.text() == "Copy")
        assert copy.isEnabled()
        copy.trigger()
        assert app.clipboard().text() == "SAMPLE"

        field = window.settings_fields["ib_account_id"]
        app.clipboard().setText("DU123")
        field.setFocus()
        field.selectAll()
        QTest.keyClick(field, Qt.Key_V, Qt.ControlModifier)
        assert field.text() == "DU123"
    finally:
        window.close()


def test_mock_window_trade_derived_lots(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "ui.sqlite3"))
    window = ui.MainWindow()
    try:
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Overview", "Trades", "Lots", "Cash", "Analytics", "Rebalance"]
        window.db.add_position({"display_ticker": "ABC US Equity", "ib_symbol": "ABC"})
        window._refresh_models()
        model = window.models["Portfolio"]
        window.refresh_market()
        assert model.rows[0]["ib_symbol"] == "ABC"
        assert model.rows[0]["price_source"] == "MOCK"
        buy = window.db.add_trade({"trade_date": "2026-01-02", "display_ticker": "ABC US Equity",
                                   "side": "BUY", "quantity": 100, "price": 10})
        sell = window.db.add_trade({"trade_date": "2026-01-05", "display_ticker": "ABC US Equity",
                                    "side": "SELL", "quantity": 40, "price": 12})
        window.service.matching.match(sell, [(buy, 40)])
        window._refresh_models()
        assert len(window.models["Lots"].rows) == 2
        assert {row["lot_state"] for row in window.models["Lots"].rows} == {"Open", "Closed"}
        window.models["Lots"].set_lot_filter("Open")
        assert [row["lot_state"] for row in window.models["Lots"].rows] == ["Open"]
        window.models["Lots"].set_lot_filter("Closed")
        assert [row["lot_state"] for row in window.models["Lots"].rows] == ["Closed"]
    finally:
        window.close()


def test_rebalance_quote_refresh_waits_for_editor_focus_to_leave(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "rebalance_quote.sqlite3"))
    window = ui.MainWindow()
    try:
        tab = window.calcs_tab
        tab.ideas["NEW"] = {"con_id": 123, "symbol": "NEW", "security_type": "STK",
                            "currency": "USD", "price_usd": None}
        window.tabs.setCurrentWidget(tab)
        window.show()
        tab.refresh()
        app.processEvents()
        row = tab._row_for_position("idea:NEW")
        assert tab.table.item(row, 7).text() == "—"
        editor = tab.table.cellWidget(row, 4)
        editor.setFocus()
        app.processEvents()
        window.market.quotes["idea:NEW"] = {"ticks": {"last": 123}, "mode": "Live",
                                              "updated": "2026-10-08T10:00:00"}
        window._refresh_calcs_live()
        assert window.calcs_refresh_timer.isActive()
        assert tab.table.item(row, 7).text() == "—"
        tab.idea_input.setFocus()
        app.processEvents()
        window._refresh_calcs_live()
        row = tab._row_for_position("idea:NEW")
        assert tab.table.item(row, 7).text() == "123.00 USD"
    finally:
        window.close()


def test_special_tab_switch_reuses_current_portfolio_projection(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "tab-switch-cache.sqlite3"))
    window = ui.MainWindow()
    original_projection = window.service.portfolio_rows
    try:
        window.tabs.setCurrentWidget(window.grid_tabs["Portfolio"])
        window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.exposure_page)
        assert window._stream_rows is not None

        def unexpected_projection(*_args, **_kwargs):
            raise AssertionError("Tab switch recomputed portfolio rows")

        monkeypatch.setattr(window.service, "portfolio_rows", unexpected_projection)
        window.tabs.setCurrentWidget(window.calcs_tab)
        window.tabs.setCurrentWidget(window.analytics_tab)
    finally:
        monkeypatch.setattr(window.service, "portfolio_rows", original_projection)
        window.close()


def test_special_tab_switch_reads_external_changes_before_periodic_refresh(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db_path = tmp_path / "tab-switch-external.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    writer = Database(db_path)
    try:
        position_id = writer.add_position({"display_ticker": "SYNTH", "ib_symbol": "SYNTH"})
        writer.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                          "side": "BUY", "quantity": 1, "price": 10})
        window.tabs.setCurrentWidget(window.calcs_tab)
        assert window.calcs_tab.table.item(0, 0).text() == "SYNTH"
    finally:
        writer.conn.close()
        window.close()


def test_tab_order_can_be_saved_restored_and_keeps_view_actions_on_right_tab(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db_path = tmp_path / "tabs.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        assert window.tabs.isMovable()
        bar = window.tabs.tabBar()
        bar.moveTab(window._tab_index("Rebalance"), 0)
        assert bar.tabData(0) == "Rebalance"
        window.tabs.setCurrentIndex(window._tab_index("Trades"))
        assert window._current_table_name() == "Trades"
        window.save_view_action.trigger()
        assert window.db.setting("default_view_Trades")
        # Dragging a tab (tabMoved) and changing the active tab both persist
        # automatically via widgets.persist_tab_order; no manual save step.
        assert json.loads(window.db.setting(window.TAB_LAYOUT_KEY)) == {
            "version": 1, "order": ["Rebalance", "Portfolio", "Trades", "Lots", "Cash", "Analytics"],
            "active": "Trades"}
        bar.moveTab(0, window.tabs.count() - 1)
        window.tabs.setCurrentIndex(window._tab_index("Portfolio"))
        assert json.loads(window.db.setting(window.TAB_LAYOUT_KEY)) == {
            "version": 1, "order": ["Portfolio", "Trades", "Lots", "Cash", "Analytics", "Rebalance"],
            "active": "Portfolio"}
        window.reset_tab_order()
        assert [bar.tabData(i) for i in range(bar.count())] == list(window.DEFAULT_TAB_ORDER)
        assert json.loads(window.db.setting(window.TAB_LAYOUT_KEY))["order"] == list(window.DEFAULT_TAB_ORDER)
        bar.moveTab(window._tab_index("Rebalance"), 0)
        window.tabs.setCurrentIndex(window._tab_index("Trades"))
        assert window._current_table_name() == "Trades"
    finally:
        window.close()
    reopened = ui.MainWindow()
    try:
        bar = reopened.tabs.tabBar()
        assert [bar.tabData(i) for i in range(bar.count())] == [
            "Rebalance", "Portfolio", "Trades", "Lots", "Cash", "Analytics"]
        assert reopened._current_table_name() == "Trades"
    finally:
        reopened.close()


def test_tab_and_summary_keyboard_shortcuts_follow_visible_layout(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "shortcuts.sqlite3"))
    window = ui.MainWindow()
    try:
        window.show()
        app.processEvents()
        bar = window.tabs.tabBar()
        bar.moveTab(window._tab_index("Rebalance"), 0)
        window.tabs.setCurrentIndex(0)

        QTest.keyClick(window.tabs, Qt.Key_Tab, Qt.ControlModifier)
        assert bar.tabData(window.tabs.currentIndex()) == "Portfolio"
        window.views["Portfolio"].setFocus()
        QTest.keyClick(window.views["Portfolio"], Qt.Key_Tab,
                       Qt.ControlModifier | Qt.ShiftModifier)
        assert bar.tabData(window.tabs.currentIndex()) == "Rebalance"
        QTest.keyClick(window.tabs, Qt.Key_Tab, Qt.ControlModifier | Qt.ShiftModifier)
        assert bar.tabData(window.tabs.currentIndex()) == "Analytics"
        QTest.keyClick(window.tabs, Qt.Key_Tab, Qt.ControlModifier)
        assert bar.tabData(window.tabs.currentIndex()) == "Rebalance"

        window.tabs.setCurrentIndex(window._tab_index("Portfolio"))
        assert window.summary.isVisible()
        window.views["Portfolio"].setFocus()
        summary_modifier = Qt.ControlModifier | (Qt.ShiftModifier if sys.platform == "darwin" else Qt.NoModifier)
        QTest.keyClick(window.views["Portfolio"], Qt.Key_H, summary_modifier)
        assert not window.show_summary_action.isChecked()
        assert not window.summary.isVisible()
        assert window.db.setting("show_account_summary") == "0"
        QTest.keyClick(window.tabs, Qt.Key_H, summary_modifier)
        assert window.show_summary_action.isChecked()
        assert window.summary.isVisible()
        assert window.db.setting("show_account_summary") == "1"
        window.tabs.setCurrentWidget(window.calcs_tab)
        QTest.keyClick(window.tabs, Qt.Key_H, summary_modifier)
        assert not window.show_summary_action.isChecked()
        assert not window.summary.isVisible()
    finally:
        window.close()


def test_compact_summary_restores_window_when_tab_is_clicked(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "compact-summary.sqlite3"))
    window = ui.MainWindow()
    try:
        window.resize(1200, 700)
        window.show()
        app.processEvents()
        original = window.geometry()
        window.show_summary_action.setChecked(False)
        window.tabs.setCurrentWidget(window.cash_tab)

        compact_modifier = Qt.ControlModifier | (Qt.ShiftModifier if sys.platform == "darwin" else Qt.NoModifier)
        QTest.keyClick(window.tabs, Qt.Key_M, compact_modifier)
        app.processEvents()
        assert window.compact_summary_action.isChecked()
        assert window.tabs.currentIndex() == window._tab_index("Portfolio")
        assert window.summary.isVisible()
        assert window.tabs.tabBar().isVisible()
        assert not window.tabs.currentWidget().isVisible()
        assert window.height() < original.height()
        assert window.width() < original.width()
        assert window.height() <= window.sizeHint().height() + 2
        assert not window.statusBar().isVisible()

        QTest.mouseClick(window.tabs.tabBar(), Qt.LeftButton,
                         pos=window.tabs.tabBar().tabRect(window._tab_index("Portfolio")).center())
        app.processEvents()
        assert not window.compact_summary_action.isChecked()
        assert window.geometry() == original
        assert window.tabs.currentWidget().isVisible()
        assert window.statusBar().isVisible()
        assert not window.summary.isVisible()
        assert window.db.setting("show_account_summary") == "0"

        window.compact_summary_action.trigger()
        app.processEvents()
        QTest.keyClick(window.tabs, Qt.Key_M, compact_modifier)
        app.processEvents()
        assert not window.compact_summary_action.isChecked()
        assert window.geometry() == original

        window.compact_summary_action.trigger()
        app.processEvents()
        assert window.expand_full_view_action.isEnabled()
        QTest.keyClick(window.tabs, Qt.Key_L, compact_modifier)
        app.processEvents()
        assert not window.compact_summary_action.isChecked()
        assert not window.expand_full_view_action.isEnabled()
        assert window.geometry() == original

        window.compact_summary_action.trigger()
        app.processEvents()
        QTest.mouseClick(window.tabs.tabBar(), Qt.LeftButton,
                         pos=window.tabs.tabBar().tabRect(window._tab_index("Trades")).center())
        app.processEvents()
        assert not window.compact_summary_action.isChecked()
        assert window.tabs.currentIndex() == window._tab_index("Trades")
        assert window.geometry() == original

        window.compact_summary_action.trigger()
        QTest.keyClick(window.tabs, Qt.Key_Tab, Qt.ControlModifier)
        app.processEvents()
        assert not window.compact_summary_action.isChecked()
        assert window.tabs.currentIndex() == window._tab_index("Trades")
        assert window.geometry() == original

        window.showMaximized()
        app.processEvents()
        window.compact_summary_action.trigger()
        app.processEvents()
        assert not window.isMaximized()
        QTest.mouseClick(window.tabs.tabBar(), Qt.LeftButton,
                         pos=window.tabs.tabBar().tabRect(window._tab_index("Portfolio")).center())
        app.processEvents()
        assert window.isMaximized()
    finally:
        window.close()


def test_grid_toolbar_dropdowns_stay_grouped_and_aligned(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "toolbar-layout.sqlite3"))
    window = ui.MainWindow()
    try:
        window.resize(1500, 850)
        window.show()
        for name, combo in (("Trades", window.trade_side_combo),
                            ("Lots", window.lot_filter_combo)):
            window.tabs.setCurrentWidget(window.views[name].parentWidget())
            app.processEvents()
            toolbar = window.findChild(QWidget, f"{name}Toolbar")
            columns = next(button for button in toolbar.findChildren(QPushButton)
                           if button.text() == "Columns")
            assert combo.objectName() == columns.objectName() == "toolbarDropdown"
            assert combo.height() == columns.height() == 32
            assert columns.geometry().left() - combo.geometry().right() - 1 == TOOLBAR_GROUP_SPACING
            assert abs(columns.geometry().center().y() - combo.geometry().center().y()) <= 2
        window.tabs.setCurrentWidget(window.views["Portfolio"].parentWidget())
        app.processEvents()
        overview = window.findChild(QWidget, "PortfolioToolbar")
        assert any(label.text() == "Show" for label in overview.findChildren(QLabel))
        overview_columns = next(button for button in overview.findChildren(QPushButton)
                                if button.text() == "Columns")
        overview_gap = (window.hide_closed_checkbox.mapTo(overview, QPoint(0, 0)).x() -
                        overview_columns.mapTo(overview, QPoint(overview_columns.width(), 0)).x())
        window.tabs.setCurrentWidget(window.cash_tab)
        app.processEvents()
        assert overview_gap == TOOLBAR_GROUP_SPACING
        assert window.cash_tab.range_combo.objectName() == "toolbarDropdown"
        assert window.analytics_tab.range_combo.objectName() == "toolbarDropdown"
        assert window.analytics_tab.metric_combo.objectName() == "toolbarDropdown"
    finally:
        window.close()


def test_trade_side_and_instrument_filters_combine_with_lot_status(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "filters.sqlite3"))
    window = ui.MainWindow()
    try:
        abc = window.db.add_position({"display_ticker": "ABC US Equity", "ib_symbol": "ABC"})
        xyz = window.db.add_position({"display_ticker": "XYZ US Equity", "ib_symbol": "XYZ"})
        buy = window.db.add_trade({"position_id": abc, "trade_date": "2026-01-02",
                                   "side": "BUY", "quantity": 10, "price": 10})
        sell = window.db.add_trade({"position_id": abc, "trade_date": "2026-01-03",
                                    "side": "SELL", "quantity": 4, "price": 12})
        window.db.add_trade({"position_id": xyz, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 3, "price": 10})
        window.service.matching.match(sell, [(buy, 4)])
        window._refresh_models()
        trades = window.models["Trades"]
        lots = window.models["Lots"]
        assert len(trades.rows) == 3
        window.trade_side_combo.setCurrentText("SELL")
        assert [(row["display_ticker"], row["side"]) for row in trades.rows] == [("ABC US Equity", "SELL")]
        window.search_fields["Trades"].setText("xyz")
        assert trades.rows == []
        window.trade_side_combo.setCurrentText("BUY")
        assert [(row["display_ticker"], row["side"]) for row in trades.rows] == [("XYZ US Equity", "BUY")]
        window.search_fields["Lots"].setText("abc")
        assert {row["lot_state"] for row in lots.rows} == {"Open", "Closed"}
        window.lot_filter_combo.setCurrentText("Closed")
        assert [row["lot_state"] for row in lots.rows] == ["Closed"]
        window.search_fields["Lots"].setText("xyz")
        assert lots.rows == []
        window.lot_filter_combo.setCurrentText("Open")
        assert [(row["ticker"], row["lot_state"]) for row in lots.rows] == [("XYZ US Equity", "Open")]
    finally:
        window.close()


def test_hide_closed_positions_only_filters_overview_and_persists(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "hide-closed.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        closed = window.db.add_position({"display_ticker": "CLOSED", "ib_symbol": "CLOSED"})
        active = window.db.add_position({"display_ticker": "ACTIVE", "ib_symbol": "ACTIVE"})
        for position_id, side, quantity in ((closed, "BUY", 10), (closed, "SELL", 10),
                                            (active, "BUY", 5)):
            window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                                 "side": side, "quantity": quantity, "price": 10,
                                 "commission_usd": 0})
        window._refresh_models()
        portfolio = window.models["Portfolio"]
        assert {row["display_ticker"] for row in portfolio.rows} == {"CLOSED", "ACTIVE"}
        summary_before = {key: label.text() for key, label in window.metric_values.items()}
        trades_before = window.models["Trades"].rowCount()
        lots_before = window.models["Lots"].rowCount()
        window.hide_closed_checkbox.click()
        assert [row["display_ticker"] for row in portfolio.rows] == ["ACTIVE"]
        assert window.db.setting("hide_closed_positions") == "1"
        assert trades_before == window.models["Trades"].rowCount()
        assert lots_before == window.models["Lots"].rowCount()
        assert summary_before == {key: label.text() for key, label in window.metric_values.items()}
        window.search_fields["Portfolio"].setText("CLOSED")
        assert portfolio.rowCount() == 0
        window.hide_closed_checkbox.click()
        assert [row["display_ticker"] for row in portfolio.rows] == ["CLOSED"]
        window.search_fields["Portfolio"].clear()
        window.hide_closed_checkbox.click()
    finally:
        window.close()
    reopened = ui.MainWindow()
    try:
        assert reopened.hide_closed_checkbox.isChecked()
        assert [row["display_ticker"] for row in reopened.models["Portfolio"].rows] == ["ACTIVE"]
    finally:
        reopened.close()


def test_trades_are_read_only_and_selection_shortcuts_survive(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "grid.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        trade_id = window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                                        "side": "BUY", "quantity": 100, "price": 10, "commission_usd": 0})
        window._refresh_models()
        model, view = window.models["Trades"], window.views["Trades"]
        before = window.db.rows("trades", 1)
        assert view.editTriggers() == ui.QAbstractItemView.NoEditTriggers
        assert not model.flags(model.index(0, 4)) & Qt.ItemIsEditable
        assert not model.setData(model.index(0, 4), "11")
        view.setCurrentIndex(model.index(0, 3))
        app.clipboard().setText("55")
        for key, modifiers in ((Qt.Key_V, Qt.ControlModifier),
                               (Qt.Key_Return, Qt.ControlModifier),
                               (Qt.Key_Space, Qt.ShiftModifier)):
            view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, modifiers))
        assert window.db.rows("trades", 1) == before
        assert model.rows[0]["id"] == trade_id
        view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Space, Qt.ControlModifier))
        assert {index.column() for index in view.selectedIndexes()} == {3}
        view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Minus, Qt.ControlModifier))
        assert not view.isColumnHidden(3)
        assert window.db.rows("trades", 1) == before
        assert "Import" not in [button.text() for button in window.tabs.widget(1).findChildren(QPushButton)]
        assert "Add trade" not in [button.text() for button in window.findChildren(QPushButton)]
        assert all("Import" not in action.text() for action in window.menuBar().actions())
    finally:
        window.close()


def test_header_click_sorts_and_ctrl_click_selects_column(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "header-sort.sqlite3"))
    window = ui.MainWindow()
    try:
        for quantity in (10, 2, 100):
            position_id = window.db.add_position({"display_ticker": f"Q{quantity}",
                                                  "ib_symbol": f"Q{quantity}"})
            window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                                 "side": "BUY", "quantity": quantity, "price": 10,
                                 "commission_usd": 0})
        window._refresh_models()
        window.tabs.setCurrentIndex(1)
        window.show()
        app.processEvents()
        model, view = window.models["Trades"], window.views["Trades"]
        header = view.horizontalHeader()
        point = QPoint(header.sectionViewportPosition(3) + header.sectionSize(3) // 2,
                       header.height() // 2)
        QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=point)
        assert [row["quantity"] for row in model.rows] == [2, 10, 100]
        assert not view.selectedIndexes()
        assert header.isSortIndicatorShown() and header.sortIndicatorSection() == 3
        QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=point)
        assert [row["quantity"] for row in model.rows] == [100, 10, 2]
        QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.ControlModifier, pos=point)
        assert [row["quantity"] for row in model.rows] == [100, 10, 2]
        assert {index.column() for index in view.selectedIndexes()} == {3}
        assert len(view.selectedIndexes()) == 3
        window._refresh_models()
        assert [row["quantity"] for row in model.rows] == [100, 10, 2]
    finally:
        window.close()


def test_refresh_preserves_scrolled_table_with_selected_row(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "scroll.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "MANY", "ib_symbol": "MANY"})
        for _ in range(40):
            window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                                 "side": "BUY", "quantity": 1, "price": 10,
                                 "commission_usd": 0})
        window._refresh_models()
        window.resize(800, 600)
        window.show()
        for tab, name in ((1, "Trades"), (2, "Lots")):
            window.tabs.setCurrentIndex(tab)
            app.processEvents()
            view = window.views[name]
            view.setCurrentIndex(view.model().index(0, 0))
            view.selectRow(0)
            vertical = view.verticalScrollBar()
            horizontal = view.horizontalScrollBar()
            assert vertical.maximum() > 0
            vertical.setValue(vertical.maximum() // 2)
            horizontal.setValue(horizontal.maximum() // 2)
            app.processEvents()
            before = (vertical.value(), horizontal.value())
            assert before[0] > 0
            window._refresh_model(name)
            app.processEvents()
            assert (vertical.value(), horizontal.value()) == before
            if name == "Lots":
                wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(),
                                    QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                                    Qt.ScrollUpdate, False)
                app.sendEvent(view.viewport(), wheel)
                app.processEvents()
                assert vertical.value() > before[0]
                before = (vertical.value(), horizontal.value())
            window._refresh_models()
            app.processEvents()
            assert (vertical.value(), horizontal.value()) == before
            assert view.currentIndex().row() == 0
            if name == "Lots":
                window.market.events.put(("quote", [position_id], "last", 20, "Mock"))
                window._poll()
                assert window.lots_refresh_timer.isActive()
                window.lots_refresh_timer.timeout.emit()
                assert (vertical.value(), horizontal.value()) == before
    finally:
        window.close()


def test_overview_bars_and_lots_open_pnl_values(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "move-bars.sqlite3"))
    window = ui.MainWindow()
    try:
        model = window.models["Portfolio"]
        model.beginResetModel()
        model.rows = [
            {"id": 1, "display_ticker": "GAIN", "unrealized_pnl": 12345.67,
             "one_day": 0.04, "mtd": -0.10, "ytd": 0.25},
            {"id": 2, "display_ticker": "LOSS", "unrealized_pnl": -23456.78,
             "one_day": -0.02, "mtd": 0.05, "ytd": -0.50},
        ]
        model._update_bar_scales()
        model.endResetModel()
        assert model.bar_scales == {"unrealized_pnl": 23456.78,
                                    "one_day": 0.04, "mtd": 0.10, "ytd": 0.50}
        view = window.views["Portfolio"]
        for column in (6, 9, 10, 11):
            assert not view.isColumnHidden(column)
            assert isinstance(view.itemDelegateForColumn(column), ui.SignedBarDelegate)
            index = model.index(0, column)
            assert model.data(index, Qt.DisplayRole).endswith("%") if column != 6 else model.data(index, Qt.DisplayRole) == "12,345.67"
            assert "largest absolute value" in model.headerData(column, Qt.Horizontal, Qt.ToolTipRole)

        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 140, 25)
        option.state = QStyle.State_Enabled
        option.palette = view.palette()
        option.font = view.font()
        option.widget = view
        for theme, gain, loss in (("light", ui.COLORS["light"]["bar_positive"], ui.COLORS["light"]["bar_negative"]),
                                  ("dark", ui.COLORS["dark"]["bar_positive"], ui.COLORS["dark"]["bar_negative"])):
            window._apply_theme(theme)
            option.palette = view.palette()
            for column in (6, 9):
                for row, expected_color, side in ((0, gain, "right"), (1, loss, "left")):
                    image = QImage(140, 25, QImage.Format_ARGB32)
                    image.fill(QColor("#ffffff"))
                    painter = QPainter(image)
                    view.itemDelegateForColumn(column).paint(painter, option, model.index(row, column))
                    painter.end()
                    colored = [x for x in range(140) if image.pixelColor(x, 20).name() == expected_color]
                    assert colored
                    if side == "right":
                        assert min(colored) > 69
                    else:
                        assert max(colored) < 69

        lots = window.models["Lots"]
        lots.beginResetModel()
        lots.rows = [{"buy_trade_id": 1, "net_unrealized": 3000.50},
                     {"buy_trade_id": 2, "net_unrealized": -1500.25},
                     {"buy_trade_id": 3, "net_unrealized": None}]
        lots._update_bar_scales()
        lots.endResetModel()
        assert lots.bar_scales == {}
        lot_view = window.views["Lots"]
        assert not isinstance(lot_view.itemDelegateForColumn(8), ui.SignedBarDelegate)
        assert lots.headerData(8, Qt.Horizontal, Qt.ToolTipRole) is None
        assert lots.data(lots.index(0, 8), Qt.DisplayRole) == "3,000.50"
        assert lots.data(lots.index(1, 8), Qt.DisplayRole) == "-1,500.25"
        assert lots.data(lots.index(2, 8), Qt.DisplayRole) == "—"
    finally:
        window.close()


def test_saved_default_views_restore_layout_filters_and_sort(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "default-view.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        window.show()
        app.processEvents()
        overview = window.views["Portfolio"]
        header = overview.horizontalHeader()
        header.moveSection(header.visualIndex(4), 0)
        overview.setColumnWidth(4, 175)
        overview.hideColumn(8)
        window.search_fields["Portfolio"].setText("AAPL")
        window.hide_closed_checkbox.setChecked(True)
        window._sort_table("Portfolio", 1, Qt.DescendingOrder)
        window.models["Portfolio"].set_column_decimals(1, 4)
        window.save_default_view("Portfolio")
        window.search_fields["Trades"].setText("AAPL")
        window.trade_side_combo.setCurrentText("SELL")
        window._sort_table("Trades", 3, Qt.DescendingOrder)
        window.models["Trades"].set_column_decimals(3, 0)
        window.save_default_view("Trades")
        window.lot_filter_combo.setCurrentText("Closed")
        window.models["Lots"].set_column_decimals(8, 5)
        window.save_default_view("Lots")

        window.search_fields["Portfolio"].clear()
        window.hide_closed_checkbox.setChecked(False)
        overview.showColumn(8)
        header.moveSection(header.visualIndex(4), 4)
        window._sort_table("Portfolio", 0, Qt.AscendingOrder)
        window.models["Portfolio"].set_column_decimals(1, 2)
        window.search_fields["Trades"].clear()
        window.trade_side_combo.setCurrentText("All")
        window.models["Trades"].set_column_decimals(3, 2)
        window.lot_filter_combo.setCurrentText("All")
        window.models["Lots"].set_column_decimals(8, 2)
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        overview = reopened.views["Portfolio"]
        assert overview.horizontalHeader().visualIndex(4) == 0
        assert overview.columnWidth(4) == 175
        assert overview.isColumnHidden(8)
        assert reopened.search_fields["Portfolio"].text() == "AAPL"
        assert reopened.hide_closed_checkbox.isChecked()
        assert reopened.models["Portfolio"].sort_column == 1
        assert reopened.models["Portfolio"].sort_order == Qt.DescendingOrder
        assert reopened.models["Portfolio"].decimals_for(1) == 4
        assert reopened.search_fields["Trades"].text() == "AAPL"
        assert reopened.trade_side_combo.currentText() == "SELL"
        assert reopened.models["Trades"].sort_column == 3
        assert reopened.models["Trades"].sort_order == Qt.DescendingOrder
        assert reopened.models["Trades"].decimals_for(3) == 0
        assert reopened.lot_filter_combo.currentText() == "Closed"
        assert reopened.models["Lots"].decimals_for(8) == 5
        reopened.search_fields["Portfolio"].clear()
        assert reopened.restore_default_view("Portfolio")
        assert reopened.search_fields["Portfolio"].text() == "AAPL"
        old_view = json.loads(reopened.db.setting("default_view_Portfolio"))
        old_view.pop("column_decimals")
        reopened.db.set_setting("default_view_Portfolio", json.dumps(old_view))
        reopened.models["Portfolio"].set_column_decimals(1, 6)
        assert reopened.restore_default_view("Portfolio")
        assert reopened.models["Portfolio"].decimals_for(1) == 2
    finally:
        reopened.close()


def test_cash_tab_view_menu_saves_and_restores_cash_view(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "cash-default-view.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        window.show()
        window.tabs.setCurrentWidget(window.cash_tab)
        app.processEvents()
        view_action = next(action for action in window.menuBar().actions() if action.text() == "View")
        menu = view_action.menu()
        menu.aboutToShow.emit()
        assert window._current_table_name() == "Cash"
        assert window.save_view_action.isEnabled()
        assert not window.restore_view_action.isEnabled()

        cash = window.cash_tab
        cash.range_combo.setCurrentText("Custom")
        cash.start_date.setDate(QDate(2026, 1, 2))
        cash.end_date.setDate(QDate(2026, 2, 3))
        header = cash.table.horizontalHeader()
        header.moveSection(header.visualIndex(1), 0)
        cash.table.setColumnWidth(1, 220)
        cash.table.set_column_decimals(4, 3)
        cash.table.sortItems(4, Qt.DescendingOrder)
        window.save_view_action.trigger()
        assert json.loads(window.db.setting("default_view_Cash"))["range"] == "Custom"

        cash.range_combo.setCurrentText("MTD")
        header.moveSection(header.visualIndex(1), 1)
        cash.table.setColumnWidth(1, 170)
        cash.table.set_column_decimals(4, 2)
        cash.table.sortItems(0, Qt.AscendingOrder)
        menu.aboutToShow.emit()
        assert window.restore_view_action.isEnabled()
        window.restore_view_action.trigger()
        assert cash.range_combo.currentText() == "Custom"
        assert cash.start_date.date() == QDate(2026, 1, 2)
        assert cash.end_date.date() == QDate(2026, 2, 3)
        assert header.visualIndex(1) == 0
        assert cash.table.columnWidth(1) == 220
        assert cash.table.decimals_for(4) == 3
        assert header.sortIndicatorSection() == 4
        assert header.sortIndicatorOrder() == Qt.DescendingOrder
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        cash = reopened.cash_tab
        assert cash.range_combo.currentText() == "Custom"
        assert cash.start_date.date() == QDate(2026, 1, 2)
        assert cash.end_date.date() == QDate(2026, 2, 3)
        assert cash.table.horizontalHeader().visualIndex(1) == 0
        assert cash.table.columnWidth(1) == 220
        assert cash.table.decimals_for(4) == 3
        assert cash.table.horizontalHeader().sortIndicatorSection() == 4
    finally:
        reopened.close()


def test_analytics_and_rebalance_view_menu_save_restore_after_restart(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "all-tab-views.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        window.show()
        view_action = next(action for action in window.menuBar().actions()
                           if action.text() == "View")
        view_menu = view_action.menu()
        analytics = window.analytics_tab
        window.tabs.setCurrentWidget(analytics)
        analytics.range_combo.setCurrentText("Custom")
        analytics.start_date.setDate(QDate(2026, 1, 2))
        analytics.end_date.setDate(QDate(2026, 2, 3))
        analytics.metric_combo.setCurrentText("NAV")
        analytics.capital_combo.setCurrentText("Cash / portfolio")
        analytics.benchmark_toggle.setChecked(False)
        analytics.pnl_layout_combo.setCurrentText("Side by side")
        analytics.capital_layout_combo.setCurrentText("Side by side")
        analytics.pnl_table.set_column_decimals(1, 3)
        analytics.pnl_table.hideColumn(3)
        analytics.pnl_table.sortItems(1, Qt.AscendingOrder)
        analytics.cash_share_table.set_column_decimals(3, 4)
        analytics.exposure_page.group_combo.setCurrentText("Custom")
        analytics.exposure_page.basis_combo.setCurrentText("Market value")
        analytics.exposure_page.positions.set_column_decimals(3, 4)
        analytics.exposure_page.positions.sortItems(0, Qt.AscendingOrder)
        analytics.sections.setCurrentWidget(analytics.exposure_page)
        app.processEvents()
        view_menu.aboutToShow.emit()
        assert window._current_table_name() == "Analytics"
        assert window.save_view_action.isEnabled()
        assert not window.restore_view_action.isEnabled()
        window.save_view_action.trigger()
        assert json.loads(window.db.setting("default_view_Analytics"))["section"] == 3

        analytics.sections.setCurrentWidget(analytics.pnl_page)
        analytics.range_combo.setCurrentText("3 months")
        analytics.metric_combo.setCurrentText("Adjusted performance")
        analytics.pnl_layout_combo.setCurrentText("Top and bottom")
        analytics.capital_layout_combo.setCurrentText("Top and bottom")
        analytics.pnl_table.set_column_decimals(1, 2)
        analytics.pnl_table.showColumn(3)
        analytics.cash_share_table.set_column_decimals(3, 2)
        analytics.exposure_page.group_combo.setCurrentText("Sector")
        analytics.exposure_page.basis_combo.setCurrentText("Adjusted")
        view_menu.aboutToShow.emit()
        assert window.restore_view_action.isEnabled()
        window.restore_view_action.trigger()
        assert analytics.sections.currentWidget() is analytics.exposure_page
        assert analytics.range_combo.currentText() == "Custom"
        assert analytics.start_date.date() == QDate(2026, 1, 2)
        assert analytics.end_date.date() == QDate(2026, 2, 3)
        assert analytics.metric_combo.currentText() == "NAV"
        assert analytics.capital_combo.currentText() == "Cash / portfolio"
        assert not analytics.benchmark_toggle.isChecked()
        assert analytics.pnl_layout_combo.currentText() == "Side by side"
        assert analytics.capital_layout_combo.currentText() == "Side by side"
        assert analytics.pnl_table.decimals_for(1) == 3
        assert analytics.cash_share_table.decimals_for(3) == 4
        assert analytics.pnl_table.isColumnHidden(3)
        assert analytics.pnl_table.horizontalHeader().sortIndicatorSection() == 1
        assert analytics.exposure_page.group_combo.currentText() == "Custom"
        assert analytics.exposure_page.basis_combo.currentText() == "Market value"
        assert analytics.exposure_page.positions.decimals_for(3) == 4
        assert analytics.exposure_page.positions.horizontalHeader().sortIndicatorSection() == 0

        rebalance = window.calcs_tab
        window.tabs.setCurrentWidget(rebalance)
        rebalance.view_choice.setCurrentIndex(rebalance.view_choice.findData("cases"))
        rebalance.table.setColumnWidth(0, 210)
        rebalance.set_column_decimals(1, 5)
        rebalance.table.sortItems(2, Qt.DescendingOrder)
        target_before = dict(rebalance.targets)
        view_menu.aboutToShow.emit()
        assert window._current_table_name() == "Rebalance"
        assert window.save_view_action.isEnabled()
        window.save_view_action.trigger()
        assert json.loads(window.db.setting("default_view_Rebalance"))["mode"] == "cases"
        rebalance.view_choice.setCurrentIndex(rebalance.view_choice.findData("allocation"))
        rebalance.table.setColumnWidth(0, 150)
        rebalance.set_column_decimals(1, 4)
        rebalance.table.sortItems(0, Qt.AscendingOrder)
        view_menu.aboutToShow.emit()
        assert window.restore_view_action.isEnabled()
        window.restore_view_action.trigger()
        assert rebalance.view_mode == "cases"
        assert rebalance.table.columnWidth(0) == 210
        assert rebalance.decimals_for(1) == 5
        assert rebalance.table.horizontalHeader().sortIndicatorSection() == 2
        assert rebalance.table.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder
        assert rebalance.targets == target_before
        for name in window.DEFAULT_TAB_ORDER:
            window.tabs.setCurrentIndex(window._tab_index(name))
            view_menu.aboutToShow.emit()
            assert window.save_view_action.isEnabled(), name
            window.save_view_action.trigger()
            assert window.db.setting(f"default_view_{name}"), name
            view_menu.aboutToShow.emit()
            assert window.restore_view_action.isEnabled(), name
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        assert reopened.analytics_tab.sections.currentWidget() is reopened.analytics_tab.exposure_page
        assert reopened.analytics_tab.range_combo.currentText() == "Custom"
        assert reopened.analytics_tab.capital_combo.currentText() == "Cash / portfolio"
        assert reopened.analytics_tab.cash_share_table.decimals_for(3) == 4
        assert reopened.analytics_tab.pnl_layout_combo.currentText() == "Side by side"
        assert reopened.analytics_tab.exposure_page.group_combo.currentText() == "Custom"
        assert reopened.calcs_tab.view_mode == "cases"
        assert reopened.calcs_tab.table.columnWidth(0) == 210
        assert reopened.calcs_tab.decimals_for(1) == 5
    finally:
        reopened.close()


def test_column_decimal_menu_and_toolbar_default_view_menu(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "decimal-menu.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 1234.56789, "price": 12.34567})
        window._refresh_models()
        window.tabs.setCurrentIndex(1)
        window.show()
        app.processEvents()
        model = window.models["Trades"]
        assert window.windowTitle() == "IBKR Dashboard"
        assert model.data(model.index(0, 3), Qt.DisplayRole) == "1,234.57"
        menu = window._build_header_menu("Trades", 3)
        decimals = next(action.menu() for action in menu.actions() if action.text() == "Decimal places")
        assert [action.text() for action in decimals.actions()] == [
            "0 decimal places", "1 decimal place", "2 decimal places",
            "3 decimal places", "Custom…"]
        assert next(action for action in decimals.actions() if action.isChecked()).text() == "2 decimal places"
        next(action for action in decimals.actions() if action.text() == "3 decimal places").trigger()
        assert model.data(model.index(0, 3), Qt.DisplayRole) == "1,234.568"
        monkeypatch.setattr(ui.QInputDialog, "getInt", lambda *args: (8, True))
        next(action for action in decimals.actions() if action.text() == "Custom…").trigger()
        assert model.data(model.index(0, 3), Qt.DisplayRole) == "1,234.56789000"
        custom_menu = window._build_header_menu("Trades", 3)
        custom_decimals = next(action.menu() for action in custom_menu.actions()
                               if action.text() == "Decimal places")
        assert next(action for action in custom_decimals.actions() if action.isChecked()).text() == "Custom…"
        assert model.data(model.index(0, 4), Qt.DisplayRole) == "12.35"
        assert not any(action.text() == "Decimal places" for action in window._build_header_menu("Trades", 1).actions())
        header = window.views["Trades"].horizontalHeader()
        header_calls = []
        monkeypatch.setattr(window, "_header_menu", lambda name, point: header_calls.append((name, point)))
        header_point = QPoint(header.sectionViewportPosition(3) + 5, header.height() // 2)
        app.sendEvent(header.viewport(), QContextMenuEvent(
            QContextMenuEvent.Mouse, header_point, header.viewport().mapToGlobal(header_point)))
        assert len(header_calls) == 1 and header_calls[0][0] == "Trades"
        assert header.logicalIndexAt(header_calls[0][1]) == 3
        window.models["Portfolio"].set_column_decimals(9, 4)
        window.models["Portfolio"].rows[0]["one_day"] = 0.123456
        assert window.models["Portfolio"].data(window.models["Portfolio"].index(0, 9), Qt.DisplayRole) == "12.3456%"
        assert window.models["Portfolio"].decimals_for(9) == 4
        assert window.models["Portfolio"].decimals_for(10) == 2

        toolbar = window.tabs.widget(1).findChild(QWidget, "TradesToolbar")
        assert toolbar.contextMenuPolicy() == Qt.CustomContextMenu
        calls = []
        monkeypatch.setattr(window, "_toolbar_context_menu", lambda name, point: calls.append(name))
        point = QPoint(4, 4)
        app.sendEvent(toolbar, QContextMenuEvent(QContextMenuEvent.Mouse, point, toolbar.mapToGlobal(point)))
        assert calls == ["Trades"]
        columns_button = next(button for button in toolbar.findChildren(QPushButton) if button.text() == "Columns")
        app.sendEvent(columns_button, QContextMenuEvent(
            QContextMenuEvent.Mouse, point, columns_button.mapToGlobal(point)))
        assert calls == ["Trades", "Trades"]
        view_menu = window._build_toolbar_menu("Trades")
        assert not view_menu.actions()[1].isEnabled()
        view_menu.actions()[0].trigger()
        assert window._build_toolbar_menu("Trades").actions()[1].isEnabled()
    finally:
        window.close()


def test_displayed_financial_numbers_use_commas_and_two_decimals(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "number-display.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_setting("number_decimals", "6")  # Older preference must not override the display rule.
        samples = {
            "Portfolio": {"id": 1, "display_ticker": "AAPL", "position_qty": 12345.6789,
                          "last_px": 1234.5678, "value": 9876543.219,
                          "unrealized_pct": 0.1234, "portfolio_pct": 12.345,
                          "one_day": -0.012345},
            "Trades": {"id": 1, "display_ticker": "AAPL", "quantity": 12345.6789,
                       "price": 1234.5678, "commission_usd": -12.3456,
                       "realized_net": 123.456, "realized_pct": 0.1234,
                       "net_unrealized": -45.678, "unrealized_pct": -0.0567,
                       "traded_pct": .2},
            "Lots": {"buy_trade_id": 1, "ticker": "AAPL", "buy_qty": 12345.6789,
                     "buy_price": 1234.5678, "realized_net": 9876543.219},
        }
        for name, row in samples.items():
            model = window.models[name]
            model.beginResetModel()
            model.rows = [row]
            model.endResetModel()
        portfolio = window.models["Portfolio"]
        assert portfolio.data(portfolio.index(0, 1), Qt.DisplayRole) == "12,345.68"
        assert portfolio.data(portfolio.index(0, 3), Qt.DisplayRole) == "1,234.57"
        assert portfolio.data(portfolio.index(0, 4), Qt.DisplayRole) == "9,876,543.22"
        assert portfolio.headerData(7, Qt.Horizontal, Qt.DisplayRole) == "Open P&L %"
        assert portfolio.data(portfolio.index(0, 7), Qt.DisplayRole) == "12.34%"
        assert portfolio.data(portfolio.index(0, 8), Qt.DisplayRole) == "1,234.50%"
        assert portfolio.data(portfolio.index(0, 9), Qt.DisplayRole) == "-1.23%"
        assert portfolio.headerData(999, Qt.Vertical, Qt.DisplayRole) == "1,000"
        assert portfolio.data(portfolio.index(0, 1), Qt.EditRole) == "12345.6789"
        trades, lots = window.models["Trades"], window.models["Lots"]
        assert trades.data(trades.index(0, 3), Qt.DisplayRole) == "12,345.68"
        assert trades.data(trades.index(0, 5), Qt.DisplayRole) == "-12.35"
        assert [label for _, label, _ in trades.columns[-5:]] == [
            "Realized P&L USD", "Realized P&L %", "Open P&L USD", "Open P&L %",
            "Position traded %"]
        header = window.views["Trades"].horizontalHeader()
        assert header.visualIndex(trades.columnCount() - 1) == header.visualIndex(3) + 1
        assert all(not window.views["Trades"].isColumnHidden(column)
                   for column in range(trades.columnCount() - 5, trades.columnCount()))
        assert [trades.data(trades.index(0, column), Qt.DisplayRole)
                for column in range(trades.columnCount() - 5, trades.columnCount())] == [
                    "123.46", "12.34%", "-45.68", "-5.67%", "20.00%"]
        assert lots.data(lots.index(0, 3), Qt.DisplayRole) == "12,345.68"
        assert lots.data(lots.index(0, 7), Qt.DisplayRole) == "9,876,543.22"
        assert "number_decimals" not in window.settings_fields
        assert "portfolio_nav_usd" not in window.settings_fields
        assert all(button.text() != "Track instrument" for button in window.findChildren(QPushButton))
        assert all(action.text() not in ("Trade", "Lots") for action in window.menuBar().actions())
    finally:
        window.close()


def test_position_details_format_numbers_but_not_references(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "detail-display.sqlite3"))
    window = ui.MainWindow()
    try:
        model = window.models["Portfolio"]
        model.beginResetModel()
        model.rows = [{"id": 1, "display_ticker": "AAPL", "position_qty": 12345.6789,
                       "value": 9876543.219, "one_day": 0.01234, "5d": None,
                       "1m": None, "3m": None, "adv20": 1234567.89,
                       "data_status": "OK"}]
        model.endResetModel()
        window.views["Portfolio"].setCurrentIndex(model.index(0, 0))
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"position_id": 1, "trade_date": "2026-01-02", "side": "BUY",
             "quantity": 12345.6789, "price": 1234.5678,
             "commission_usd": 12.3456, "match_status": "Open"}])
        monkeypatch.setattr(window.service, "lots_rows", lambda: [
            {"position_id": 1, "buy_date": "2026-01-02", "buy_qty": 12345.6789,
             "buy_price": 1234.5678, "sell_date": None, "realized_net": None,
             "net_unrealized": 9876543.219}])
        dialogs = []
        monkeypatch.setattr(ui.QDialog, "exec", lambda dialog: dialogs.append(dialog) or 0)
        window.show_position_details()
        dialog = dialogs[0]
        labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "Quantity 12,345.68" in labels
        assert "Value 9,876,543.22 USD" in labels
        assert "ADV20 1,234,567.89" in labels
        trades, lots = dialog.findChildren(QTableWidget)
        assert trades.item(0, 2).text() == "12,345.68"
        assert trades.item(0, 3).text() == "1,234.57"
        assert trades.item(0, 4).text() == "12.35"
        assert lots.item(0, 1).text() == "12,345.68"
        assert lots.item(0, 5).text() == "9,876,543.22"
    finally:
        window.close()


def test_numeric_input_shows_two_decimals_without_losing_edit_precision():
    app = QApplication.instance() or QApplication([])
    container = QWidget()
    layout = QVBoxLayout(container)
    spin = ui.DisplayAmountSpinBox()
    spin.setRange(0, 1_000_000)
    spin.setDecimals(6)
    spin.setGroupSeparatorShown(True)
    spin.setValue(12345.678901)
    button = QPushButton("Done")
    layout.addWidget(spin)
    layout.addWidget(button)
    try:
        container.show()
        button.setFocus()
        app.processEvents()
        assert spin.text() == "12,345.68"
        assert spin.value() == 12345.678901
        spin.setFocus()
        app.processEvents()
        assert spin.text() == "12,345.678901"
        spin.lineEdit().setText("23,456.123456")
        button.setFocus()
        app.processEvents()
        assert spin.text() == "23,456.12"
        assert spin.value() == 23456.123456
        spin.setFocus()
        app.processEvents()
        assert spin.text() == "23,456.123456"
        assert spin.value() == 23456.123456
    finally:
        container.close()


def test_window_content_fits_small_desktop(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "layout.sqlite3"))
    window = ui.MainWindow()
    try:
        for index in range(15):
            window.db.add_position({"display_ticker": f"STOCK{index}", "ib_symbol": f"STOCK{index}"})
        window._refresh_models()
        for width, height in ((800, 600), (1280, 720)):
            window.resize(width, height)
            window.show()
            for tab, name in enumerate(("Portfolio", "Trades", "Lots")):
                window.tabs.setCurrentIndex(tab)
                app.processEvents()
                assert window.width() <= width
                assert window.height() <= height
                assert window.views[name].width() >= width - 80
                assert window.views[name].height() >= 120
    finally:
        window.close()


def test_dark_palette_columns_button_sort_and_selection(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    old_palette = app.palette()
    dark = QPalette(old_palette)
    dark.setColor(QPalette.Text, QColor("white"))
    dark.setColor(QPalette.Window, QColor("#292929"))
    app.setPalette(dark)
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "dark.sqlite3"))
    window = ui.MainWindow()
    try:
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.add_trade({"position_id": position_id, "trade_date": "2026-01-02", "side": "BUY",
                             "quantity": 100, "price": 10, "commission_usd": 0})
        window._refresh_models()
        model, view = window.models["Trades"], window.views["Trades"]
        assert model.data(model.index(0, 1), Qt.DisplayRole) == "ABC"
        assert model.data(model.index(0, 1), Qt.ForegroundRole).name() == "#1d2b3a"
        window.show()
        window.tabs.setCurrentIndex(1)
        buttons = window.tabs.widget(1).findChildren(QPushButton)
        columns = next(button for button in buttons if button.text() == "Columns")
        QTest.mouseClick(columns, Qt.LeftButton)
        app.processEvents()
        next(action for action in columns.menu().actions() if action.text() == "Show all columns").trigger()
        columns.menu().hide()
        assert not view.isColumnHidden(11)

        view.setCurrentIndex(model.index(0, 0))
        view.selectRow(0)
        view.selection_axis = "row"
        window._refresh_models()
        assert view.selectedIndexes() and model.rows[view.selectedIndexes()[0].row()]["id"] == window.db.rows("trades", 1)[0]["id"]
    finally:
        window.close()
        app.setPalette(old_palette)


def test_account_summary_and_theme_persist(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "theme.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        assert window.heading.text() == "Account not set"
        window.db.set_setting("ib_account_id", "U0000001")
        window.db.set_portfolio_nav(1, 1234567.89)
        window._refresh_models()
        assert window.heading.text() == "U0000001"
        assert window.metric_values["nav"].text() == "$1,234,567.89"
        assert window.metric_values["cash"].text() == "—"
        assert "not available" in window.metric_values["cash"].toolTip().lower()
        position_id = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.cache_contract(position_id, 123)
        window.db.save_broker_positions(1, [{"con_id": 123, "quantity": 5,
                                             "symbol": "ABC", "currency": "USD"}], "2026-10-06")
        window._refresh_models()
        assert "1 position difference" in window.data_notice.text()
        assert window.metric_values["holdings"].text() == "—"
        assert "position difference" in window.metric_values["holdings"].toolTip()
        window.theme_button.click()
        assert window.theme == "dark"
        assert window.db.setting("ui_theme") == "dark"
        assert COLORS["dark"]["canvas"] in window.styleSheet()
        window.theme_button.click()
        assert window.theme == "light"
        window.theme_button.click()
    finally:
        window.close()
    reopened = ui.MainWindow()
    try:
        assert reopened.theme == "dark"
        assert reopened.heading.text() == "U0000001"
    finally:
        reopened.close()


def test_account_summary_visibility_persists_across_tabs_and_launches(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "summary-visibility.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        window.show()
        app.processEvents()
        assert window.show_summary_action.isChecked()
        assert window.summary.isVisible()

        window.show_summary_action.setChecked(False)
        assert window.db.setting("show_account_summary") == "0"
        assert not window.summary.isVisible()
        window.db.set_portfolio_nav(1, 1234)
        window._refresh_models()
        assert window.metric_values["nav"].text() == "$1,234.00"
        window.tabs.setCurrentWidget(window.cash_tab)
        app.processEvents()
        assert not window.summary.isVisible()
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        reopened.show()
        app.processEvents()
        assert not reopened.show_summary_action.isChecked()
        assert not reopened.summary.isVisible()
        reopened.show_summary_action.setChecked(True)
        assert reopened.db.setting("show_account_summary") == "1"
        assert reopened.summary.isVisible()
        reopened.tabs.setCurrentWidget(reopened.calcs_tab)
        app.processEvents()
        assert not reopened.summary.isVisible()
        reopened.tabs.setCurrentIndex(0)
        app.processEvents()
        assert reopened.summary.isVisible()
    finally:
        reopened.close()


def test_daily_pnl_summary_uses_gain_loss_colors_in_both_themes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "daily-color.sqlite3"))
    window = ui.MainWindow()
    try:
        monkeypatch.setattr(window, "_refresh_model", lambda name, rows=None: None)
        row = {"value": 100, "daily_pnl": 12.5, "unrealized_pnl": 20,
               "position_qty": 1, "reconciliation": "MATCH", "display_ticker": "ABC"}
        monkeypatch.setattr(window.service, "portfolio_rows", lambda: [row])
        monkeypatch.setattr(window.service, "lots_rows", lambda rows=None: [])
        daily_label = window.metric_values["daily"]
        for theme in ("light", "dark"):
            window._apply_theme(theme)
            for amount, color in ((12.5, COLORS[theme]["positive"]),
                                  (-12.5, COLORS[theme]["negative"]),
                                  (0, COLORS[theme]["text"])):
                row["daily_pnl"] = amount
                window._refresh_models()
                app.processEvents()
                assert daily_label.palette().color(daily_label.foregroundRole()).name() == color
            row["reconciliation"] = "DIFFERENCE"
            window._refresh_models()
            app.processEvents()
            assert daily_label.text() == "—"
            assert daily_label.palette().color(daily_label.foregroundRole()).name() == COLORS[theme]["text"]
            row["reconciliation"] = "MATCH"
    finally:
        window.close()


def test_unused_header_area_follows_theme(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "header.sqlite3"))
    window = ui.MainWindow()
    try:
        window.resize(1400, 600)
        window.tabs.setCurrentIndex(2)
        window.show()
        app.processEvents()
        header = window.views["Lots"].horizontalHeader()
        window.resize(window.width() + header.length() - header.viewport().width() + 100,
                      window.height())
        app.processEvents()
        assert header.viewport().width() > header.length()
        for theme in ("light", "dark"):
            window._apply_theme(theme)
            app.processEvents()
            assert header.palette().color(header.backgroundRole()).name() == COLORS[theme]["surface_alt"]
    finally:
        window.close()


def test_settings_and_help_are_readable_in_both_themes(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "help.sqlite3"))
    window = ui.MainWindow()
    try:
        window.show()
        window.open_settings()
        app.processEvents()
        settings_page = window.settings_dialog.findChild(QWidget, "settingsPage")
        for theme in ("light", "dark"):
            window._apply_theme(theme)
            app.processEvents()
            assert settings_page.palette().color(settings_page.backgroundRole()).name() == COLORS[theme]["canvas"]
            assert len(window.help_screenshots) == 28
            assert all(not image._original.isNull()
                       for image in window.help_screenshots)
        window.settings_dialog.close()
        window.help_button.click()
        app.processEvents()
        assert window.help_dialog.isVisible()
        assert window.help_navigation.currentItem().text() == "Portfolio"
        assert [window.help_navigation.item(i).text() for i in range(window.help_navigation.count())] == [
            "Take a tour…", "Start", "Portfolio", "Matched lots", "Analytics", "Rebalance",
            "Views & data", "Shortcuts"]
        guide = " ".join(label.text() for label in window.help_dialog.findChildren(QLabel))
        next_tab = native_text(shortcut("Ctrl+Tab", "Meta+Tab"))
        previous_tab = native_text(shortcut("Ctrl+Shift+Tab", "Meta+Shift+Tab"))
        for term in ("Overview", "Trades", "P&L", "Shift+Space",
                     native_text(shortcut("Ctrl+Space")), native_text(QKeySequence(QKeySequence.Copy)),
                     next_tab, previous_tab, native_text(shortcut("Ctrl+H", "Ctrl+Shift+H")),
                     "F1", "Read-Only API"):
            assert term in guide
        for term in (native_text(shortcut("Ctrl+-")), native_text(shortcut("Ctrl+Shift+-")),
                     native_text(shortcut("Ctrl+0")),
                     "Rebalance: remove idea or zero target", "elsewhere zoom out"):
            assert term in guide
        for title, phrase in (("Start", "Performance & Reports → Flex Queries"),
                              ("Portfolio", "Hide closed"),
                              ("Matched lots", "Reset sale to automatic"),
                              ("Analytics", "Exposure"),
                              ("Rebalance", "Target weight"),
                              ("Views & data", "Save current tab view"),
                              ("Shortcuts", f"{next_tab} / {previous_tab}")):
            window.help_navigation.setCurrentRow(window.help_section_indices[title])
            app.processEvents()
            assert window.help_sections.currentWidget().isVisible()
            if title in ("Start", "Portfolio", "Matched lots", "Analytics", "Rebalance",
                         "Views & data"):
                assert not window.help_sections.currentWidget().horizontalScrollBar().isVisible(), title
            section_text = " ".join(label.text() for label in
                                    window.help_sections.currentWidget().findChildren(QLabel))
            assert phrase in section_text
            if title == "Start":
                for instruction in ("Global Configuration", "Gateway: Configure", "7496", "4001",
                                    "7497", "4002", "it may differ from the defaults",
                                    "Enter the host, Socket Port"):
                    assert instruction in section_text
                for instruction in ("Trades → Executions", "IB Execution ID", "Corporate Actions",
                                    "Position Amount in Base", "Cash Transactions", "save the Flex token",
                                    "PortfolioAnalyst since-inception monthly Detailed CSV",
                                    "Activity Statement CSV", f"before {ui.oldest_flex_date().isoformat()}",
                                    "PortfolioAnalyst must include that month", "Change in NAV",
                                    "Select both files together", "not missing daily NAV"):
                    assert instruction in section_text
                disclosures = window.help_sections.currentWidget().findChildren(QToolButton)
                assert [button.text() for button in disclosures] == [
                    "Trades query · fields to select", "Analytics query · fields to select",
                    "Cash query · fields to select"]
                disclosures[0].click()
                app.processEvents()
                assert disclosures[0].isChecked()
                assert [window.help_jump_menu.itemText(i) for i in range(1, 8)] == [
                    "Port", "Connect", "Sync", "Flex queries", "Flex token", "Import history", "Older CSV"]
            if title == "Matched lots":
                for action in ("Remove split", "Rebuild automatic matches", "FIFO", "LIFO",
                               "Max realized gain", "Max unrealized gain",
                               "Clear match for selected lot", "Save changes"):
                    assert action in section_text
            if title == "Analytics":
                assert window.help_jump_menu.itemText(5) == "Monthly P&L"
                window.help_jump_menu.activated.emit(5)
                app.processEvents()
                assert window.help_sections.currentWidget().verticalScrollBar().value() > 0
        window.help_dialog.close()
        window.tabs.setCurrentWidget(window.views["Lots"].parentWidget())
        window.open_help()
        assert window.help_navigation.currentItem().text() == "Matched lots"
        window.help_dialog.close()
        window.tabs.setCurrentWidget(window.analytics_tab)
        window.open_help()
        assert window.help_navigation.currentItem().text() == "Analytics"
        window.help_dialog.close()
        window.tabs.setCurrentWidget(window.calcs_tab)
        window.open_help()
        assert window.help_navigation.currentItem().text() == "Rebalance"
        window.help_dialog.close()
        window.activateWindow()
        window.setFocus()
        app.processEvents()
        assert window.help_guide_action.text() == "User guide"
        assert window.help_guide_action.shortcut() == QKeySequence("F1")
        if sys.platform == "darwin":
            # The offscreen macOS plugin cannot raise the window for a native
            # application shortcut, so exercise the registered action.
            window.help_guide_action.trigger()
        else:
            QTest.keyClick(window, Qt.Key_F1)
        app.processEvents()
        assert window.help_dialog.isVisible()
    finally:
        window.help_dialog.close()
        window.settings_dialog.close()
        window.close()


@pytest.mark.parametrize("port", [4001, 7497])
def test_broker_connection_triggers_trade_sync_once(tmp_path, monkeypatch, port):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "gateway-sync.sqlite3"))
    window = ui.MainWindow()
    previous_market = window.market
    try:
        calls = []
        monkeypatch.setattr(window, "_launch_flex_sync", lambda **kwargs: calls.append(kwargs))
        gateway = ui.IBKRMarketDataProvider("127.0.0.1", port, 27, account_id="U0000002")
        window.market = gateway
        gateway.events.put(("connected", True))
        window._poll()
        window._poll()
        assert calls == [{}]
    finally:
        window.market = previous_market
        window.close()


def test_trade_flex_catches_up_on_launch_without_gateway(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db_path = tmp_path / "startup-sync.sqlite3"
    seed = Database(db_path)
    seed.set_setting("ib_account_id", "U0000002")
    seed.set_setting("flex_query_id", "1662556")
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    calls = []

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs: calls.append(command) or Process())
    window = ui.MainWindow()
    try:
        assert [command[-2:] for command in calls] == [["scripts.sync_trades", str(db_path)]]
        assert not window.sync_progress_bar.isHidden()
        assert window.sync_progress_label.text() == "Syncing Trades history…"
    finally:
        window.close()


def test_failed_trade_status_opens_details(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "failure-ui.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_metadata("ibkr_flex_sync_status", "failed")
        window.db.set_metadata("ibkr_flex_sync_error", "Flex account mismatch")
        window.db.set_metadata("ibkr_flex_sync_stage", "validating Flex report")
        window.db.set_metadata("ibkr_flex_sync_failed_at", "2026-10-08T09:00:00")
        window._update_status()
        assert '>failed</a>' in window.trade_sync_status.text()
        shown = []
        monkeypatch.setattr(ui.QMessageBox, "warning", lambda _parent, title, details: shown.append((title, details)))
        window.show()
        QApplication.processEvents()
        label = window.trade_sync_status
        QTest.mouseClick(label, Qt.LeftButton, pos=QPoint(label.width() - 25, label.height() // 2))
        assert shown and shown[0][0] == "IBKR trade sync failed"
        assert "Flex account mismatch" in shown[0][1]
        assert "validating Flex report" in shown[0][1]
        assert str(tmp_path / "logs" / "sync-trades.log") in shown[0][1]
    finally:
        window.close()


def test_trade_sync_process_crash_sets_clickable_failure(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "crashed-sync.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_setting("ib_account_id", "U0000002")
        window.db.set_setting("flex_query_id", "123")

        class CrashedProcess:
            def poll(self):
                return 7

        monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: CrashedProcess())
        window._launch_flex_sync(manual=True)
        window._advance_sync_queue()
        window._update_status()
        assert window.db.metadata("ibkr_flex_sync_status") == "failed"
        assert "code 7" in window.db.metadata("ibkr_flex_sync_error")
        assert '>failed</a>' in window.trade_sync_status.text()
    finally:
        window.close()


def test_unavailable_trade_statement_is_shown_as_pending(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "pending-ui.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_setting("flex_query_id", "123")
        window.db.set_metadata("ibkr_flex_sync_completed", "2026-10-07T23:54:52")
        window.db.set_metadata("ibkr_flex_sync_status", "failed")
        window.db.set_metadata("ibkr_flex_sync_error", "Flex error 1003: Statement is not available.")
        window.db.set_metadata("ibkr_flex_sync_stage", "requesting Flex report")
        window.db.set_metadata("ibkr_flex_sync_failed_at", "2026-10-08T07:08:24")
        window._update_status()
        window._update_sync_button()
        assert "Trades synced: 2026-10-07 23:54:52" in window.trade_sync_status.text()
        assert ">statement pending</a>" in window.trade_sync_status.text()
        assert "failed" not in window.trade_sync_status.text()
        assert "Trades: statement pending" in window.sync_button.toolTip()
        assert "Historical Flex sync: 2026-10-07 23:54:52 (statement pending)" in window.settings_info.text()
        shown = []
        monkeypatch.setattr(ui.QMessageBox, "information", lambda _parent, title, details: shown.append((title, details)))
        window.show_trade_sync_details()
        assert shown and shown[0][0] == "IBKR trade statement pending"
        assert "Checked at: 2026-10-08T07:08:24" in shown[0][1]
        assert "Date window:" in shown[0][1]
    finally:
        window.close()


def test_header_sync_starts_each_configured_flex_catchup_once(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "manual-sync.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_setting("ib_account_id", "U0000002")
        window.db.set_setting("flex_query_id", "1662556")
        window.db.set_setting("analytics_flex_query_id", "history-query")
        window.db.set_setting("cash_flex_query_id", "cash-query")
        window.db.set_setting("provider", "IBKR")
        live_refreshes = []
        monkeypatch.setattr(window, "refresh_market", lambda **kwargs: live_refreshes.append(kwargs))
        assert window.db.setting("flex_auto_sync_enabled") == "0"
        calls = []
        processes = []

        class Process:
            done = False

            def poll(self):
                return 0 if self.done else None

        def launch(command, **kwargs):
            calls.append(command)
            process = Process()
            processes.append(process)
            return process

        monkeypatch.setattr(ui.subprocess, "Popen", launch)
        window.sync_button.click()
        window.sync_button.click()
        assert live_refreshes == [{"force": True}]
        assert len(calls) == 1
        assert not window.sync_progress_bar.isHidden()
        assert (window.sync_progress_bar.minimum(), window.sync_progress_bar.maximum()) == (0, 0)
        for expected_count in (2, 3):
            processes[-1].done = True
            window._advance_sync_queue()
            assert len(calls) == expected_count
        assert {tuple(command[-2:]) for command in calls} == {
            ("scripts.sync_trades", str(window.db.path)),
            ("scripts.sync_analytics", str(window.db.path)),
            ("scripts.sync_cash", str(window.db.path)),
        }
        assert len(calls) == 3
        assert not window.sync_button.isEnabled()
        processes[-1].done = True
        window._advance_sync_queue()
        window._update_sync_button()
        assert window.sync_button.isEnabled()
        assert window.sync_progress_bar.isHidden()
    finally:
        window.close()


def test_header_sync_progress_waits_for_live_ibapi_callbacks(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "live-progress.sqlite3"))
    window = ui.MainWindow()
    previous_market = window.market
    try:
        window.db.set_setting("provider", "IBKR")
        window.db.set_setting("ib_account_id", "U0000002")
        gateway = ui.IBKRMarketDataProvider("127.0.0.1", 4001, 27, account_id="U0000002")
        monkeypatch.setattr(window, "refresh_market", lambda **kwargs: setattr(window, "market", gateway))
        window.sync_button.click()
        assert not window.sync_progress_bar.isHidden()
        assert window.sync_progress_label.text() == "Syncing live IBKR data…"
        for event in (("broker_positions", {"U0000002": []}), ("account_summary_end",)):
            gateway.events.put(event)
        window._poll()
        assert not window.sync_progress_bar.isHidden()
        gateway.events.put(("executions_end", 0))
        window._poll()
        assert window.sync_progress_bar.isHidden()
        assert window.sync_button.isEnabled()
    finally:
        window.market = previous_market
        window.close()


def test_startup_sync_progress_waits_for_live_ibapi_callbacks(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "startup-progress.sqlite3"
    seed = Database(db_path)
    seed.set_setting("provider", "IBKR")
    seed.set_setting("ib_account_id", "U0000002")
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    gateway = ui.IBKRMarketDataProvider("127.0.0.1", 4001, 27, account_id="U0000002")
    monkeypatch.setattr(ui.MainWindow, "_start_provider",
                        lambda self, **kwargs: setattr(self, "market", gateway))
    window = ui.MainWindow()
    try:
        assert not window.sync_progress_bar.isHidden()
        assert window.sync_progress_label.text() == "Syncing live IBKR data…"
        for event in (("broker_positions", {"U0000002": []}), ("account_summary_end",),
                      ("executions_end", 0)):
            gateway.events.put(event)
        window._poll()
        assert window.sync_progress_bar.isHidden()
    finally:
        window.close()

def test_overview_removes_quantity_diagnostics_without_losing_reconciliation(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "overview-quantity.sqlite3"
    seed = Database(db_path)
    position_id = seed.add_position({"display_ticker": "AAPL", "ib_symbol": "AAPL"})
    seed.cache_contract(position_id, 900000001, multiplier=1)
    seed.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                    "side": "BUY", "quantity": 100, "price": 10, "commission_usd": 0})
    seed.save_broker_positions(1, [{"con_id": 900000001, "quantity": 95,
                                    "symbol": "AAPL", "currency": "USD"}], "2026-10-06T12:00:00")
    seed.set_metadata("broker_positions_refresh_1", "2026-10-06T12:00:00")
    seed.set_setting("header_layout_version", "3")
    seed.set_setting("header_Portfolio", "old-seventeen-column-state")
    seed.set_setting("sort_Portfolio", "11:desc")
    seed.set_setting("default_view_Portfolio", json.dumps({
        "version": 1, "header": "old-seventeen-column-state", "sort_key": "broker_qty",
        "sort_order": "desc", "search": "AAPL", "hide_closed": False}))
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        model = window.models["Portfolio"]
        assert {"broker_qty", "qty_difference"}.isdisjoint(
            key for key, _, _ in model.columns)
        assert model.columnCount() == 16
        assert model.columns[12][0] == "reconciliation"
        assert not window.views["Portfolio"].isColumnHidden(12)
        assert model.sort_column == 1
        assert window.db.setting("sort_Portfolio") == "1:desc"
        assert window._saved_default_view("Portfolio")["header"] is None
        assert model.rows[0]["broker_qty"] == 95
        assert model.rows[0]["qty_difference"] == 5
        assert model.rows[0]["reconciliation"] == "DIFFERENCE"
        assert window.db.setting("portfolio_columns_version") == "4"
    finally:
        window.close()


def test_live_position_delta_refreshes_reconciliation(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db_path = tmp_path / "position-delta.sqlite3"
    seed = Database(db_path)
    seed.set_setting("ib_account_id", "U1")
    position_id = seed.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    seed.cache_contract(position_id, 123, multiplier=1)
    seed.add_trade({"position_id": position_id, "trade_date": "2026-01-02",
                    "side": "BUY", "quantity": 5, "price": 10})
    seed.save_broker_positions(1, [{"con_id": 123, "quantity": 5,
                                    "symbol": "ABC", "currency": "USD"}], "2026-10-06T12:00:00")
    seed.set_metadata("broker_positions_refresh_1", "2026-10-06T12:00:00")
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        assert window.models["Portfolio"].rows[0]["reconciliation"] == "OK"
        window.market.other_events.append(("broker_position_delta", "U1", {
            "con_id": 123, "quantity": 3, "symbol": "ABC", "currency": "USD",
            "security_type": "STK"}))
        window._poll()
        assert window.models["Portfolio"].rows[0]["reconciliation"] == "DIFFERENCE"
        assert window.db.broker_positions(1)[0]["quantity"] == 3
    finally:
        window.close()


def test_sync_reuses_a_healthy_ibkr_connection(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "warm-sync.sqlite3"))
    window = ui.MainWindow()
    try:
        class ConnectedProvider(ui.MarketDataProvider):
            def __init__(self):
                super().__init__()
                self.connected = True
                self.refreshes = 0

            def refresh_live(self):
                self.refreshes += 1

            def stop(self):
                pass

        monkeypatch.setattr(ui, "IBKRMarketDataProvider", ConnectedProvider)
        window.market = ConnectedProvider()
        window.service.market = window.market
        window.db.set_setting("provider", "IBKR")
        monkeypatch.setattr(window, "refresh_market", lambda **_kwargs: pytest.fail("unnecessary reconnect"))
        window.sync_all()
        assert window.market.refreshes == 1
    finally:
        window.close()


def test_overview_migrates_saved_difference_column_sort(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    db_path = tmp_path / "overview-difference.sqlite3"
    seed = Database(db_path)
    seed.set_setting("header_layout_version", "3")
    seed.set_setting("portfolio_columns_version", "2")
    seed.set_setting("header_Portfolio", "old-sixteen-column-state")
    seed.set_setting("sort_Portfolio", "12:desc")
    seed.conn.close()
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        model = window.models["Portfolio"]
        assert model.columnCount() == 16
        assert model.columns[12][0] == "reconciliation"
        assert not window.views["Portfolio"].isColumnHidden(12)
        assert model.sort_column == 12
        assert window.db.setting("sort_Portfolio") == "12:desc"
        window.db.set_setting("default_view_Portfolio", json.dumps({
            "version": 1, "header": "old-sixteen-column-state",
            "columns": [key for key, _, _ in ui.PORTFOLIO if key != "unrealized_pct"][:11] +
                       ["qty_difference", "reconciliation", "data_status", "price_source", "last_updated"],
            "sort_key": "qty_difference", "sort_order": "desc", "search": "",
            "hide_closed": False}))
        state = window._saved_default_view("Portfolio")
        assert state["header"] is None
        assert state["sort_key"] == "reconciliation"
        assert window.db.setting("portfolio_columns_version") == "4"
    finally:
        window.close()


def test_matches_menu_runs_manual_matching(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "matches.sqlite3"))
    window = ui.MainWindow()
    try:
        position = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        window.db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                             "side": "BUY", "quantity": 10, "price": 10, "commission_usd": 0})
        window.db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                             "side": "SELL", "quantity": 4, "price": 12, "commission_usd": 0})
        window._refresh_models()
        matches_action = next(action for action in window.menuBar().actions()
                              if action.text() == "Matches")
        menu = matches_action.menu()
        assert [action.text() for action in menu.actions() if action.text()] == [
            "Match lots…", "Rebuild automatic matches", "Auto-match remaining", "Clear match for selected lot"]
        auto_menu = next(a for a in menu.actions() if a.text() == "Auto-match remaining").menu()
        assert [action.text() for action in auto_menu.actions()] == [
            "FIFO", "LIFO", "Max realized gain", "Max unrealized gain"]

        def matched():
            return window.db.conn.execute("SELECT COUNT(*) FROM trade_matches").fetchone()[0]

        assert matched() == 0
        next(a for a in menu.actions() if a.text() == "Rebuild automatic matches").trigger()
        assert matched() == 1
        with window.db.conn:
            window.db.conn.execute("DELETE FROM trade_matches")
        for option in auto_menu.actions():
            option.trigger()
            assert matched() == 1
            with window.db.conn:
                window.db.conn.execute("DELETE FROM trade_matches")
        # Clearing with nothing selected must not raise or block on a dialog.
        next(a for a in menu.actions() if a.text() == "Clear match for selected lot").trigger()
    finally:
        window.close()
