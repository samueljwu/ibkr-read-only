"""Cross-tab UI contracts for shared controls and presentation patterns."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFontInfo, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QHeaderView,
                               QDoubleSpinBox, QLabel, QLineEdit, QPushButton,
                               QTableWidgetItem, QWidget)

import app.ui as ui
from app.views.calcs_ui import CalcsTab, CalcsTable
from app.storage.database import Database
from app.views.table_ui import InputReportTable
from app.views.ui_theme import (COLORS, TOOLBAR_GROUP_SPACING,
                          TOOLBAR_ITEM_SPACING, stylesheet)


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "ui-consistency.sqlite3"))
    result = ui.MainWindow()
    result.resize(1500, 850)
    result.show()
    app.processEvents()
    try:
        yield result
    finally:
        result.close()


def _label(root, text):
    return next(label for label in root.findChildren(QLabel) if label.text() == text)


def _gap(left, right, window):
    """Distance between widget edges, independent of their parent layouts."""
    return (right.mapTo(window, QPoint(0, 0)).x() -
            left.mapTo(window, QPoint(left.width(), 0)).x())


def _font_size(widget):
    return QFontInfo(widget.font()).pixelSize()


def _margins(widget):
    margins = widget.layout().contentsMargins()
    return margins.left(), margins.top(), margins.right(), margins.bottom()


def _assert_input_text_fits(root):
    """Numeric/date text and selected dropdown text must fit beside their controls."""
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    editors = root.findChildren(QAbstractSpinBox)
    combos = root.findChildren(QComboBox)
    checked = 0
    for editor in editors:
        if not editor.isVisible():
            continue
        checked += 1
        line = editor.lineEdit()
        text_width = line.fontMetrics().horizontalAdvance(editor.text())
        assert line.width() >= text_width + 8, (type(editor).__name__, editor.text(), line.width(), text_width)
        assert editor.height() >= editor.sizeHint().height(), (type(editor).__name__, editor.height())
    for combo in combos:
        if combo.isVisible() and not combo.isEditable():
            checked += 1
            assert combo.width() >= combo.sizeHint().width(), (combo.currentText(), combo.width(), combo.sizeHint().width())
            assert combo.height() >= combo.sizeHint().height(), (combo.currentText(), combo.height(),
                                                                 combo.parentWidget().size())
    # A page that owns inputs but shows none must not pass silently.
    assert checked or not (editors or combos), "inputs exist but none were visible to check"


@pytest.mark.parametrize("theme", ("light", "dark"))
@pytest.mark.parametrize("width", (800, 1372))
def test_visible_inputs_fit_across_tabs_and_settings(window, theme, width):
    app = QApplication.instance()
    window._apply_theme(theme)
    window.resize(width, 700)
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        page = window.tabs.currentWidget()
        if page is window.cash_tab:
            page.range_combo.setCurrentText("Custom")
        elif page is window.analytics_tab:
            page.range_combo.setCurrentText("Custom")
            for section in (page.pnl_page, page.capital_page, page.exposure_page):
                page.sections.setCurrentWidget(section)
                app.processEvents()
                _assert_input_text_fits(page)
        app.processEvents()
        _assert_input_text_fits(page)
    window.open_settings()
    window.advanced_settings.show()
    app.processEvents()
    _assert_input_text_fits(window.settings_dialog)
    for field in window.settings_fields.values():
        if isinstance(field, QLineEdit) and field.isVisible():
            assert field.width() >= 90 and field.height() >= field.sizeHint().height()
    window.settings_dialog.close()


def test_rebalance_table_inputs_fit_large_values_in_both_themes(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "input-fit.sqlite3")
    db.set_portfolio_nav(1, 100000)

    class Service:
        def portfolio_rows(self):
            return [{"id": 1, "display_ticker": "HELD", "ib_symbol": "HELD",
                     "position_qty": 10, "value": 60000, "security_type": "STK",
                     "reconciliation": "OK", "price_source": "LIVE LAST",
                     "last_px": 100, "currency": "USD"}]

    tab = CalcsTab(db, Service(), 1)
    try:
        tab.idea_input.setText("NEW")
        tab.add_idea()
        tab.ideas["NEW"]["price_usd"] = 10000
        tab.scenarios["1"] = {"up_mode": "percent", "up_value": 10000,
                               "down_mode": "price", "down_value": 10000}
        tab.refresh()
        tab.show()
        for theme in ("light", "dark"):
            tab.setStyleSheet(stylesheet(theme))
            for width in (800, 1372):
                tab.resize(width, 600)
                app.processEvents()
                _assert_input_text_fits(tab)
                for position_id in (1, "idea:NEW"):
                    row = tab._row_for_position(position_id)
                    assert tab.table.rowHeight(row) == 25
                    weight = tab.table.cellWidget(row, 4)
                    assert weight.buttonSymbols() == QAbstractSpinBox.NoButtons
                    assert weight.height() >= weight.sizeHint().height()
                    assert weight.height() <= tab.table.rowHeight(row)
                    for column in (4, 7, 8, 9, 11, 12):
                        widget = tab.table.cellWidget(row, column)
                        if widget is None:
                            continue
                        assert isinstance(widget, (QComboBox, QDoubleSpinBox))
                        if isinstance(widget, QDoubleSpinBox):
                            assert widget.buttonSymbols() == QAbstractSpinBox.NoButtons
                        assert widget.height() >= widget.sizeHint().height()
                        assert widget.height() <= tab.table.rowHeight(row)
                assert tab.cash_editor.buttonSymbols() == QAbstractSpinBox.NoButtons
                assert tab.cash_editor.height() == tab.table.verticalHeader().defaultSectionSize()
                tab.table.setColumnWidth(8, 100)
                tab.table.setColumnWidth(4, 100)
                assert tab.table.columnWidth(8) >= 100
                assert tab.table.columnWidth(4) >= 145
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_columns_drag_and_keep_order_after_refresh_and_reopen(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "rebalance-columns.sqlite3")
    db.set_portfolio_nav(1, 100000)

    class Service:
        def portfolio_rows(self):
            return [{"id": 1, "display_ticker": "HELD", "position_qty": 10,
                     "value": 60000, "security_type": "STK", "reconciliation": "OK",
                     "last_px": 100, "currency": "USD"}]

    tab = CalcsTab(db, Service(), 1)
    try:
        tab.resize(1927, 700)
        tab.show()
        app.processEvents()
        header = tab.table.horizontalHeader()
        assert header.sectionsMovable()
        assert header.sectionResizeMode(4) == QHeaderView.Interactive
        start = QPoint(header.sectionViewportPosition(4) + header.sectionSize(4) // 2,
                       header.height() // 2)
        end = QPoint(header.sectionViewportPosition(2) + header.sectionSize(2) // 2,
                     header.height() // 2)
        QTest.mousePress(header.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(header.viewport(), QPoint((start.x() + end.x()) // 2, start.y()), 20)
        QTest.mouseMove(header.viewport(), end, 20)
        QTest.mouseRelease(header.viewport(), Qt.LeftButton, Qt.NoModifier, end)
        app.processEvents()
        moved_index = header.visualIndex(4)
        assert moved_index < 4
        tab.refresh()
        assert header.visualIndex(4) == moved_index
        tab.save_view()
    finally:
        tab.close()
    reopened = CalcsTab(db, Service(), 1)
    try:
        assert reopened.table.horizontalHeader().sectionsMovable()
        assert reopened.table.horizontalHeader().visualIndex(4) == moved_index
    finally:
        reopened.close()
        db.conn.close()


def test_rebalance_old_header_order_maps_to_separate_scenario_columns(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "rebalance-header-migration.sqlite3")
    db.set_portfolio_nav(1, 100000)
    old_table = CalcsTable(0, 12)
    old_header = old_table.horizontalHeader()
    old_table.setColumnWidth(4, 165)
    old_header.moveSection(old_header.visualIndex(9), 1)
    old_header.moveSection(old_header.visualIndex(8), 3)
    db.set_setting("calcs_header_v4_1", bytes(old_header.saveState().toBase64()).decode("ascii"))

    class Service:
        def portfolio_rows(self):
            return [{"id": 1, "display_ticker": "HELD", "position_qty": 10,
                     "value": 60000, "security_type": "STK", "reconciliation": "OK",
                     "last_px": 100, "currency": "USD"}]

    tab = CalcsTab(db, Service(), 1)
    try:
        header = tab.table.horizontalHeader()
        assert [header.visualIndex(column) for column in (11, 12, 13)] == [1, 2, 3]
        assert [header.visualIndex(column) for column in (8, 9, 10)] == [5, 6, 7]
        assert tab.table.columnWidth(4) == 165
        assert header.sectionsMovable()
    finally:
        tab.close()
        old_table.close()
        db.conn.close()


def test_toolbar_labels_spacing_and_alignment(window):
    app = QApplication.instance()

    def check_pair(label, control):
        assert _gap(label, control, window) == TOOLBAR_ITEM_SPACING
        assert abs(label.mapTo(window, QPoint(0, label.height() // 2)).y() -
                   control.mapTo(window, QPoint(0, control.height() // 2)).y()) <= 2
        assert control.objectName() == "toolbarDropdown"
        assert control.maximumHeight() == 32
        assert control.minimumHeight() >= 31

    for name, label_text, control in (
            ("Portfolio", "Show", None),
            ("Trades", "Side", window.trade_side_combo),
            ("Lots", "Status", window.lot_filter_combo)):
        window.tabs.setCurrentWidget(window.views[name].parentWidget())
        app.processEvents()
        toolbar = window.findChild(QWidget, f"{name}Toolbar")
        if control is None:
            control = next(button for button in toolbar.findChildren(QPushButton)
                           if button.text() == "Columns")
        check_pair(_label(toolbar, label_text), control)

    window.tabs.setCurrentWidget(window.cash_tab)
    app.processEvents()
    check_pair(_label(window.cash_tab.range_controls, "Range"), window.cash_tab.range_combo)

    window.tabs.setCurrentWidget(window.analytics_tab)
    window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.pnl_page)
    app.processEvents()
    check_pair(_label(window.analytics_tab, "Chart"), window.analytics_tab.metric_combo)
    check_pair(_label(window.analytics_tab, "Range"), window.analytics_tab.range_combo)
    window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.exposure_page)
    app.processEvents()
    exposure = window.analytics_tab.exposure_page
    check_pair(_label(exposure, "Group by"), exposure.group_combo)
    check_pair(_label(exposure, "Exposure"), exposure.basis_combo)

    window.tabs.setCurrentWidget(window.calcs_tab)
    app.processEvents()
    check_pair(_label(window.calcs_tab, "Show"), window.calcs_tab.view_choice)

    window.tabs.setCurrentWidget(window.views["Portfolio"].parentWidget())
    app.processEvents()
    overview = window.findChild(QWidget, "PortfolioToolbar")
    columns = next(button for button in overview.findChildren(QPushButton)
                   if button.text() == "Columns")
    overview_gap = _gap(columns, window.hide_closed_checkbox, window)
    window.tabs.setCurrentWidget(window.cash_tab)
    app.processEvents()
    assert overview_gap == TOOLBAR_GROUP_SPACING


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_body_fonts_page_margins_and_cash_alignment(window, theme):
    window._apply_theme(theme)
    QApplication.instance().processEvents()
    table_views = [*window.views.values(), window.cash_tab.table,
                   window.analytics_tab.pnl_table, window.analytics_tab.capital_table,
                   window.analytics_tab.exposure_page.positions,
                   window.calcs_tab.table]
    assert len({_font_size(table) for table in table_views}) == 1
    assert len({QFontInfo(table.font()).family() for table in table_views}) == 1

    summary_labels = window.cash_tab.summary_content.findChildren(QLabel)
    body_labels = [label for label in summary_labels if label.objectName() in
                   {"cashCurrencyLabel", "cashItemLabel", "cashAmount",
                    "cashTotalLabel", "cashTotalAmount"}]
    assert body_labels
    assert all(_font_size(label) == _font_size(window.cash_tab.table) for label in body_labels)
    assert _font_size(window.cash_tab.summary_heading) == _font_size(window.cash_tab.transactions_heading)

    assert _margins(window.cash_tab) == _margins(window.analytics_tab) == _margins(window.calcs_tab)
    assert len({_margins(window.views[name].parentWidget()) for name in window.views}) == 1
    assert window.cash_tab.summary_list.contentsMargins().left() == 12
    assert window.cash_tab.summary_list.contentsMargins().right() == 12
    amounts = window.cash_tab.summary_content.findChildren(QLabel, "cashAmount")
    assert amounts
    assert all(label.alignment() & Qt.AlignRight for label in amounts)


def test_data_tables_keep_shared_sorting_and_filtering_contract(window):
    position = window.db.add_position({"display_ticker": "ABC US Equity", "ib_symbol": "ABC"})
    buy = window.db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                               "side": "BUY", "quantity": 10, "price": 10})
    sell = window.db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                                "side": "SELL", "quantity": 4, "price": 12})
    window.service.matching.match(sell, [(buy, 4)])
    window._refresh_models()
    grid_views = tuple(window.views.values())
    report_tables = (window.cash_tab.table, window.analytics_tab.pnl_table,
                     window.analytics_tab.capital_table,
                     window.analytics_tab.exposure_page.positions,
                     window.calcs_tab.table)
    for table in (*grid_views, *report_tables):
        assert table.editTriggers() == QAbstractItemView.NoEditTriggers
        assert not table.wordWrap()
        assert not table.showGrid()
        assert table.verticalHeader().defaultSectionSize() == 25
        assert table.horizontalHeader().sectionsClickable()
        assert table.horizontalHeader().sectionsMovable()
    assert all(table.isSortingEnabled() for table in report_tables)
    for name, view in window.views.items():
        assert view.horizontalHeader().sectionsMovable()
        search = window.search_fields[name]
        assert view.model().rowCount() > 0
        search.setText("no matching instrument")
        assert view.model().rowCount() == 0
        search.clear()
        assert view.model().rowCount() > 0
        view.horizontalHeader().sectionClicked.emit(0)
        assert window.models[name].sort_column == 0
        assert view.horizontalHeader().isSortIndicatorShown()
    assert window.trade_side_combo.itemText(0) == "All"
    assert window.lot_filter_combo.itemText(0) == "All"

    for table in report_tables:
        header = table.horizontalHeader()
        original_first = header.logicalIndex(0)
        header.moveSection(0, 1)
        assert header.visualIndex(original_first) == 1


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_shared_cards_and_buttons_follow_theme(window, theme):
    window._apply_theme(theme)
    QApplication.instance().processEvents()
    cards = (window.summary,
             window.analytics_tab.findChild(QWidget, "analyticsSummary"),
             window.cash_tab.summary_content)
    assert all(card.palette().color(QPalette.Window).name() == COLORS[theme]["surface"]
               for card in cards)
    cash_panel = window.calcs_tab.findChild(QWidget, "calcsCashPanel")
    assert cash_panel.palette().color(QPalette.Window).name() == COLORS[theme]["surface_alt"]
    secondary = [button for button in window.findChildren(QPushButton)
                 if button.objectName() == "secondary"]
    assert secondary
    assert all(button.palette().color(QPalette.Button).name() == COLORS[theme]["surface"]
               for button in secondary)
    for name in window.views:
        toolbar = window.findChild(QWidget, f"{name}Toolbar")
        columns = next(button for button in toolbar.findChildren(QPushButton)
                       if button.text() == "Columns")
        assert columns.palette().color(QPalette.Button).name() == COLORS[theme]["surface"]


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_history_dialog_uses_shared_theme_and_compact_controls(window, theme):
    window._apply_theme(theme)
    window.open_history_import()
    dialog = window.history_import_dialog
    QApplication.instance().processEvents()
    panel = dialog.findChild(QWidget, "historyImportForm")
    assert panel.palette().color(QPalette.Window).name() == COLORS[theme]["surface"]
    assert dialog.from_date.line_edit.palette().color(QPalette.Base).name() == COLORS[theme]["surface"]
    assert dialog.details.palette().color(QPalette.Base).name() == COLORS[theme]["surface"]
    assert dialog.import_button.width() < dialog.width() // 2
    for date_input in (dialog.from_date, dialog.to_date):
        edit = date_input.line_edit
        assert edit.width() >= edit.fontMetrics().horizontalAdvance(edit.text()) + 12
    assert not dialog.details.isVisible()
    dialog.close()


def test_input_table_keyboard_contract_covers_spin_combo_and_text(tmp_path):
    app = QApplication.instance() or QApplication([])
    table = InputReportTable(2, 4)
    committed = []
    try:
        for row in range(2):
            identity = f"row-{row}"
            for column in range(4):
                item = QTableWidgetItem(identity if column == 0 else "")
                if column == 0:
                    item.setData(Qt.UserRole, identity)
                table.setItem(row, column, item)
            spin = QDoubleSpinBox()
            spin.setValue(10 + row)
            table.setCellWidget(row, 1, spin)
            table.register_input(spin, identity, 1, lambda widget=spin: committed.append(widget.value()))
            combo = QComboBox()
            combo.addItems(("Price", "%"))
            combo.setEditable(row == 1)
            table.setCellWidget(row, 2, combo)
            table.register_input(combo, identity, 2, lambda widget=combo: committed.append(widget.currentIndex()))
            text = QLineEdit("initial")
            table.setCellWidget(row, 3, text)
            table.register_input(text, identity, 3, lambda widget=text: committed.append(widget.text()))
        table.show()
        app.processEvents()

        table.select_cell(0, 1, focus=True)
        QTest.keyClick(table, Qt.Key_F2)
        spin = table.cellWidget(0, 1)
        assert spin.hasFocus()
        spin.setValue(25)
        QTest.keyClick(spin.lineEdit(), Qt.Key_Escape)
        assert spin.value() == 10
        assert table.hasFocus() and (table.currentRow(), table.currentColumn()) == (0, 1)
        spin.setFocus()
        spin.setValue(30)
        QTest.keyClick(spin.lineEdit(), Qt.Key_Return)
        assert committed[-1] == 30
        assert (table.currentRow(), table.currentColumn()) == (1, 1)

        next_spin = table.cellWidget(1, 1)
        next_spin.setFocus()
        next_spin.setValue(40)
        QTest.keyClick(next_spin.lineEdit(), Qt.Key_Right)
        assert committed[-1] == 40
        assert (table.currentRow(), table.currentColumn()) == (1, 2)

        table.select_cell(0, 2, focus=True)
        QTest.keyClick(table, Qt.Key_Return)
        combo = table.cellWidget(0, 2)
        assert combo.hasFocus()
        combo.blockSignals(True)
        combo.setCurrentIndex(1)
        combo.blockSignals(False)
        QTest.keyClick(combo, Qt.Key_Escape)
        assert combo.currentIndex() == 0
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key_Return)
        assert committed[-1] == 0
        assert (table.currentRow(), table.currentColumn()) == (1, 2)
        editable_combo = table.cellWidget(1, 2)
        editable_combo.lineEdit().setText("draft")
        QTest.keyClick(editable_combo.lineEdit(), Qt.Key_Escape)
        assert editable_combo.currentText() == "Price"
        editable_combo.lineEdit().setText("saved")
        QTest.keyClick(editable_combo.lineEdit(), Qt.Key_Return)
        assert committed[-1] == 0
        assert editable_combo.currentText() == "saved"

        table.select_cell(0, 3, focus=True)
        QTest.keyClick(table, Qt.Key_F2)
        text = table.cellWidget(0, 3)
        text.setText("draft")
        QTest.keyClick(text, Qt.Key_Escape)
        assert text.text() == "initial"
        text.setFocus()
        text.setText("saved")
        QTest.keyClick(text, Qt.Key_Return)
        assert committed[-1] == "saved"
        assert (table.currentRow(), table.currentColumn()) == (1, 3)
    finally:
        table.close()


def test_rebalance_registers_every_table_input_with_shared_navigation(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "consistency-inputs.sqlite3")
    db.set_portfolio_nav(1, 100000)

    class Service:
        def portfolio_rows(self):
            return [{"id": 1, "display_ticker": "HELD", "ib_symbol": "HELD",
                     "position_qty": 10, "value": 60000, "security_type": "STK",
                     "reconciliation": "OK", "price_source": "LIVE LAST",
                     "last_px": 100, "currency": "USD"}]

    tab = CalcsTab(db, Service(), 1)
    try:
        tab.idea_input.setText("NEW")
        tab.add_idea()
        tab.show()
        app.processEvents()
        table = tab.table
        assert isinstance(table, InputReportTable)
        for identity, columns in ((1, (4, 8, 9, 11, 12)),
                                  ("idea:NEW", (4, 7, 8, 9, 11, 12))):
            for column in columns:
                row = tab._row_for_position(identity)
                widget = table.cellWidget(row, column)
                assert isinstance(widget, (QAbstractSpinBox, QComboBox))
                assert widget in table._input_bindings
                table.select_cell(row, column, focus=True)
                widget.setFocus()
                target = widget.lineEdit() if isinstance(widget, QAbstractSpinBox) else widget
                QTest.keyClick(target, Qt.Key_Escape)
                assert table.hasFocus()
                assert (table.currentRow(), table.currentColumn()) == (row, column)
                row = tab._row_for_position(identity)
                widget = table.cellWidget(row, column)
                widget.setFocus()
                target = widget.lineEdit() if isinstance(widget, QAbstractSpinBox) else widget
                QTest.keyClick(target, Qt.Key_Return)
                assert table.hasFocus()
                assert table.currentColumn() == column
                assert table.currentRow() == min(row + 1, table.rowCount() - 1)
        header = table.horizontalHeader()
        assert header.sectionsMovable()
        header.moveSection(header.visualIndex(8), 5)
        assert header.visualIndex(8) == 5
        row = tab._row_for_position(1)
        table.select_cell(row, 4, focus=True)
        QTest.keyClick(table, Qt.Key_Right)
        assert table.currentColumn() == 8
        table.select_cell(row, 4, focus=True)
        target = table.cellWidget(row, 4)
        target.setFocus()
        QTest.keyClick(target.lineEdit(), Qt.Key_Right)
        assert table.currentColumn() == 8
        assert header.visualIndex(8) == 5
        tab.save_view()
    finally:
        tab.close()
    reopened = CalcsTab(db, Service(), 1)
    try:
        assert reopened.table.horizontalHeader().visualIndex(8) == 5
    finally:
        reopened.close()
        db.conn.close()


def test_sale_lot_allocation_uses_shared_input_navigation():
    app = QApplication.instance() or QApplication([])
    buy = {"id": 7, "trade_date": "2026-01-01", "trade_id": "BUY-7",
           "side": "BUY", "position_id": 1, "quantity": 10000, "price": 100}
    sell = {"id": 8, "trade_id": "SELL-8", "display_ticker": "ABC",
            "position_id": 1, "quantity": 10000}

    class Service:
        portfolio_id = 1

        class DB:
            def rows(self, name, portfolio_id):
                return [] if name == "trade_matches" else [buy]

        db = DB()

        def remaining(self):
            return {7: 10000}

        def _precedes(self, buy_row, sell_row):
            return True

        def _gain_per_unit(self, buy_row, sell_row):
            return 1

        def suggested_max_gain(self, sell_id, replace_existing=False):
            return [(7, 5)]

    dialog = ui.MatchDialog(Service(), sell)
    try:
        dialog.show()
        app.processEvents()
        assert isinstance(dialog.table, InputReportTable)
        spin = dialog.table.cellWidget(0, 6)
        spin.setValue(10000)
        app.processEvents()
        _assert_input_text_fits(dialog)
        dialog.table.setColumnWidth(6, 60)
        assert dialog.table.columnWidth(6) >= spin.sizeHint().width() + 8
        spin.setValue(5)
        header = dialog.table.horizontalHeader()
        assert header.sectionsMovable()
        header.moveSection(header.visualIndex(6), 1)
        assert header.visualIndex(6) == 1
        spin = dialog.table.cellWidget(0, 6)
        dialog.table.select_cell(0, 6, focus=True)
        QTest.keyClick(dialog.table, Qt.Key_F2)
        assert spin.hasFocus()
        spin.setValue(8)
        QTest.keyClick(spin.lineEdit(), Qt.Key_Escape)
        assert spin.value() == 5
        spin.setFocus()
        spin.setValue(7)
        QTest.keyClick(spin.lineEdit(), Qt.Key_Return)
        assert dialog.allocations() == [(7, 7)]
        assert dialog.table.hasFocus()
        dialog.table.select_cell(0, 6, focus=True)
        QTest.keyClick(dialog.table, Qt.Key_Right)
        assert dialog.table.currentColumn() == header.logicalIndex(2)
    finally:
        dialog.close()


def test_custom_exposure_categories_share_input_navigation_and_movable_columns(window, monkeypatch):
    import app.views.exposure_custom_ui as custom_ui
    from app.finance.exposure import add_custom_set

    add_custom_set(window.db, 1, "Theme")
    monkeypatch.setattr(custom_ui, "exposure_snapshot", lambda *_args, **_kwargs:
                        {"positions": [{"under_con_id": 301, "symbol": "ABC", "type": "STK"}]})
    dialog = custom_ui.CustomSetDialog(window.analytics_tab.exposure_page, "Theme")
    try:
        dialog.show()
        QApplication.instance().processEvents()
        table = dialog.table
        assert isinstance(table, InputReportTable)
        header = table.horizontalHeader()
        assert header.sectionsMovable()
        header.moveSection(header.visualIndex(1), 0)
        assert header.visualIndex(1) == 0
        table.select_cell(0, 1, focus=True)
        QTest.keyClick(table, Qt.Key_F2)
        combo = table.cellWidget(0, 1)
        assert combo.hasFocus() or combo.lineEdit().hasFocus()
        combo.lineEdit().setText("Draft")
        QTest.keyClick(combo.lineEdit(), Qt.Key_Escape)
        assert combo.currentText() == ""
        combo.lineEdit().setText("Saved")
        QTest.keyClick(combo.lineEdit(), Qt.Key_Return)
        assert combo.currentText() == "Saved"
        assert table.hasFocus()
        table.select_cell(0, 1, focus=True)
        QTest.keyClick(table, Qt.Key_Right)
        assert table.currentColumn() == 0
    finally:
        dialog.close()
