"""Reusable ISO date entry with a keyboard-friendly calendar grid."""

from PySide6.QtCore import QDate, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPen, QTextCharFormat
from PySide6.QtWidgets import (QCalendarWidget, QHBoxLayout, QLineEdit,
                               QMenu, QToolButton, QWidget, QWidgetAction)

from app.views.ui_theme import COLORS


class DatePickerCalendar(QCalendarWidget):
    """Calendar with neutral day tiles that follow the app's theme."""

    def __init__(self, colors, parent=None):
        super().__init__(parent)
        self.colors = colors
        self.range_start = None
        self.range_end = None
        self.setObjectName("datePickerCalendar")
        self.setFirstDayOfWeek(Qt.Monday)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        self.setGridVisible(False)
        self.setMinimumSize(310, 260)
        header = QTextCharFormat()
        header.setForeground(QColor(colors["muted"]))
        header.setFontWeight(QFont.DemiBold)
        self.setHeaderTextFormat(header)
        for weekday in (Qt.Monday, Qt.Tuesday, Qt.Wednesday, Qt.Thursday,
                        Qt.Friday, Qt.Saturday, Qt.Sunday):
            self.setWeekdayTextFormat(weekday, header)
        for name, label in (("qt_calendar_prevmonth", "‹"),
                            ("qt_calendar_nextmonth", "›")):
            button = self.findChild(QToolButton, name)
            if button is not None:
                button.setArrowType(Qt.NoArrow)
                button.setIcon(QIcon())
                button.setText(label)
                button.setToolButtonStyle(Qt.ToolButtonTextOnly)

    def setRange(self, first, last):
        self.range_start = first if first.isValid() and last.isValid() and first <= last else None
        self.range_end = last if self.range_start is not None else None
        self.updateCells()

    def paintCell(self, painter, rect, day):
        colors = self.colors
        painter.save()
        painter.fillRect(rect, QColor(colors["surface"]))
        tile = rect.adjusted(3, 2, -3, -2)
        selected = day == self.selectedDate()
        in_range = (self.range_start is not None and self.range_start <= day <= self.range_end)
        if selected or in_range:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["calendar_selected"] if selected else
                                    colors["calendar_range"]))
            painter.drawRoundedRect(tile, 6, 6)
        elif day == QDate.currentDate() and self.minimumDate() <= day <= self.maximumDate():
            painter.setPen(QPen(QColor(colors["line"]), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(tile.adjusted(0, 0, -1, -1), 6, 6)
        available = self.minimumDate() <= day <= self.maximumDate()
        current_month = day.year() == self.yearShown() and day.month() == self.monthShown()
        painter.setPen(QColor(colors["surface"] if selected else
                               colors["text"] if available and current_month else colors["muted"]))
        painter.drawText(rect, Qt.AlignCenter, str(day.day()))
        painter.restore()


class CalendarDateInput(QWidget):
    """Allow typing an ISO date or choosing one from a bounded calendar."""

    dateChanged = Signal(QDate)

    def __init__(self, selected, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.line_edit = QLineEdit()
        self.line_edit.setInputMask("0000-00-00;_")
        self.line_edit.setText(selected.toString("yyyy-MM-dd"))
        self.line_edit.setMinimumWidth(136)
        self.line_edit.setToolTip("Type a date as YYYY-MM-DD.")
        self.setFocusProxy(self.line_edit)
        row.addWidget(self.line_edit)
        self.calendar_button = QToolButton()
        self.calendar_button.setObjectName("calendarDateButton")
        self.calendar_button.setArrowType(Qt.DownArrow)
        self.calendar_button.setFixedWidth(28)
        self.calendar_button.setToolTip("Choose a date from the calendar")
        self.calendar_button.setFocusPolicy(Qt.NoFocus)
        self.calendar_button.clicked.connect(self._show_calendar)
        row.addWidget(self.calendar_button)
        self._minimum = None
        self._maximum = None
        self._range_partner = None
        self._starts_range = False
        self.line_edit.editingFinished.connect(self._edited)

    def date(self):
        return QDate.fromString(self.line_edit.text(), "yyyy-MM-dd")

    def setDate(self, selected):
        if selected.isValid():
            self.line_edit.setText(selected.toString("yyyy-MM-dd"))
            self.dateChanged.emit(selected)

    def setMinimumDate(self, minimum):
        self._minimum = minimum

    def setMaximumDate(self, maximum):
        self._maximum = maximum

    def setRangePartner(self, partner, *, starts_range=False):
        """Shade the ordered span between this field and another date field."""
        self._range_partner = partner
        self._starts_range = starts_range

    def _edited(self):
        selected = self.date()
        if selected.isValid():
            self.dateChanged.emit(selected)

    def _theme_colors(self):
        owner = self
        while owner is not None:
            theme = getattr(owner, "theme", None)
            if theme in COLORS:
                return COLORS[theme]
            owner = owner.parentWidget()
        return COLORS["light"]

    def _show_calendar(self):
        menu = QMenu(self)
        menu.setObjectName("datePickerMenu")
        menu.setAttribute(Qt.WA_DeleteOnClose)
        calendar = DatePickerCalendar(self._theme_colors(), menu)
        if self._minimum is not None:
            calendar.setMinimumDate(self._minimum)
        if self._maximum is not None:
            calendar.setMaximumDate(self._maximum)
        selected = self.date()
        if selected.isValid():
            calendar.setSelectedDate(max(calendar.minimumDate(),
                                         min(selected, calendar.maximumDate())))
        if self._range_partner is not None:
            first, last = ((self.date(), self._range_partner.date()) if self._starts_range
                           else (self._range_partner.date(), self.date()))
            calendar.setRange(first, last)
        action = QWidgetAction(menu)
        action.setDefaultWidget(calendar)
        menu.addAction(action)

        def choose(day):
            self.setDate(day)
            menu.close()

        calendar.clicked.connect(choose)
        calendar.activated.connect(choose)
        screen = self.calendar_button.screen().availableGeometry()
        anchor = self.calendar_button.mapToGlobal(self.calendar_button.rect().bottomLeft())
        size = menu.sizeHint()
        x = max(screen.left(), min(anchor.x(), screen.right() - size.width() + 1))
        y = anchor.y()
        if y + size.height() > screen.bottom() + 1:
            y = self.calendar_button.mapToGlobal(QPoint(0, 0)).y() - size.height()
        y = max(screen.top(), min(y, screen.bottom() - size.height() + 1))
        menu.popup(QPoint(x, y))
        calendar.setFocus()
