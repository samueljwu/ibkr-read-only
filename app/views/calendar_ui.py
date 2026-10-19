"""Reusable interval controls and adaptive calendar grids."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel,
                               QSizePolicy, QWidget)

from app.views.action_icons import action_icon
from app.views.ui_theme import COLORS, style_toolbar_dropdown
from app.views.widgets import button, section_title


class CalendarToolbar(QWidget):
    """Interval and optional filters on one row."""

    def __init__(self, intervals, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._row = row
        row.addWidget(QLabel("Interval"))
        self.interval_combo = style_toolbar_dropdown(QComboBox())
        self.interval_combo.addItems(intervals)
        self.interval_combo.setFixedWidth(120)
        row.addWidget(self.interval_combo)
        self._next_control_index = row.count()
        row.addStretch()

    def add_control(self, widget):
        """Place another filter beside the interval selector."""
        self._row.insertWidget(self._next_control_index, widget)
        self._next_control_index += 1


class CalendarNavigation(QWidget):
    """Theme-aware period navigation with room for a summary."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._row = row
        self.previous_button = button("")
        self.previous_button.setToolTip("Previous period")
        self.previous_button.setAccessibleName("Previous period")
        self.previous_button.setFixedSize(32, 32)
        self.previous_button.setIconSize(QSize(16, 16))
        row.addWidget(self.previous_button)
        self.period_label = section_title("")
        self.period_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self.period_label)
        self.next_button = button("")
        self.next_button.setToolTip("Next period")
        self.next_button.setAccessibleName("Next period")
        self.next_button.setFixedSize(32, 32)
        self.next_button.setIconSize(QSize(16, 16))
        row.addWidget(self.next_button)
        self.set_theme("light")

    def add_detail(self, widget):
        """Show contextual information beside the current period."""
        self._row.addWidget(widget, 1)

    def set_period(self, label, can_previous, can_next):
        self.period_label.setText(label)
        self.previous_button.setEnabled(can_previous)
        self.next_button.setEnabled(can_next)

    def set_theme(self, theme):
        color = COLORS[theme]["text"]
        self.previous_button.setIcon(action_icon("chevron_left", color))
        self.next_button.setIcon(action_icon("chevron_right", color))


class DecimalPlacesControl(QWidget):
    """Compact, reusable precision control for a displayed value."""

    valueChanged = Signal(int)

    def __init__(self, label, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.caption = QLabel(f"{label} decimals")
        self.caption.setToolTip(f"Decimal places for {label}")
        row.addWidget(self.caption)
        self.dropdown = style_toolbar_dropdown(QComboBox())
        self.dropdown.addItems([str(places) for places in range(31)])
        self.dropdown.setCurrentIndex(2)
        self.dropdown.setFixedWidth(86)
        self.dropdown.setToolTip(f"Decimal places for {label.lower()}")
        self.dropdown.setAccessibleName(f"{label} decimal places")
        self.dropdown.currentIndexChanged.connect(self.valueChanged.emit)
        row.addWidget(self.dropdown)

    def value(self):
        return self.dropdown.currentIndex()

    def setValue(self, places):
        if not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 30:
            raise ValueError("Choose 0–30 decimal places")
        self.dropdown.setCurrentIndex(places)


class CalendarCell(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calendarCell")
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setContentsMargins(8, 7, 6, 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(58)

    def set_content(self, text, detail, amount=None, *, active=True):
        self.setText(text)
        self.setToolTip(detail)
        self.setProperty("calendarState", "inactive" if not active else
                         "positive" if amount is not None and amount > 0 else
                         "negative" if amount is not None and amount < 0 else "normal")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_empty(self):
        self.clear()
        self.setToolTip("")
        self.setProperty("calendarState", "empty")
        self.style().unpolish(self)
        self.style().polish(self)


class CalendarGrid(QWidget):
    def __init__(self, cell_count, columns, weekday_headers=(), parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self._header_rows = int(bool(weekday_headers))
        for column, name in enumerate(weekday_headers):
            label = QLabel(name)
            label.setObjectName("metricLabel")
            label.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(label, 0, column)
        self.cells = [CalendarCell() for _ in range(cell_count)]
        self.arrange(cell_count, columns)

    def arrange(self, count, columns):
        for cell in self.cells:
            self.grid.removeWidget(cell)
            cell.hide()
        rows = (count + columns - 1) // columns
        for row in range(max(getattr(self, "_rows", 0), rows)):
            self.grid.setRowStretch(row + self._header_rows, 0)
        for column in range(max(getattr(self, "_columns", 0), columns)):
            self.grid.setColumnStretch(column, 0)
        for index, cell in enumerate(self.cells[:count]):
            self.grid.addWidget(cell, index // columns + self._header_rows, index % columns)
            cell.show()
        for row in range(rows):
            self.grid.setRowStretch(row + self._header_rows, 1)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)
        self._rows, self._columns = rows, columns
