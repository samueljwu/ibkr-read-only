"""Choose among separate portfolio databases."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox, QVBoxLayout)

from app.views import widgets
from app.views.ui_theme import apply_widget_theme


class DatabaseSelectionWindow(QDialog):
    def __init__(self, open_database, create_database, remove_database):
        super().__init__()
        self._open_database = open_database
        self._remove_database = remove_database
        self._current = None
        self.setWindowTitle("Portfolios — IBKR Dashboard")
        self.resize(660, 410)
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "dialog")
        self.heading = QLabel("Portfolios")
        self.heading.setObjectName("heading")
        layout.addWidget(self.heading)
        self.explanation = widgets.hint("")
        layout.addWidget(self.explanation)

        card, card_layout = widgets.card()
        card_layout.addWidget(widgets.section_title("Saved portfolios"))
        self.saved_list = QListWidget()
        self.saved_list.setObjectName("portfolioList")
        self.saved_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.saved_list.setTextElideMode(Qt.ElideMiddle)
        self.saved_list.itemDoubleClicked.connect(self._open_saved)
        card_layout.addWidget(self.saved_list)
        open_row = QHBoxLayout()
        self.remove_selected_button = widgets.button(
            "Remove from list…", self._remove_saved, variant="danger")
        open_row.addWidget(self.remove_selected_button)
        open_row.addStretch()
        self.open_selected_button = widgets.button(
            "Open selected portfolio", self._open_saved, variant="primary")
        open_row.addWidget(self.open_selected_button)
        card_layout.addLayout(open_row)
        layout.addWidget(card, 1)
        self.saved_list.currentItemChanged.connect(self._update_buttons)

        footer = QHBoxLayout()
        footer.addWidget(widgets.button("Open existing database…", self._choose_database))
        footer.addWidget(widgets.button("Create new portfolio", create_database))
        footer.addStretch()
        footer.addWidget(widgets.button("Close", self.close))
        layout.addLayout(footer)
        self.set_appearance("light")

    def set_appearance(self, theme, zoom_percent=100, font=None):
        if font is not None:
            self.setFont(font)
        apply_widget_theme(self, theme, zoom_percent)

    def refresh(self, paths, *, current=None):
        self.saved_list.clear()
        self._current = Path(current).resolve() if current is not None else None
        self.heading.setText("Portfolios" if current else "No database selected")
        self.explanation.setText(
            "Open a saved portfolio, choose another database, or create a new portfolio."
            if current else "The previously selected database is unavailable. "
                            "Open another portfolio or create a new one.")
        first_available = None
        for path in paths:
            path = Path(path)
            available = path.is_file()
            item = QListWidgetItem(str(path) + ("  (missing)" if not available else ""))
            item.setData(Qt.UserRole, str(path))
            item.setData(Qt.UserRole + 1, available)
            item.setToolTip(str(path))
            self.saved_list.addItem(item)
            if available and first_available is None:
                first_available = item
            if current is not None and path == Path(current):
                self.saved_list.setCurrentItem(item)
        if self.saved_list.currentItem() is None and first_available is not None:
            self.saved_list.setCurrentItem(first_available)
        self._update_buttons()

    def _update_buttons(self, _current=None, _previous=None):
        item = self.saved_list.currentItem()
        self.open_selected_button.setEnabled(item is not None and bool(item.data(Qt.UserRole + 1)))
        self.remove_selected_button.setEnabled(
            item is not None and Path(item.data(Qt.UserRole)).resolve() != self._current)

    def _open_saved(self, _item=None):
        item = self.saved_list.currentItem()
        if item is not None and item.data(Qt.UserRole + 1):
            self._open_database(item.data(Qt.UserRole))

    def _remove_saved(self):
        item = self.saved_list.currentItem()
        if item is None or not self.remove_selected_button.isEnabled():
            return
        path = item.data(Qt.UserRole)
        if QMessageBox.question(
                self, "Remove saved portfolio",
                f"Remove this portfolio from the saved list?\n\n{path}\n\n"
                "The database file and its data will remain on disk.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._remove_database(path)

    def _choose_database(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open portfolio database", "", "Portfolio database (portfolio.sqlite3)")
        if path:
            self._open_database(path)
