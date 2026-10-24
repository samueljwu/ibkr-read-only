import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.storage.database import Database
from app.views.analytics_ui import AnalyticsTab
from app.views.table_ui import ColumnMenu, ReportTable, SortItem


def test_column_dropdown_drag_reorders_header_and_preserves_visibility():
    app = QApplication.instance() or QApplication([])
    table = ReportTable(0, 3)
    table.setHorizontalHeaderLabels(("First", "Second", "Third"))
    table.setColumnHidden(1, True)
    menu = ColumnMenu(table)
    try:
        table.show()
        menu.add_columns(table.horizontalHeader(), ("First", "Second", "Third"))
        menu.popup(QPoint(20, 20))
        app.processEvents()
        assert menu.isVisible(), "menu did not open"
        first = next(action for action in menu.actions() if action.text() == "First")
        third = next(action for action in menu.actions() if action.text() == "Third")
        assert not menu._drag_icon.isNull()
        start = menu.actionGeometry(first).center()
        end = menu.actionGeometry(third).center()
        highlight = QPoint(menu.actionGeometry(first).left() + 3, start.y())
        before = menu.grab().toImage().pixelColor(highlight)
        QTest.mousePress(menu, Qt.LeftButton, pos=start)
        assert menu.isVisible(), "menu closed on press"
        midway = start + QPoint(20, 0)
        app.sendEvent(menu, QMouseEvent(QEvent.MouseMove, QPointF(midway), QPointF(midway),
                                        QPointF(menu.mapToGlobal(midway)), Qt.NoButton,
                                        Qt.LeftButton, Qt.NoModifier))
        app.processEvents()
        assert menu.grab().toImage().pixelColor(highlight) != before
        app.sendEvent(menu, QMouseEvent(QEvent.MouseMove, QPointF(end), QPointF(end),
                                        QPointF(menu.mapToGlobal(end)), Qt.NoButton,
                                        Qt.LeftButton, Qt.NoModifier))
        assert menu.isVisible(), "menu closed on move"
        QTest.mouseRelease(menu, Qt.LeftButton, pos=end)
        assert menu.isVisible()
        header = table.horizontalHeader()
        assert [header.logicalIndex(index) for index in range(3)] == [1, 2, 0]
        assert [action.text() for action in menu.actions() if action.isCheckable()] == [
            "Second", "Third", "First"]
        assert table.isColumnHidden(1)
        assert not next(action for action in menu.actions() if action.text() == "Second").isChecked()
        QTest.mouseClick(menu, Qt.LeftButton, pos=menu.actionGeometry(first).center())
        assert table.isColumnHidden(0)
    finally:
        menu.close()
        table.close()


def test_header_columns_submenu_uses_current_visual_order():
    app = QApplication.instance() or QApplication([])
    table = ReportTable(0, 3)
    table.setHorizontalHeaderLabels(("First", "Second", "Third"))
    try:
        header = table.horizontalHeader()
        header.moveSection(2, 0)
        menu = table._build_header_menu(0)
        columns = next(action.menu() for action in menu.actions() if action.text() == "Columns")
        assert isinstance(columns, ColumnMenu)
        assert [action.text() for action in columns.actions() if action.isCheckable()] == [
            "Third", "First", "Second"]
    finally:
        table.close()


def test_shared_table_shortcuts_numeric_sort_and_decimal_preference(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "table.sqlite3")
    table = ReportTable(2, 2)
    try:
        table.setHorizontalHeaderLabels(("Name", "Amount"))
        table.configure_numbers(db, "test_decimals", {1: (2, "usd")})
        table.setItem(0, 0, SortItem("Large"))
        table.setItem(0, 1, table.numeric_item(1, 1000))
        table.setItem(1, 0, SortItem("Small"))
        table.setItem(1, 1, table.numeric_item(1, 9))
        table.setSortingEnabled(True)
        table.sortItems(1, Qt.AscendingOrder)
        assert [table.item(row, 0).text() for row in range(2)] == ["Small", "Large"]
        table.set_column_decimals(1, 3)
        assert table.item(1, 1).text() == "$1,000.000"
        assert db.setting("test_decimals") == '{"1": 3}'
        menu = table._build_header_menu(1)
        decimal_menu = next(action.menu() for action in menu.actions()
                            if action.text() == "Decimal places")
        assert [action.text() for action in decimal_menu.actions()] == [
            "0 decimal places", "1 decimal place", "2 decimal places",
            "3 decimal places", "Custom…"]
        assert not table.item(0, 1).flags() & Qt.ItemIsEditable
        assert {action.text() for action in menu.actions()} >= {"Hide column", "Show all columns"}

        table.show()
        app.processEvents()
        table.setCurrentCell(0, 0)
        QTest.keyClick(table, Qt.Key_Space, Qt.ShiftModifier)
        assert {(index.row(), index.column()) for index in table.selectedIndexes()} == {(0, 0), (0, 1)}
        QTest.keyClick(table, Qt.Key_C, Qt.ControlModifier)
        assert app.clipboard().text() == "Small\t$9.000"
        table.setCurrentCell(1, 1)
        QTest.keyClick(table, Qt.Key_Space, Qt.ControlModifier)
        assert {(index.row(), index.column()) for index in table.selectedIndexes()} == {(0, 1), (1, 1)}
        header = table.horizontalHeader()
        QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.ControlModifier,
                         QPoint(header.sectionViewportPosition(0) + 5, header.height() // 2))
        assert {(index.row(), index.column()) for index in table.selectedIndexes()} == {(0, 0), (1, 0)}
        table.hideColumn(0)
        table._show_all_columns()
        assert not table.isColumnHidden(0)
    finally:
        table.close()
        db.conn.close()








def test_report_table_context_menu_copies_selected_text():
    app = QApplication.instance() or QApplication([])
    table = ReportTable(1, 1)
    table.setItem(0, 0, SortItem("Sample text"))
    table.setCurrentCell(0, 0)
    try:
        menu = table._copy_menu(QPoint(4, 4))
        copy = next(action for action in menu.actions() if action.text() == "Copy")
        assert copy.isEnabled()
        copy.trigger()
        assert app.clipboard().text() == "Sample text"
    finally:
        table.close()
