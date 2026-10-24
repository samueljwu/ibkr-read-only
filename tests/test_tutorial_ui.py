import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import app.ui as ui
from app.storage.database import Database


def test_tutorial_walks_tabs_and_restores_starting_tab(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "tour.sqlite3"))
    window = ui.MainWindow()
    try:
        window.show()
        app.processEvents()
        window.tabs.setCurrentWidget(window.cash_tab)
        window.start_tutorial()
        assert window.tutorial.isVisible()
        assert window.tabs.tabText(window.tabs.currentIndex()) == "Overview"
        assert not window.tutorial.back_button.isEnabled()
        assert window.tutorial.progress.text() == "Step 1 of 9"

        for title in ("Trades", "Lots", "Cash", "Analytics", "Rebalance"):
            window.tutorial.next_button.click()
            app.processEvents()
            assert window.tabs.tabText(window.tabs.currentIndex()) == title
            navigation = window.navigation
            item_rect = navigation.visualItemRect(navigation.currentItem())
            expected = QRect(navigation.viewport().mapTo(window, item_rect.topLeft()), item_rect.size())
            assert window.tutorial.target_rect == expected
            assert not window.tutorial.card.geometry().intersects(expected)
        assert window.tutorial.target_rect.isValid()
        window.tutorial.back_button.click()
        assert window.tabs.tabText(window.tabs.currentIndex()) == "Analytics"

        window.resize(1050, 720)
        app.processEvents()
        assert window.tutorial.geometry() == window.rect()
        assert window.tutorial.card.geometry().intersects(window.tutorial.rect())
        navigation = window.navigation
        item_rect = navigation.visualItemRect(navigation.currentItem())
        assert window.tutorial.target_rect == QRect(
            navigation.viewport().mapTo(window, item_rect.topLeft()), item_rect.size())

        for _ in range(4):
            window.tutorial.next_button.click()
        assert window.tutorial.next_button.text() == "Finish"
        window.tutorial.next_button.click()
        assert not window.tutorial.isVisible()
        assert window.tabs.currentWidget() is window.cash_tab
    finally:
        window.close()


def test_tutorial_can_start_from_help_and_exit_with_escape(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "help_tour.sqlite3"))
    window = ui.MainWindow()
    try:
        window.show()
        window.open_help()
        assert window.help_dialog.isVisible()
        assert window.help_navigation.item(0).text() == "Take a tour…"
        assert not any(button.text() == "Take a tour…"
                       for button in window.help_dialog.findChildren(QPushButton))
        window.help_navigation.setCurrentRow(0)
        assert window.tutorial.isVisible()
        assert not window.help_dialog.isVisible()
        window.tutorial.next_button.setFocus()
        QTest.keyClick(window.tutorial.next_button, Qt.Key_Escape)
        assert not window.tutorial.isVisible()
        action = next(action for action in window.menuBar().actions()
                      if action.text() == "Help")
        assert any(item.text() == "Take a tour…" for item in action.menu().actions())
    finally:
        window.close()
