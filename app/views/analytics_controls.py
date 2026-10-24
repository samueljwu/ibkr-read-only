"""Date controls shared by account-history views; calculations stay in AnalyticsTab."""

from PySide6.QtCore import QDate, QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox

from app.views.date_input import CalendarDateEdit
from app.views.toolbar import Toolbar
from app.views.ui_theme import style_toolbar_dropdown
from app.views import widgets


class HistoryControls(Toolbar):
    presetChanged = Signal(str)
    datesChanged = Signal(object, object)
    resetRequested = Signal()

    def __init__(self, subject=()):
        super().__init__()
        if subject:
            self.add_group(*subject)
        self.range_combo = style_toolbar_dropdown(QComboBox())
        self.range_combo.addItems(("5 days", "1 month", "3 months", "6 months",
                                   "YTD", "1 year", "All", "Custom"))
        self.range_combo.setCurrentText("3 months")
        self.range_group = self.add_control(self.range_combo, "Range")
        self.start_date = CalendarDateEdit(QDate.currentDate().addMonths(-3))
        self.end_date = CalendarDateEdit(QDate.currentDate())
        self.start_group = self.add_control(self.start_date, "From")
        self.end_group = self.add_control(self.end_date, "To")
        self.reset_button = widgets.button("Reset range", self.resetRequested.emit)
        self.reset_group = self.add_control(self.reset_button)
        self.range_combo.currentTextChanged.connect(self.presetChanged.emit)
        for editor in (self.start_date, self.end_date):
            editor.dateChanged.connect(
                lambda: self.datesChanged.emit(self.start_date.date(), self.end_date.date()))
        self.show_range(True)

    def set_range(self, preset, first, last):
        blockers = [QSignalBlocker(control) for control in
                    (self.range_combo, self.start_date, self.end_date)]
        self.range_combo.setCurrentText(preset)
        self.start_date.setDate(first)
        self.end_date.setDate(last)
        del blockers

    def show_range(self, visible):
        self.range_group.setVisible(visible)
        custom = visible and self.range_combo.currentText() == "Custom"
        for group in (self.start_group, self.end_group, self.reset_group):
            group.setVisible(custom)
