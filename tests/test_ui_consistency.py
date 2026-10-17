"""Cross-tab UI contracts for shared controls and presentation patterns."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QFontInfo, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QHeaderView,
                               QDoubleSpinBox, QFormLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

import app.ui as ui
from app.views.calcs_ui import CalcsTab, CalcsTable
from app.storage.database import Database
from app.views.table_ui import InputReportTable, ReportTable
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


def test_tabs_and_settings_use_explicit_left_alignment(window):
    assert not window.tabs.tabBar().expanding()
    assert not window.analytics_tab.sections.tabBar().expanding()
    settings_page = window.settings_dialog.findChild(QScrollArea, "settingsScroll").widget()
    forms = (settings_page.layout().itemAt(0).layout(), window.advanced_settings.layout())
    for form in forms:
        assert form.formAlignment() & Qt.AlignLeft
        assert form.labelAlignment() & Qt.AlignLeft
        assert form.fieldGrowthPolicy() == QFormLayout.ExpandingFieldsGrow


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


def test_zoomed_inputs_fit(window):
    app = QApplication.instance()
    window.set_zoom(150)
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        app.processEvents()
        _assert_input_text_fits(window.tabs.currentWidget())


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
    cash_panel = window.calcs_tab.findChild(QWidget, "card")
    assert cash_panel.palette().color(QPalette.Window).name() == COLORS[theme]["surface"]
    secondary = [button for button in window.findChildren(QPushButton)
                 if button.objectName() == "secondary"]
    assert secondary
    assert all(button.palette().color(QPalette.Button).name() ==
               COLORS[theme]["surface" if button.isEnabled() else "disabled_surface"]
               for button in secondary)
    for name in window.views:
        toolbar = window.findChild(QWidget, f"{name}Toolbar")
        columns = next(button for button in toolbar.findChildren(QPushButton)
                       if button.text() == "Columns")
        assert columns.palette().color(QPalette.Button).name() == COLORS[theme]["surface"]


def test_match_lots_availability_follows_selection_and_click_away(window):
    button = window.match_lots_button
    assert not button.isEnabled()
    assert not window.match_lots_action.isEnabled()
    position = window.db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    window.db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                         "side": "BUY", "quantity": 10, "price": 10})
    window._refresh_models()
    assert not button.isEnabled()

    view = window.views["Lots"]
    window.tabs.setCurrentWidget(view.parentWidget())
    view.setCurrentIndex(view.model().index(0, 0))
    assert button.isEnabled()
    assert window.match_lots_action.isEnabled()
    QTest.mouseClick(window.selection_info["Lots"], Qt.LeftButton)
    assert not view.selectedIndexes()
    assert not view.currentIndex().isValid()
    assert not button.isEnabled()
    assert not window.match_lots_action.isEnabled()

    view.setCurrentIndex(view.model().index(0, 0))
    assert button.isEnabled()
    window.search_fields["Lots"].setText("no matching instrument")
    assert not button.isEnabled()


def test_click_away_clears_report_table_in_dialog(window):
    dialog = QDialog(window)
    layout = QVBoxLayout(dialog)
    empty_space = QLabel("Details")
    layout.addWidget(empty_space)
    table = ReportTable(1, 1)
    table.setItem(0, 0, QTableWidgetItem("Synthetic"))
    layout.addWidget(table)
    other_table = ReportTable(1, 1)
    other_table.setItem(0, 0, QTableWidgetItem("Another row"))
    layout.addWidget(other_table)
    dialog.show()
    QApplication.instance().processEvents()
    try:
        table.setCurrentCell(0, 0)
        other_table.setCurrentCell(0, 0)
        assert table.selectedIndexes() and other_table.selectedIndexes()
        QTest.mouseClick(empty_space, Qt.LeftButton)
        for view in (table, other_table):
            assert not view.selectedIndexes()
            assert not view.currentIndex().isValid()
    finally:
        dialog.close()


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_disabled_buttons_are_grey_across_shared_variants(window, theme):
    window._apply_theme(theme)
    for name in ("", "secondary", "themeToggle", "danger", "toolbarDropdown"):
        button = QPushButton("Action", window)
        button.setObjectName(name)
        button.setEnabled(False)
        button.ensurePolished()
        assert button.palette().color(QPalette.Button).name() == COLORS[theme]["disabled_surface"]
        assert button.palette().color(QPalette.ButtonText).name() == COLORS[theme]["disabled_text"]
        button.deleteLater()

    for name in ("link", "historyCalendarButton", "historyHelpButton", "returnNotesButton"):
        button = QToolButton(window)
        button.setObjectName(name)
        button.setEnabled(False)
        button.ensurePolished()
        assert button.palette().color(QPalette.ButtonText).name() == COLORS[theme]["disabled_text"]
        button.deleteLater()


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


def _drop_sale(dialog, sale_row, buy_row):
    mime = dialog.sale_table.mimeData([dialog.sale_table.item(sale_row, 0)])
    if mime is None:
        return False
    point = dialog.table.visualItemRect(dialog.table.item(buy_row, 0)).center()
    enter = QDragEnterEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    dialog.table.dragEnterEvent(enter)
    move = QDragMoveEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    dialog.table.dragMoveEvent(move)
    event = QDropEvent(QPointF(point), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    dialog.table.dropEvent(event)
    return event.isAccepted()


def test_lot_match_dialog_drag_preview_reset_and_save(tmp_path):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match.sqlite3")
    try:
        position = db.add_position({"display_ticker": "ABC US Equity", "ib_symbol": "ABC"})
        early = db.add_trade({"position_id": position, "trade_id": "IBKR:BUY-EARLY",
                              "trade_date": "2026-01-01", "side": "BUY",
                              "quantity": 50, "price": 10, "commission_usd": 0})
        late = db.add_trade({"position_id": position, "trade_id": "IBKR:BUY-LATE",
                             "trade_date": "2026-01-02", "side": "BUY",
                             "quantity": 50, "price": 9, "commission_usd": 0})
        sell = db.add_trade({"position_id": position, "trade_id": "IBKR:SALE",
                             "trade_date": "2026-01-10", "side": "SELL",
                            "quantity": 50, "price": 12, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])

        dialog = LotMatchDialog(service, position, "ABC US Equity", sale_id=sell)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            # The sale is available even though automatic matching filled it.
            assert any(m[0] == late and m[3] == "auto" for m in dialog.preview)
            assert dialog.sale_table.rowCount() == 1
            assert dialog._selected_sale_id() == sell
            assert dialog.sale_table.item(0, 0).text() == "2026-01-10 · SELL"
            assert dialog.sale_table.item(0, 1).text() == "12.00"
            assert dialog.sale_table.item(0, 2).text() == "50"
            assert "2026-01-02 @ 9.00 · 50 auto" in dialog.sale_table.item(0, 3).text()
            assert dialog.sale_table.item(0, 4).text() == "+$150.00"
            assert "0 chosen by you" in dialog.summary.text()
            assert not dialog.save_button.isEnabled()
            assert all("IBKR" not in dialog.sale_table.item(0, column).text()
                       for column in range(dialog.sale_table.columnCount()))
            assert "IBKR" not in dialog.table.item(0, 0).text()
            assert "IBKR" not in dialog.sale_detail.text()
            assert dialog.table.item(0, 6).text() == "+$100.00"
            assert dialog.table.item(0, 7).text() == "+20.00%"
            assert dialog.table.item(dialog.table.row_for_key((late, 0)), 3).text() == "50"
            assert "+$150.00" in dialog.pnl_summary.text()

            # Drop the fully auto-matched sale on another purchase.
            assert _drop_sale(dialog, 0, 0)
            assert dialog.manual_matches == [(early, sell, 50)]
            assert (early, sell, 50, "manual") in dialog.preview
            assert not any(m[0] == late for m in dialog.preview)
            assert dialog.sale_table.item(0, 0).text() == "2026-01-10 · SELL"
            assert "2026-01-01 @ 10.00 · 50 chosen" in dialog.sale_table.item(0, 3).text()
            assert dialog.table.item(dialog.table.row_for_key((early, 0)), 2).text() == "50"
            assert dialog.table.item(dialog.table.row_for_key((late, 0)), 3).text() == "0"
            assert "50 chosen by you" in dialog.summary.text()
            assert "+$100.00" in dialog.pnl_summary.text()
            assert dialog.save_button.isEnabled()
            assert dialog._selected_sale_id() == sell
            assert db.rows("trade_matches")[0]["origin"] == "auto"

            dialog.reset_button.click()
            assert dialog.manual_matches == []
            assert not dialog.save_button.isEnabled()
            assert "50 automatic" in dialog.summary.text()

            assert _drop_sale(dialog, 0, 0)
            QTest.mouseClick(dialog.save_button, Qt.LeftButton)
            assert dialog.manual_matches == [(early, sell, 50)]
            assert dialog.result() == QDialog.Accepted
        finally:
            dialog.close()

        matches = db.rows("trade_matches")
        assert len(matches) == 1
        assert matches[0]["buy_trade_id"] == early
        assert matches[0]["origin"] == "manual"
        assert service.matching.remaining()[late] == 50
    finally:
        db.conn.close()


def test_lot_match_dialog_keeps_sale_selection_and_reset_is_scoped(tmp_path):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match-sales.sqlite3")
    try:
        position = db.add_position({"display_ticker": "XYZ"})
        buy = db.add_trade({"position_id": position, "trade_date": "2026-01-01", "side": "BUY",
                            "quantity": 100, "price": 10, "commission_usd": 0})
        first = db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "SELL",
                              "quantity": 30, "price": 12, "commission_usd": 0})
        second = db.add_trade({"position_id": position, "trade_date": "2026-01-04", "side": "SELL",
                               "quantity": 20, "price": 13, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        dialog = LotMatchDialog(service, position, "XYZ", sale_id=first)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            assert dialog.sale_table.rowCount() == 2
            assert all("2026-01-01 @ 10.00" in dialog.sale_table.item(row, 3).text()
                       for row in range(2))
            dialog.sale_table.sortItems(1, Qt.DescendingOrder)
            assert dialog.sale_table.item(0, 0).data(Qt.UserRole) == second
            dialog.sale_table.sortItems(0, Qt.AscendingOrder)
            assert dialog.table.item(0, 4).text() == "50/100"
            assert len(dialog.table.item(0, 5).text().splitlines()) == 2
            assert "2026-01-03" in dialog.table.item(0, 5).text()
            assert "2026-01-04" in dialog.table.item(0, 5).text()
            assert dialog.table.rowHeight(0) > dialog.table.verticalHeader().defaultSectionSize()
            assert _drop_sale(dialog, 0, 0)
            dialog.sale_table.setCurrentCell(1, 0)
            QApplication.instance().processEvents()
            assert dialog.manual_matches == [(buy, first, 30)]
            assert dialog._selected_sale_id() == second
            assert "0 chosen by you" in dialog.summary.text()
            assert dialog.table.item(0, 2).text() == "0"
            assert dialog.table.item(0, 3).text() == "20"
            assert dialog.table.item(0, 4).text() == "50/70"
            assert "30 chosen" in dialog.table.item(0, 5).text()
            assert "20 auto" in dialog.table.item(0, 5).text()

            assert _drop_sale(dialog, 1, 0)
            assert sorted(dialog.manual_matches) == [(buy, first, 30), (buy, second, 20)]
            assert dialog.table.item(0, 4).text() == "50/70"
            assert "30 chosen" in dialog.table.item(0, 5).text()
            assert "20 chosen" in dialog.table.item(0, 5).text()
            dialog.reset_button.click()
            assert dialog.manual_matches == [(buy, first, 30)]
            dialog.reject()
            assert all(m["origin"] == "auto" for m in db.rows("trade_matches"))
        finally:
            dialog.close()
    finally:
        db.conn.close()


def test_lot_match_dialog_drop_uses_capacity_and_auto_fills_remainder(tmp_path):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match-capacity.sqlite3")
    try:
        position = db.add_position({"display_ticker": "XYZ"})
        small = db.add_trade({"position_id": position, "trade_date": "2026-01-01", "side": "BUY",
                              "quantity": 20, "price": 10, "commission_usd": 0})
        large = db.add_trade({"position_id": position, "trade_date": "2026-01-02", "side": "BUY",
                              "quantity": 50, "price": 9, "commission_usd": 0})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "SELL",
                             "quantity": 50, "price": 12, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        dialog = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            assert dialog.table.item(0, 6).text() == "+$40.00"
            assert _drop_sale(dialog, 0, 0)
            assert dialog.manual_matches == [(small, sale, 20)]
            assert (large, sale, 30, "auto") in dialog.preview
            assert dialog.sale_table.rowCount() == 1
            purchases = dialog.sale_table.item(0, 3).text()
            assert "2026-01-01 @ 10.00 · 20 chosen" in purchases
            assert "2026-01-02 @ 9.00 · 30 auto" in purchases
            assert len(purchases.splitlines()) == 2
            assert dialog.sale_table.rowHeight(0) > dialog.sale_table.verticalHeader().defaultSectionSize()
            assert dialog.sale_table.item(0, 4).text() == "+$130.00"
            assert dialog.table.item(dialog.table.row_for_key((small, 0)), 2).text() == "20"
            assert dialog.table.item(dialog.table.row_for_key((large, 0)), 3).text() == "30"
            assert "20 chosen by you" in dialog.summary.text()
            assert "30 automatic" in dialog.summary.text()
            dialog.sale_table.sortItems(4, Qt.DescendingOrder)
            assert dialog.sale_table.item(0, 0).text() == "2026-01-03 · SELL"
            dialog.sale_table.sortItems(1, Qt.DescendingOrder)
            assert dialog.sale_table.item(0, 0).text() == "2026-01-03 · SELL"
        finally:
            dialog.close()
    finally:
        db.conn.close()


def test_lot_match_purchase_sort_and_pnl_colors(tmp_path):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog
    from app.views.ui_theme import COLORS

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match-sort.sqlite3")
    try:
        position = db.add_position({"display_ticker": "XYZ"})
        winning_buy = db.add_trade({"position_id": position, "trade_date": "2026-01-01",
                                    "side": "BUY", "quantity": 10, "price": 8, "commission_usd": 0})
        losing_buy = db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                                   "side": "BUY", "quantity": 10, "price": 12, "commission_usd": 0})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                             "side": "SELL", "quantity": 10, "price": 10, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        dialog = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            assert dialog.sale_table.horizontalHeaderItem(0).text() == "Sale"
            assert dialog.sale_table.horizontalHeaderItem(1).text() == "Sale price"
            assert dialog.sale_table.horizontalHeaderItem(2).text() == "Quantity"
            assert dialog.sale_table.horizontalHeaderItem(3).text() == "Matched purchases"
            assert dialog.sale_table.isSortingEnabled()
            assert dialog.table.isSortingEnabled()
            assert dialog.table.horizontalHeader().sectionsMovable()
            assert dialog.table.horizontalHeaderItem(0).text() == "Purchase"
            assert dialog.table.horizontalHeaderItem(1).text() == "Purchase price"
            assert dialog.table.horizontalHeaderItem(2).text() == "This sale chosen"
            assert dialog.table.horizontalHeaderItem(3).text() == "This sale auto"
            assert dialog.table.horizontalHeaderItem(4).text() == "All matched / can reserve"
            assert dialog.table.horizontalHeaderItem(5).text() == "Matched sales"
            assert dialog.table.item(dialog.table.row_for_key((winning_buy, 0)), 1).text() == "8.00"
            assert dialog.table.item(dialog.table.row_for_key((winning_buy, 0)), 0).text() == "2026-01-01 · BUY"
            dialog.table.sortItems(1, Qt.AscendingOrder)
            assert dialog.table.item(0, 0).data(Qt.UserRole)[0] == winning_buy
            assert sum(dialog.table.item(row, 3).raw_value for row in range(2)) == 10
            dialog.table.sortItems(3, Qt.DescendingOrder)
            assert dialog.table.item(0, 3).raw_value == 10
            assert dialog.table.item(0, 3).foreground().color().name() == COLORS["light"]["accent"]

            dialog.table.sortItems(6, Qt.AscendingOrder)
            assert [dialog.table.item(row, 0).data(Qt.UserRole)[0] for row in range(2)] == [
                losing_buy, winning_buy]
            assert dialog.table.item(0, 6).foreground().color().name() == COLORS["light"]["negative"]
            assert dialog.table.item(1, 6).foreground().color().name() == COLORS["light"]["positive"]
            assert dialog.table.item(0, 7).foreground().color().name() == COLORS["light"]["negative"]
            dialog._render_sale()
            assert dialog.table.item(0, 0).data(Qt.UserRole)[0] == losing_buy
            dialog.set_theme("dark")
            assert dialog.table.item(0, 6).foreground().color().name() == COLORS["dark"]["negative"]
            assert dialog.table.item(1, 6).foreground().color().name() == COLORS["dark"]["positive"]
            assert _drop_sale(dialog, 0, 0)
            assert dialog.manual_matches == [(losing_buy, sale, 10)]
            assert dialog.pnl_summary.property("pnlTrend") == "negative"
            assert _drop_sale(dialog, 0, 1)
            assert dialog.manual_matches == [(winning_buy, sale, 10)]
            assert dialog.pnl_summary.property("pnlTrend") == "positive"
            sale_header = dialog.sale_table.horizontalHeader()
            purchase_header = dialog.table.horizontalHeader()
            assert all(sale_header.sectionResizeMode(column) == QHeaderView.Interactive
                       for column in range(dialog.sale_table.columnCount()))
            assert all(purchase_header.sectionResizeMode(column) == QHeaderView.Interactive
                       for column in range(dialog.table.columnCount()))
            sale_header.resizeSection(0, 180)
            purchase_header.resizeSection(1, 135)
            dialog.reject()
        finally:
            dialog.close()
        reopened = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            assert reopened.sale_table.columnWidth(0) == 180
            assert reopened.table.columnWidth(1) == 135
        finally:
            reopened.close()
    finally:
        db.conn.close()


def test_lot_match_dialog_split_lines_persist_and_limit_drops(tmp_path, monkeypatch):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match-split.sqlite3")
    try:
        position = db.add_position({"display_ticker": "XYZ"})
        buy = db.add_trade({"position_id": position, "trade_date": "2026-01-01", "side": "BUY",
                            "quantity": 100, "price": 10, "commission_usd": 0})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "SELL",
                             "quantity": 50, "price": 12, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        dialog = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            dialog.table.selectRow(0)

            def configure_split(split_dialog):
                count = split_dialog.findChild(QComboBox)
                assert count.objectName() == "toolbarDropdown"
                count.setCurrentText("3")
                fields = split_dialog.findChildren(QDoubleSpinBox)
                assert len(fields) == 3
                assert all(field.buttonSymbols() == QAbstractSpinBox.NoButtons for field in fields)
                for field, quantity in zip(fields, (20, 30, 40)):
                    field.setValue(quantity)
                confirm = split_dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
                assert not confirm.isEnabled()
                assert "10 remaining" in split_dialog.findChildren(QLabel)[-1].text()
                fields[2].setValue(50)
                assert confirm.isEnabled()
                return QDialog.Accepted

            monkeypatch.setattr(QDialog, "exec", configure_split)
            dialog.split_button.click()
            assert dialog.buy_splits[buy] == (20, 30, 50)
            assert dialog.table.rowCount() == 3
            assert dialog.table.item(0, 0).text().endswith("1/3 · 20")
            assert not db.setting(dialog._split_key)

            assert _drop_sale(dialog, 0, 0)
            assert _drop_sale(dialog, 0, 1)
            assert dialog.line_pins == {(buy, sale, 0): 20, (buy, sale, 1): 30}
            assert dialog.manual_matches == [(buy, sale, 50)]
            assert "0 automatic" in dialog.summary.text()
            assert dialog.apply()
        finally:
            dialog.close()

        reopened = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            assert reopened.table.rowCount() == 3
            assert reopened.line_pins == {(buy, sale, 0): 20, (buy, sale, 1): 30}
            assert [reopened.table.item(row, 2).text() for row in range(3)] == ["20", "30", "0"]
            assert [reopened.table.item(row, 3).text() for row in range(3)] == ["0", "0", "0"]
            assert _drop_sale(reopened, 0, 2)
            assert reopened.line_pins == {(buy, sale, 2): 50}
            assert reopened.manual_matches == [(buy, sale, 50)]
            reopened.table.selectRow(1)
            reopened.unsplit_button.click()
            assert reopened.table.rowCount() == 1
            assert reopened.manual_matches == [(buy, sale, 50)]
            assert reopened.apply()
        finally:
            reopened.close()

        assert LotMatchDialog(service, position, "XYZ", sale_id=sale).table.rowCount() == 1
    finally:
        db.conn.close()


def test_lot_match_dialog_shows_short_cover_read_only(tmp_path):
    from app.finance.services import PortfolioService
    from app.market.market_data import MockMarketDataProvider
    from app.views.lot_matching_ui import LotMatchDialog

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "lot-match-short.sqlite3")
    try:
        position = db.add_position({"display_ticker": "XYZ"})
        sale = db.add_trade({"position_id": position, "trade_date": "2026-01-01", "side": "SELL",
                             "quantity": 10, "price": 12, "commission_usd": 0})
        db.add_trade({"position_id": position, "trade_date": "2026-01-03", "side": "BUY",
                      "quantity": 10, "price": 10, "commission_usd": 0})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([position])
        dialog = LotMatchDialog(service, position, "XYZ", sale_id=sale)
        try:
            dialog.show()
            QApplication.instance().processEvents()
            assert "10 short cover" in dialog.summary.text()
            assert dialog.table.item(0, 2).text() == "0"
            assert dialog.table.item(0, 3).text() == "10"
            assert dialog.table.item(0, 6).text() == "+$20.00"
            assert dialog.table.item(0, 7).text() == "+20.00%"
            assert not dialog._can_match(sale, dialog.table.item(0, 0).data(Qt.UserRole))
            assert not _drop_sale(dialog, 0, 0)
            assert not dialog.reset_button.isEnabled()
        finally:
            dialog.close()
    finally:
        db.conn.close()


def test_lots_double_click_and_context_menu_reach_match_lots(window, monkeypatch):
    position = window.db.add_position({"display_ticker": "ABC US Equity", "ib_symbol": "ABC"})
    buy = window.db.add_trade({"position_id": position, "trade_date": "2026-01-02",
                               "side": "BUY", "quantity": 10, "price": 10})
    sell = window.db.add_trade({"position_id": position, "trade_date": "2026-01-03",
                                "side": "SELL", "quantity": 4, "price": 12})
    window.service.matching.match(sell, [(buy, 4)])
    window._refresh_models()

    calls = []
    monkeypatch.setattr(window, "match_lots", lambda: calls.append("called"))

    view = window.views["Lots"]
    view.setCurrentIndex(view.model().index(0, 0))
    view.doubleClicked.emit(view.currentIndex())
    assert calls == ["called"]

    menu = window._build_table_menu("Lots", view.visualRect(view.currentIndex()).center())
    action = next(a for a in menu.actions() if a.text() == "Match lots…")
    action.trigger()
    assert calls == ["called", "called"]


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


def test_every_report_table_follows_the_shared_table_contract(window, monkeypatch):
    """Every ReportTable (and InputReportTable) instance shares one read-only,
    no-row-number, movable-column contract, regardless of which tab built it."""
    window.db.add_position({"display_ticker": "DEF US Equity", "ib_symbol": "DEF"})
    window._refresh_models()
    portfolio_view = window.views["Portfolio"]
    portfolio_view.setCurrentIndex(window.models["Portfolio"].index(0, 0))
    dialogs = []
    monkeypatch.setattr(ui.QDialog, "exec", lambda dialog: dialogs.append(dialog) or 0)
    window.show_position_details()
    details_dialog = next(child for child in window.findChildren(QDialog)
                          if child.windowTitle().endswith("position details"))
    window.open_history_import()
    tables = (window.cash_tab.table, window.calcs_tab.table,
             window.analytics_tab.pnl_table, window.analytics_tab.capital_table,
             window.analytics_tab.cash_share_table, window.analytics_tab.monthly_table,
             window.analytics_tab.exposure_page.groups,
             window.analytics_tab.exposure_page.positions,
             window.analytics_tab.return_page.table,
             window.analytics_tab.return_page.details_table,
             window.history_import_dialog.periods_table,
             *details_dialog.findChildren(ReportTable))
    tables = [table for table in tables if table is not None]
    assert len(tables) >= 11
    for table in tables:
        assert isinstance(table, ReportTable)
        assert table.editTriggers() == QAbstractItemView.NoEditTriggers
        assert not table.wordWrap()
        assert not table.showGrid()
        assert not table.verticalHeader().isVisible()
        assert table.verticalHeader().defaultSectionSize() == 25
        assert table.horizontalHeader().sectionsMovable()
    details_dialog.close()


def test_analytics_subtabs_are_movable_and_persist_order(tmp_path, monkeypatch):
    db_path = tmp_path / "subtabs.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(db_path))
    window = ui.MainWindow()
    try:
        sections = window.analytics_tab.sections
        bar = sections.tabBar()
        assert bar.isMovable()
        order_before = [bar.tabData(i) for i in range(bar.count())]
        assert order_before == list(window.analytics_tab.SECTION_IDS)
        bar.moveTab(0, bar.count() - 1)
        key = f"analytics_section_layout_{window.portfolio_id}"
        import json as _json
        saved = _json.loads(window.db.setting(key))
        assert saved["order"][-1] == order_before[0]
    finally:
        window.close()
    reopened = ui.MainWindow()
    try:
        bar = reopened.analytics_tab.sections.tabBar()
        assert [bar.tabData(i) for i in range(bar.count())][-1] == order_before[0]
    finally:
        reopened.close()


def test_section_titles_share_one_font_size_and_weight(window):
    titles = window.findChildren(QLabel, "sectionTitle")
    assert len(titles) >= 3
    sizes = {QFontInfo(label.font()).pixelSize() for label in titles}
    weights = {label.font().weight() for label in titles}
    assert len(sizes) == 1
    assert len(weights) == 1


def test_no_stray_stylesheets_or_hardcoded_colors_outside_theme_module():
    """setStyleSheet(...) calls and 6-digit hex literals belong only in
    ui_theme.py (the shared palette/stylesheet source) and the single
    top-level stylesheet(...) application in app/ui.py's _apply_theme."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    allowed_hex_files = {root / "app" / "views" / "ui_theme.py",
                        root / "app" / "views" / "themed_checkbox.py"}
    hex_pattern = re.compile(r"#[0-9a-fA-F]{6}\b")
    style_pattern = re.compile(r"\.setStyleSheet\(")
    offenders = []
    for path in (root / "app" / "ui.py", *(root / "app" / "views").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path not in allowed_hex_files and hex_pattern.search(text):
            offenders.append(f"{path.name}: hardcoded hex color")
        for match in style_pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if path.name == "ui.py" and "stylesheet(self.theme, self.zoom_percent)" in text.splitlines()[line - 1]:
                continue
            offenders.append(f"{path.name}:{line}: setStyleSheet(...)")
    assert not offenders, offenders
