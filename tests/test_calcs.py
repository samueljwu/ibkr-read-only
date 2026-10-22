import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QHeaderView, QTabWidget, QWidget

from app.finance.calcs import calculate_targets, scenario_metrics
from app.views.calcs_ui import CalcsTab
from app.storage.database import Database
from app.market.market_data import IBKRMarketDataProvider
from datetime import date


def holding(position_id, value, quantity=10, security_type="STK", price=100):
    return {"id": position_id, "display_ticker": f"P{position_id}",
            "position_qty": quantity, "value": value,
            "security_type": security_type, "reconciliation": "OK", "price_source": "LIVE LAST",
            "last_px": price, "currency": "USD"}




def test_individual_targets_and_incomplete_data():
    rows = [holding(1, 60000, 600), holding(2, 30000, 300)]
    result = calculate_targets(rows, 100000, 10000, targets={"1": .5, "2": .3})
    assert result["cash_target"] == pytest.approx(.2)
    assert result["rows"][0]["gap_usd"] == -10000
    assert result["rows"][1]["gap_usd"] == 0
    leveraged = calculate_targets(rows, 100000, 10000, targets={"1": .8, "2": .3})
    assert leveraged["error"] is None
    assert leveraged["cash_target"] == pytest.approx(-.1)
    assert leveraged["rows"][0]["gap_quantity"] == pytest.approx(200)
    assert calculate_targets(rows, 100000, 9000)["error"]
    assert calculate_targets([holding(1, None)], 100000, 10000)["error"]
    assert calculate_targets([holding(1, 0)], 100000, 100000)["error"]
    assert calculate_targets([holding(1, 60000, -600)], 100000, 40000)["error"]
    rows[0]["reconciliation"] = "DIFFERENCE"
    assert calculate_targets(rows, 100000, 10000)["error"]






def test_scenario_prices_percentages_and_missing_downside():
    prices = scenario_metrics(100, "price", 125, "price", 90)
    assert prices["upside_pct"] == pytest.approx(.25)
    assert prices["downside_pct"] == pytest.approx(.10)
    assert prices["risk_reward"] == pytest.approx(2.5)
    assert prices["upside_price"] == pytest.approx(125)
    assert prices["downside_price"] == pytest.approx(90)
    percents = scenario_metrics(None, "percent", 25, "percent", 10)
    assert percents["risk_reward"] == pytest.approx(2.5)
    assert percents["upside_price"] is None
    assert scenario_metrics(200, "percent", 25, "percent", 10)["upside_price"] == pytest.approx(250)
    assert scenario_metrics(200, "percent", 25, "percent", 10)["downside_price"] == pytest.approx(180)
    assert scenario_metrics(None, "price", 125, "percent", 10)["risk_reward"] is None
    assert scenario_metrics(100, "price", 90, "percent", 10)["upside_pct"] is None
    assert scenario_metrics(100, "percent", 25, "price", 110)["downside_pct"] is None
    missing = scenario_metrics(100, "percent", 25, "percent", 0)
    assert missing["risk_reward"] is None and missing["downside_reason"]
    assert scenario_metrics(100, "percent", 25, "percent", 120)["risk_reward"] is None


class Service:
    def __init__(self, rows):
        self.rows = rows

    def portfolio_rows(self):
        return self.rows


def test_calcs_tab_saves_cash_and_position_targets_by_portfolio(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "calcs.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600), holding(2, 30000, 300)])
    first = CalcsTab(db, service, 1)
    try:
        first.resize(800, 600)
        first.show()
        QApplication.processEvents()
        assert first.table.viewport().width() >= 700
        assert first.table.viewport().height() >= 280
        assert first.table.rowCount() == 2
        assert first.table.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive
        assert first.cash_editor.objectName() == "calcsTargetInput"
        assert first.table.cellWidget(0, 4).objectName() == "calcsTargetInput"
        first.cash_editor.setValue(32.5)
        first.cash_editor.editingFinished.emit()
        assert first.mode == "proportional"
        assert first.table.item(0, 5).text() == "Sell $15,000.00"
        assert first.table.item(0, 6).text() == "Sell 150.0000 shares"
        assert first.cash_gap.text() == "Increase $22,500.00"
        first.table.sortItems(2, Qt.AscendingOrder)
        assert [first.table.item(row, 0).text() for row in range(2)] == ["P2", "P1"]
        first.table.cellWidget(0, 4).setValue(20)
        first.table.cellWidget(0, 4).editingFinished.emit()
        assert first.mode == "individual"
        assert first.targets["2"] == .2
        assert first.targets["1"] == pytest.approx(.45)
        assert first.cash_editor.value() == 35
        first.table.setColumnWidth(0, 210)
        first.set_column_decimals(2, 3)
        assert first.table.item(1, 2).text() == "60,000.000"
        menu = first._build_header_menu(1)
        decimals = next(action.menu() for action in menu.actions() if action.text() == "Decimal places")
        next(action for action in decimals.actions() if action.text() == "1 decimal place").trigger()
        assert first.table.item(1, 1).text() == "600.0"
        assert not any(action.text() == "Decimal places" for action in first._build_header_menu(0).actions())
        first.save_view()
    finally:
        first.close()
    with db.conn:
        db.conn.execute("INSERT INTO portfolios(id,name,nav_usd) VALUES (2,'DW',100000)")
    second = CalcsTab(db, service, 1)
    other = CalcsTab(db, service, 2)
    try:
        assert second.mode == "individual"
        assert second.targets["2"] == .2
        assert second.table.columnWidth(0) == 210
        assert second.decimals_for(2) == 3
        assert other.targets == {}
        second.reset_targets()
        assert second.targets == {}
    finally:
        second.close()
        other.close()
        db.conn.close()


def test_position_targets_fill_trade_estimates_and_cash_scales_pro_rata(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "negative_cash.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600), holding(2, 30000, 300)])
    tab = CalcsTab(db, service, 1)
    try:
        for position_id, target in ((1, 80), (2, 30)):
            editor = tab.table.cellWidget(tab._row_for_position(position_id), 4)
            editor.setValue(target)
            editor.editingFinished.emit()
        assert tab.cash_editor.value() == pytest.approx(-10)
        assert tab.table.item(tab._row_for_position(1), 5).text() == "Buy $20,000.00"
        assert tab.table.item(tab._row_for_position(1), 6).text() == "Buy 200.0000 shares"
        assert tab.table.item(tab._row_for_position(2), 5).text() == "—"
        assert tab.cash_editor.value() + sum(tab.table.cellWidget(row, 4).value()
                                             for row in range(tab.table.rowCount())) == pytest.approx(100)

        tab.cash_editor.setValue(-20)
        tab.cash_editor.editingFinished.emit()
        assert tab.mode == "proportional"
        assert tab.cash_editor.value() == pytest.approx(-20)
        weights = [tab.table.cellWidget(tab._row_for_position(position_id), 4).value()
                   for position_id in (1, 2)]
        assert weights == pytest.approx([87.27, 32.73])
        assert sum(weights) - 20 == pytest.approx(100)
        assert all(weight >= 0 for weight in weights)
        assert tab.table.item(tab._row_for_position(1), 5).text() == "Buy $27,270.00"
        assert tab.table.item(tab._row_for_position(1), 6).text() == "Buy 272.7000 shares"

        tab.cash_editor.setValue(120)
        tab.cash_editor.editingFinished.emit()
        assert "negative position weights" in tab.status.text()
        assert tab.cash_editor.value() == pytest.approx(-20)
        assert [tab.table.cellWidget(tab._row_for_position(position_id), 4).value()
                for position_id in (1, 2)] == pytest.approx(weights)
    finally:
        tab.close()
    reopened = CalcsTab(db, service, 1)
    try:
        assert reopened.cash_editor.value() == pytest.approx(-20)
        assert [reopened.table.cellWidget(reopened._row_for_position(position_id), 4).value()
                for position_id in (1, 2)] == pytest.approx([87.27, 32.73])
    finally:
        reopened.close()
        db.conn.close()






def test_scenario_edit_after_sort_and_persistence(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "scenario.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600), holding(2, 30000, 300)])
    tab = CalcsTab(db, service, 1)
    try:
        tab.table.sortItems(2, Qt.AscendingOrder)
        assert tab.table.item(0, 0).text() == "P2"
        up = tab.table.cellWidget(0, 9)
        up.setValue(120)
        up.editingFinished.emit()
        tab.table.cellWidget(0, 11).setCurrentIndex(1)
        down = tab.table.cellWidget(0, 12)
        down.setValue(10)
        down.editingFinished.emit()
        assert tab.table.item(0, 10).text() == "20.00 %"
        assert tab.table.item(0, 13).text() == "90.00 USD"
        assert tab.table.item(0, 14).text() == "2.00"
        assert tab.table.item(1, 14).text() == "—"
        tab.table.cellWidget(0, 8).setCurrentIndex(1)
        assert tab.scenarios["2"]["up_value"] == pytest.approx(20)
        assert tab.table.item(0, 10).text() == "120.00 USD"
        assert tab.table.item(0, 14).text() == "2.00"
        tab.set_column_decimals(14, 3)
        assert tab.table.item(0, 14).text() == "2.000"
    finally:
        tab.close()
    reopened = CalcsTab(db, service, 1)
    try:
        assert reopened.scenarios["2"]["up_mode"] == "percent"
        assert reopened.table.item(1, 14).text() == "2.000"
    finally:
        reopened.close()
        db.conn.close()


def test_scenario_arrows_move_between_mode_value_and_rows(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "scenario_arrows.sqlite3")
    db.set_portfolio_nav(1, 100000)
    tab = CalcsTab(db, Service([holding(1, 60000, 600), holding(2, 30000, 300)]), 1)
    try:
        tab.resize(2000, 600)
        tab.show()
        app.processEvents()
        tab.table.sortItems(2, Qt.AscendingOrder)
        first_id = tab.table.item(0, 0).data(Qt.UserRole)
        second_id = tab.table.item(1, 0).data(Qt.UserRole)
        mode = tab.table.cellWidget(0, 8)
        assert isinstance(mode, QComboBox)
        assert isinstance(tab.table.cellWidget(0, 9), QDoubleSpinBox)
        assert tab.table.cellWidget(0, 8) is not tab.table.cellWidget(0, 9)
        mode.setFocus()
        mode.setCurrentIndex(1)
        assert tab.scenarios[str(first_id)]["up_mode"] == "percent"
        assert tab.table.cellWidget(0, 8).hasFocus()
        mode = tab.table.cellWidget(0, 8)
        QTest.keyClick(mode, Qt.Key_Right)
        assert tab.table.cellWidget(0, 9).hasFocus()
        assert (tab.table.currentRow(), tab.table.currentColumn()) == (0, 9)
        value = tab.table.cellWidget(0, 9)
        QTest.keyClick(value.lineEdit(), Qt.Key_Left)
        assert tab.table.cellWidget(0, 8).hasFocus()
        mode = tab.table.cellWidget(0, 8)
        QTest.keyClick(mode, Qt.Key_Down)
        assert tab.table.cellWidget(1, 8).hasFocus()
        mode = tab.table.cellWidget(1, 8)
        QTest.keyClick(mode, Qt.Key_Right)
        value = tab.table.cellWidget(1, 9)
        assert value.hasFocus()
        value.setValue(125)
        QTest.keyClick(value.lineEdit(), Qt.Key_Up)
        assert tab.scenarios[str(second_id)]["up_value"] == 125
        assert tab.table.cellWidget(0, 9).hasFocus()
        assert tab.table.item(0, 0).data(Qt.UserRole) == first_id
        down_mode = tab.table.cellWidget(0, 11)
        down_mode.setFocus()
        QTest.keyClick(down_mode, Qt.Key_Right)
        assert tab.table.cellWidget(0, 12).hasFocus()
        QTest.keyClick(tab.table.cellWidget(0, 12).lineEdit(), Qt.Key_Left)
        assert tab.table.cellWidget(0, 11).hasFocus()
        header = tab.table.horizontalHeader()
        header.moveSection(header.visualIndex(12), header.visualIndex(8) + 1)
        tab.table.cellWidget(0, 8).setFocus()
        QTest.keyClick(tab.table.cellWidget(0, 8), Qt.Key_Right)
        assert tab.table.cellWidget(0, 12).hasFocus()
        assert tab.table.horizontalHeaderItem(14).text() == "Upside / downside"
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_idea_add_price_scenario_persistence_and_remove(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "ideas.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600), holding(2, 30000, 300)])
    tab = CalcsTab(db, service, 1)
    try:
        tab.idea_input.setText("new")
        tab.add_idea()
        assert tab.table.rowCount() == 3
        assert tab.ideas == {"NEW": {"price_usd": None}}
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        assert tab.table.item(row, 3).text() == "0.0000 %"
        tab.table.cellWidget(row, 4).setValue(10)
        tab.table.cellWidget(row, 4).editingFinished.emit()
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        assert tab.table.item(row, 5).text() == "Buy $10,000.00"
        assert tab.table.item(row, 6).text() == "—"
        tab.table.cellWidget(row, 7).setValue(200)
        tab.table.cellWidget(row, 7).editingFinished.emit()
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        assert tab.table.item(row, 6).text() == "Buy 50.0000 shares"
        tab.table.cellWidget(row, 8).setCurrentIndex(1)
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        tab.table.cellWidget(row, 9).setValue(25)
        tab.table.cellWidget(row, 9).editingFinished.emit()
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        tab.table.cellWidget(row, 11).setCurrentIndex(1)
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        tab.table.cellWidget(row, 12).setValue(10)
        tab.table.cellWidget(row, 12).editingFinished.emit()
        row = next(i for i in range(3) if tab.table.item(i, 0).text() == "NEW · idea")
        assert tab.table.item(row, 10).text() == "250.00 USD"
        assert tab.table.item(row, 13).text() == "180.00 USD"
        assert tab.table.item(row, 14).text() == "2.50"
        tab.idea_input.setText("new")
        tab.add_idea()
        assert "already an idea" in tab.idea_notice.text()
        tab.idea_input.setText("P1")
        tab.add_idea()
        assert "already in current positions" in tab.idea_notice.text()
        assert len(service.rows) == 2
        tab.table.sortItems(2, Qt.AscendingOrder)
        assert tab.table.item(0, 0).text() == "NEW · idea"
    finally:
        tab.close()
    reopened = CalcsTab(db, service, 1)
    try:
        row = next(i for i in range(3) if reopened.table.item(i, 0).text() == "NEW · idea")
        assert reopened.targets["idea:NEW"] == pytest.approx(.1)
        assert reopened.ideas["NEW"]["price_usd"] == 200
        assert reopened.table.item(row, 14).text() == "2.50"
        reopened.table.cellWidget(row, 15).click()
        assert reopened.table.rowCount() == 2
        assert "NEW" not in reopened.ideas
        assert "idea:NEW" not in reopened.targets
        assert "idea:NEW" not in reopened.scenarios
        assert len(service.rows) == 2
    finally:
        reopened.close()
        db.conn.close()


def test_rebalance_sort_keeps_idea_visible_in_a_long_portfolio(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "sorted_idea.sqlite3")
    db.set_portfolio_nav(1, 100000)
    tab = CalcsTab(db, Service([holding(i, 1000, i) for i in range(1, 30)]), 1)
    try:
        tab.resize(800, 500)
        tab.show()
        app.processEvents()
        tab.table.sortItems(2, Qt.DescendingOrder)
        tab.idea_input.setText("NEW")
        tab.add_idea()
        app.processEvents()
        row = tab._row_for_position("idea:NEW")
        assert row == 29
        rect = tab.table.visualItemRect(tab.table.item(row, 0))
        assert 0 <= rect.top() and rect.bottom() <= tab.table.viewport().height()
        tab.targets["idea:NEW"] = .1
        tab.scenarios["idea:NEW"] = {"up_mode": "percent", "up_value": 25,
                                      "down_mode": "percent", "down_value": 10}
        for column in (2, 0):
            tab.table.sortItems(column, Qt.DescendingOrder)
            app.processEvents()
            row = tab._row_for_position("idea:NEW")
            assert row == 29
            rect = tab.table.visualItemRect(tab.table.item(row, 0))
            assert 0 <= rect.top() and rect.bottom() <= tab.table.viewport().height()
            tab.refresh()
            app.processEvents()
            assert tab._row_for_position("idea:NEW") == row
            assert tab.targets["idea:NEW"] == pytest.approx(.1)
            assert tab.scenarios["idea:NEW"]["up_value"] == 25
    finally:
        tab.close()
        db.conn.close()


def test_acquired_idea_becomes_one_real_position_and_retains_plan(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "acquired.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600)])
    tab = CalcsTab(db, service, 1)
    try:
        tab.idea_input.setText("NEW")
        tab.add_idea()
        tab.targets["idea:NEW"] = .2
        tab.scenarios["idea:NEW"] = {"up_mode": "percent", "up_value": 25,
                                      "down_mode": "percent", "down_value": 10}
        tab._save()
        tab._save_scenarios()
        acquired = holding(3, 10000, 100)
        acquired["ib_symbol"] = "NEW"
        service.rows.append(acquired)
        tab.refresh()
        assert tab.table.rowCount() == 2
        assert tab.ideas == {}
        assert tab.targets["3"] == .2
        assert tab.scenarios["3"]["up_value"] == 25
    finally:
        tab.close()
        db.conn.close()




def test_rebalance_keyboard_on_cash_and_idea_price(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "keyboard_idea.sqlite3")
    db.set_portfolio_nav(1, 100000)
    tab = CalcsTab(db, Service([holding(1, 60000, 600)]), 1)
    try:
        tab.show()
        app.processEvents()
        tab.cash_editor.setFocus()
        tab.cash_editor.setValue(50)
        QTest.keyClick(tab.cash_editor.lineEdit(), Qt.Key_Escape)
        assert tab.mode == "individual"
        assert tab.cash_editor.value() == 40
        assert tab.table.hasFocus()
        tab.cash_editor.setFocus()
        tab.cash_editor.setValue(45)
        QTest.keyClick(tab.cash_editor.lineEdit(), Qt.Key_Down)
        assert tab.mode == "proportional"
        assert tab.cash_target == pytest.approx(.45)
        assert (tab.table.currentRow(), tab.table.currentColumn()) == (0, 4)
        tab.idea_input.setText("NEW")
        tab.add_idea()
        app.processEvents()
        row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "NEW · idea")
        price = tab.table.cellWidget(row, 7)
        price.setFocus()
        app.processEvents()
        price = tab.table.cellWidget(row, 7)
        price.setValue(200)
        QTest.keyClick(price.lineEdit(), Qt.Key_Left)
        assert tab.ideas["NEW"]["price_usd"] == 200
        row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "NEW · idea")
        assert (tab.table.currentRow(), tab.table.currentColumn()) == (row, 6)
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_keyboard_reaches_and_edits_cash_target(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "cash_keyboard.sqlite3")
    db.set_portfolio_nav(1, 100000)
    tab = CalcsTab(db, Service([holding(1, 60000, 600)]), 1)
    try:
        tab.show()
        app.processEvents()
        tab.table.select_cell(0, 4, focus=True)
        QTest.keyClick(tab.table, Qt.Key_Up)
        assert tab.cash_editor.hasFocus()
        assert tab.cash_editor.lineEdit().selectedText()
        QTest.keyClicks(tab.cash_editor.lineEdit(), "25")
        QTest.keyClick(tab.cash_editor.lineEdit(), Qt.Key_Return)
        assert tab.cash_target == pytest.approx(.25)
        assert (tab.table.currentRow(), tab.table.currentColumn()) == (0, 4)

        tab.cash_editor.setFocus()
        QTest.keyClick(tab.cash_editor.lineEdit(), Qt.Key_F2)
        assert tab.cash_editor.lineEdit().selectedText()

        tab.view_choice.setCurrentIndex(tab.view_choice.findData("cases"))
        tab.table.select_cell(0, 0, focus=True)
        QTest.keyClick(tab.table, Qt.Key_Up)
        assert tab.cash_editor.hasFocus()
        tab.cash_editor.setFocus()
        QTest.keyClick(tab.cash_editor.lineEdit(), Qt.Key_Down)
        assert tab.table.currentRow() == 0
        assert not tab.table.isColumnHidden(tab.table.currentColumn())
    finally:
        tab.close()
        db.conn.close()


def test_ctrl_minus_zeros_holdings_removes_ideas_and_is_rebalance_only(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "remove_shortcut.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600)])
    tab = CalcsTab(db, service, 1)
    tabs = QTabWidget()
    other = QWidget()
    other.setFocusPolicy(Qt.StrongFocus)
    tabs.addTab(tab, "Rebalance")
    tabs.addTab(other, "Other")
    try:
        tab.idea_input.setText("NEW")
        tab.add_idea()
        tab.targets["idea:NEW"] = .15
        tab.scenarios["idea:NEW"] = {"up_mode": "percent", "up_value": 20,
                                       "down_mode": "percent", "down_value": 10}
        tab._save()
        tab._save_scenarios()
        tab.refresh()
        tab.table.sortItems(2, Qt.AscendingOrder)
        tabs.show()
        app.processEvents()

        idea_row = tab._row_for_position("idea:NEW")
        tab.table.select_cell(idea_row, 4, focus=True)
        QTest.keyClick(tab.table, Qt.Key_Space, Qt.ShiftModifier)
        assert len(tab.table.selectionModel().selectedRows()) == 1
        tabs.setCurrentWidget(other)
        other.setFocus()
        QTest.keyClick(other, Qt.Key_Minus, Qt.ControlModifier)
        assert "NEW" in tab.ideas
        assert tab.targets.get("1") != 0

        tabs.setCurrentWidget(tab)
        held_row = tab._row_for_position(1)
        tab.table.select_cell(held_row, 4, focus=True)
        QTest.keyClick(tab.table, Qt.Key_Minus, Qt.ControlModifier)
        assert tab.targets.get("1") != 0
        QTest.keyClick(tab.table, Qt.Key_Space, Qt.ShiftModifier)
        assert len(tab.table.selectionModel().selectedRows()) == 1
        other_row = tab._row_for_position("idea:NEW")
        tab.table.selectionModel().select(tab.table.model().index(other_row, 0), QItemSelectionModel.Select)
        QTest.keyClick(tab.table, Qt.Key_Minus, Qt.ControlModifier)
        assert tab.targets["1"] == 0
        QTest.keyClick(tab.table, Qt.Key_Space, Qt.ShiftModifier)
        QTest.keyClick(tab.table, Qt.Key_Minus, Qt.ControlModifier)
        assert tab.targets["1"] == 0
        assert tab.table.rowCount() == 2
        assert tab._row_for_position(1) >= 0
        assert len(service.rows) == 1

        idea_row = tab._row_for_position("idea:NEW")
        tab.table.select_cell(idea_row, 4, focus=True)
        editor = tab.table.cellWidget(idea_row, 4)
        editor.setFocus()
        QTest.keyClick(editor.lineEdit(), Qt.Key_Minus, Qt.ControlModifier)
        assert "NEW" in tab.ideas
        tab.table.setFocus()
        QTest.keyClick(tab.table, Qt.Key_Space, Qt.ShiftModifier)
        assert len(tab.table.selectionModel().selectedRows()) == 1
        QTest.keyClick(tab.table, Qt.Key_Minus, Qt.ControlModifier)
        app.processEvents()
        assert "NEW" not in tab.ideas
        assert "idea:NEW" not in tab.targets
        assert "idea:NEW" not in tab.scenarios
        assert tab.table.rowCount() == 1
        assert tab._row_for_position(1) == 0
    finally:
        tabs.close()

    reopened = CalcsTab(db, service, 1)
    try:
        assert reopened.targets["1"] == 0
        assert reopened.ideas == {}
    finally:
        reopened.close()
        db.conn.close()












def test_rebalance_focus_views_reduce_visible_columns_and_persist(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "focus_views.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(1, 60000, 600)])
    db.set_setting("calcs_view_mode_1", "allocation")
    tab = CalcsTab(db, service, 1)
    try:
        assert tab.view_mode == "all"
        assert [tab.view_choice.itemData(i) for i in range(tab.view_choice.count())] == [
            "all", "allocation", "cases"]
        assert not tab.table.isColumnHidden(4)
        assert not tab.table.isColumnHidden(8)
        assert tab.table.isColumnHidden(15)
        tab.view_choice.setCurrentIndex(tab.view_choice.findData("allocation"))
        assert tab.view_mode == "allocation"
        assert tab.table.isColumnHidden(8)
        tab.table.select_cell(0, 4, focus=True)
        tab.view_choice.setCurrentIndex(tab.view_choice.findData("cases"))
        assert tab.view_mode == "cases"
        assert tab.table.isColumnHidden(4)
        assert not tab.table.isColumnHidden(8)
        assert not tab.table.isColumnHidden(14)
        header = tab.table.horizontalHeader()
        assert header.sectionPosition(7) == header.sectionPosition(0) + header.sectionSize(0)
        tab.refresh()
        assert header.sectionPosition(7) == header.sectionPosition(0) + header.sectionSize(0)
        assert tab.table.currentColumn() == 0
        tab.table.select_cell(0, 14, focus=True)
        QTest.keyClick(tab.table, Qt.Key_Right)
        assert tab.table.currentColumn() == 14
        tab.idea_input.setText("NEW")
        tab.add_idea()
        assert not tab.table.isColumnHidden(15)
    finally:
        tab.close()
    reopened = CalcsTab(db, service, 1)
    try:
        assert reopened.view_mode == "cases"
        assert reopened.view_choice.currentData() == "cases"
        assert reopened.table.isColumnHidden(4)
        assert not reopened.table.isColumnHidden(8)
        reopened.view_choice.setCurrentIndex(reopened.view_choice.findData("all"))
        assert not reopened.table.isColumnHidden(4)
        assert not reopened.table.isColumnHidden(8)
    finally:
        reopened.close()
        db.conn.close()




def test_rebalance_refresh_uses_latest_values_without_local_pause(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "paused_rebalance.sqlite3")
    db.set_portfolio_nav(1, 100000)
    db.set_account_value(1, "TotalCashValue", 40000, "USD", "2026-10-07T12:00:00")
    service = Service([holding(1, 60000, 600, price=100)])
    tab = CalcsTab(db, service, 1)
    try:
        assert not hasattr(tab, "pause_button")
        assert not tab.table.isColumnHidden(8)
        assert not tab.table.isColumnHidden(9)
        service.rows[0]["value"] = 50000
        service.rows[0]["last_px"] = 120
        db.set_account_value(1, "TotalCashValue", 50000, "USD", "2026-10-07T12:01:00")
        tab.refresh()
        assert tab.table.item(0, 3).text() == "50.0000 %"
        assert tab.table.item(0, 7).text() == "120.00 USD"
        assert "$50,000.00" in tab.cash_value.text()
        tab.table.cellWidget(0, 4).setValue(50)
        tab.table.cellWidget(0, 4).editingFinished.emit()
        assert tab.table.item(0, 5).text() == "—"
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_sorted_quote_refresh_keeps_each_instrument_on_one_row(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "sorted_rebalance.sqlite3")
    db.set_portfolio_nav(1, 100000)
    service = Service([holding(i, i * 1000, 10, price=i * 100) for i in range(1, 5)])
    tab = CalcsTab(db, service, 1)
    try:
        tab.table.sortItems(2, Qt.AscendingOrder)
        service.rows[0]["value"] = 5000
        service.rows[0]["last_px"] = 500
        tab.refresh()
        assert [(tab.table.item(row, 0).data(Qt.UserRole),
                 tab.table.item(row, 0).text(),
                 tab.table.item(row, 2).sort_value)
                for row in range(tab.table.rowCount())] == [
                    (2, "P2", 2000), (3, "P3", 3000), (4, "P4", 4000), (1, "P1", 5000)]
        assert tab._table_matches(service.rows)
        tab.table.item(0, 0).setData(Qt.UserRole, 3)
        assert not tab._table_matches(service.rows)
        tab.refresh()
        assert [tab.table.item(row, 0).data(Qt.UserRole)
                for row in range(tab.table.rowCount())] == [2, 3, 4, 1]
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_earnings_column_shows_stock_date_and_explains_missing_values(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "earnings_rebalance.sqlite3")
    db.set_portfolio_nav(1, 100000)
    stock = holding(1, 60000, 600)
    stock["con_id"] = 101
    option = holding(2, 10000, 1, security_type="OPT")
    service = Service([stock, option])
    tab = CalcsTab(db, service, 1)
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17)
    requested = []
    monkeypatch.setattr(provider, "request_earnings", lambda stocks: requested.extend(stocks))
    provider.earnings[101] = {"result": {"date": "2026-11-03", "time_of_day": "AFTER MARKET",
                                        "status": "CONFIRMED"}, "checked": date.today().isoformat()}
    try:
        tab.market = provider
        tab.refresh()
        assert tab.table.horizontalHeaderItem(16).text() == "Next earnings"
        assert requested == [(1, 101)]
        stock_row = tab._row_for_position(1)
        option_row = tab._row_for_position(2)
        assert tab.table.item(stock_row, 16).text() == "2026-11-03"
        assert "AFTER MARKET" in tab.table.item(stock_row, 16).toolTip()
        assert tab.table.item(option_row, 16).text() == "—"
        tab.view_choice.setCurrentIndex(tab.view_choice.findData("allocation"))
        assert not tab.table.isColumnHidden(16)
        tab.view_choice.setCurrentIndex(tab.view_choice.findData("cases"))
        assert tab.table.isColumnHidden(16)
    finally:
        tab.close()
        db.conn.close()


def test_rebalance_previous_header_layout_keeps_order_with_new_column(tmp_path):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "earnings_header.sqlite3")
    service = Service([holding(1, 100, 1)])
    from app.views.calcs_ui import CalcsTable
    legacy = CalcsTable(0, 16)
    legacy_header = legacy.horizontalHeader()
    legacy_header.moveSection(legacy_header.visualIndex(4), 2)
    legacy.setColumnWidth(0, 205)
    db.set_setting("calcs_header_v5_1", bytes(legacy_header.saveState().toBase64()).decode("ascii"))
    reopened = CalcsTab(db, service, 1)
    try:
        header = reopened.table.horizontalHeader()
        assert header.visualIndex(4) == 2
        assert reopened.table.columnWidth(0) == 205
        assert header.visualIndex(16) == 16
    finally:
        reopened.close()
        db.conn.close()






def test_ibkr_idea_requires_suggestion_and_uses_delayed_quote_and_fx(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "broker_ideas.sqlite3")
    db.set_portfolio_nav(1, 100000)
    db.set_fx_rate("EUR", date.today().isoformat(), 1.2)
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 42)
    provider.connected = True
    requested = []
    monkeypatch.setattr(provider, "search", lambda text: requested.append(text) or 1234)
    monkeypatch.setattr(provider, "_subscribe_idea", lambda idea_id, details: requested.append((idea_id, details["con_id"])))
    monkeypatch.setattr(provider, "_subscribe_fx", lambda currency: requested.append(("fx", currency)))
    tab = CalcsTab(db, Service([holding(1, 60000, 600)]), 1)
    try:
        tab.set_market_provider(provider)
        tab.idea_input.setText("SAP")
        tab.add_idea()
        assert not tab.ideas
        assert "Choose a stock" in tab.idea_notice.text()
        tab._search_ideas()
        assert requested == ["SAP"]
        provider.search_results[1234] = [
            {"con_id": 900000001, "symbol": "SAP", "security_type": "STK", "currency": "EUR",
             "primary_exchange": "IBIS", "description": "SAP SE"},
            {"con_id": 7, "symbol": "SAP", "security_type": "OPT", "currency": "EUR"}]
        tab.process_search_results()
        assert len(tab._search_labels) == 1
        label = next(iter(tab._search_labels))
        tab._select_suggestion(label)
        tab.idea_input.setText("SAP")
        tab.add_idea()
        assert not tab.ideas
        assert "Choose a stock" in tab.idea_notice.text()
        tab._select_suggestion(label)
        provider.connected = False
        tab.add_idea()
        assert not tab.ideas
        assert "Connect to IBKR" in tab.idea_notice.text()
        provider.connected = True
        tab.add_idea()
        assert tab.ideas["SAP"]["con_id"] == 900000001
        assert ("idea:SAP", 900000001) in requested
        assert ("fx", "EUR") in requested
        row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "SAP · idea")
        assert tab.table.item(row, 7).text() == "—"
        provider.events.put(("quote", ["idea:SAP"], "delayed_last", 100, "Delayed"))
        ids = provider.drain()
        tab.cache_idea_quotes(ids)
        tab.refresh()
        row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "SAP · idea")
        assert tab.table.item(row, 7).text() == "100.00 EUR"
        assert "DELAYED" in tab.table.item(row, 7).toolTip().upper()
        tab.table.cellWidget(row, 4).setValue(12)
        tab.table.cellWidget(row, 4).editingFinished.emit()
        row = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "SAP · idea")
        assert tab.table.item(row, 6).text() == "Buy 100.0000 shares"
    finally:
        tab.close()
    reopened = CalcsTab(db, Service([holding(1, 60000, 600)]), 1)
    try:
        assert reopened.ideas["SAP"]["con_id"] == 900000001
        assert reopened.ideas["SAP"]["price_usd"] == 100
        row = next(i for i in range(reopened.table.rowCount()) if reopened.table.item(i, 0).text() == "SAP · idea")
        assert "CACHED QUOTE" in reopened.table.item(row, 7).toolTip()
    finally:
        reopened.close()
        db.conn.close()






def test_ibkr_idea_subscription_uses_selected_contract_and_cancels_on_remove(monkeypatch):
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 44)
    provider.stale_con_ids = set()
    market_requests = []
    canceled = []
    history_requests = []
    monkeypatch.setattr(provider.client, "reqMktData", lambda request_id, contract, *_:
                        market_requests.append((request_id, contract.conId, contract.secType, contract.currency)))
    monkeypatch.setattr(provider.client, "cancelMktData", canceled.append)
    monkeypatch.setattr(provider.client, "reqHistoricalData", lambda request_id, contract, *rest:
                        history_requests.append(contract.secType))
    provider.set_idea_contracts({"SAP": {"con_id": 900000001, "symbol": "SAP", "security_type": "STK",
                                         "currency": "EUR", "primary_exchange": "IBIS"}})
    provider._request_all()
    assert any(con_id == 900000001 and sec_type == "STK" for _, con_id, sec_type, _ in market_requests)
    assert "STK" not in history_requests
    idea_request = next(request_id for request_id, ids in provider.client.ticker_ids.items()
                        if "idea:SAP" in ids)
    provider.set_idea_contracts({})
    assert idea_request in canceled
