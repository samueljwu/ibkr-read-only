"""Calendar views of covered, completed account P&L observations."""

from calendar import monthrange
from datetime import date, timedelta
import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from app.views.calendar_ui import CalendarGrid, CalendarToolbar, DecimalPlacesControl


def _signed_money(value, places=2):
    return f"{'+' if value >= 0 else '-'}${abs(value):,.{places}f}"


def _pnl_percent(rows):
    """Compound covered daily returns; omit percentages with missing inputs."""
    returns = [row.get("daily_return") for row in rows]
    if not returns or any(value is None or not math.isfinite(value) for value in returns):
        return None
    return (math.prod(1 + value for value in returns) - 1) * 100


def _signed_percent(value, places=2):
    return f"{value:+,.{places}f}%" if value is not None else "—"


class PnlCalendar(QWidget):
    """Browse daily or aggregated P&L within the selected analytics range."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._first = self._last = self._anchor = date.today()
        self._rows = {}
        self._complete = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toolbar = CalendarToolbar(("Daily", "Weekly", "Monthly", "Quarterly", "Yearly"))
        self.interval_combo = self.toolbar.interval_combo
        self.interval_combo.currentIndexChanged.connect(self._render)
        self.previous_button = self.toolbar.previous_button
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.period_label = self.toolbar.period_label
        self.next_button = self.toolbar.next_button
        self.next_button.clicked.connect(lambda: self._change_page(1))
        self.usd_decimal_control = DecimalPlacesControl("USD")
        self.percent_decimal_control = DecimalPlacesControl("P&L %")
        self.usd_decimals = self.usd_decimal_control
        self.percent_decimals = self.percent_decimal_control
        self.toolbar.add_control(self.usd_decimal_control)
        self.toolbar.add_control(self.percent_decimal_control)
        layout.addWidget(self.toolbar)
        self.usd_decimals.valueChanged.connect(self._render)
        self.percent_decimals.valueChanged.connect(self._render)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("hint")
        self.summary_label.setAlignment(Qt.AlignCenter)
        self.summary_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.toolbar.add_navigation_detail(self.summary_label)
        self.note_label = QLabel()
        self.note_label.setObjectName("hint")
        self.note_label.setWordWrap(True)
        self.note_label.hide()
        layout.addWidget(self.note_label)

        self.grids = QStackedWidget()
        self.daily_grid = CalendarGrid(30, 5, ("Mon", "Tue", "Wed", "Thu", "Fri"))
        self.day_cells = self.daily_grid.cells
        self.grids.addWidget(self.daily_grid)
        self.period_grid = CalendarGrid(12, 3)
        self.period_cells = self.period_grid.cells
        self.grids.addWidget(self.period_grid)
        layout.addWidget(self.grids, 1)

    def set_data(self, rows, first, last, complete):
        self._rows = {row["day"]: row for row in rows}
        self._first, self._last = first, last
        self._complete = complete
        if not first <= self._anchor <= last:
            self._anchor = last
        self._render()

    def set_note(self, message):
        self.note_label.setText(message)
        self.note_label.setVisible(bool(message))

    def _money(self, value):
        return _signed_money(value, self.usd_decimals.value())

    def _percent(self, value):
        return _signed_percent(value, self.percent_decimals.value())

    def _page_start(self, day):
        interval = self.interval_combo.currentText()
        if interval == "Daily":
            return day.replace(day=1)
        if interval == "Weekly":
            first_monday = self._first - timedelta(days=self._first.weekday())
            weeks = (day - first_monday).days // 7
            return first_monday + timedelta(weeks=weeks // 12 * 12)
        if interval == "Quarterly":
            start_year = self._first.year
            return date(start_year + (day.year - start_year) // 2 * 2, 1, 1)
        if interval == "Yearly":
            start_year = self._first.year
            return date(start_year + (day.year - start_year) // 12 * 12, 1, 1)
        return date(day.year, 1, 1)

    def _change_page(self, direction):
        interval = self.interval_combo.currentText()
        page = self._page_start(self._anchor)
        if interval == "Daily":
            index = page.year * 12 + page.month - 1 + direction
            self._anchor = date(index // 12, index % 12 + 1, 1)
        elif interval == "Weekly":
            self._anchor = page + timedelta(weeks=12 * direction)
        else:
            years = 12 if interval == "Yearly" else 2 if interval == "Quarterly" else 1
            self._anchor = date(page.year + direction * years, 1, 1)
        self._render()

    def _covered_rows(self, first, last):
        if not self._complete:
            return []
        return [row for day, row in self._rows.items()
                if first.isoformat() <= day <= last.isoformat() and
                row["source"] != "live" and row["daily_pnl"] is not None]

    def _set_summary(self, rows):
        percent = _pnl_percent(rows)
        self.summary_label.setText(
            f"Selected range · {len(rows)} covered days · Net "
            f"{self._money(math.fsum(row['daily_pnl'] for row in rows))}"
            f" · P&L %: {self._percent(percent)}"
            if rows else "No covered daily P&L in this period")

    def _render(self, *_):
        if not hasattr(self, "grids"):
            return
        interval = self.interval_combo.currentText()
        page = self._page_start(self._anchor)
        self.toolbar.set_period("", page > self._page_start(self._first),
                                page < self._page_start(self._last))
        self.grids.setCurrentIndex(0 if interval == "Daily" else 1)
        if interval == "Daily":
            self._render_days(page)
        else:
            self._render_periods(page, interval)

    def _render_days(self, month):
        self.period_label.setText(month.strftime("%B %Y"))
        days = monthrange(month.year, month.month)[1]
        self._set_summary(self._covered_rows(month, date(month.year, month.month, days)))
        first_monday = month - timedelta(days=month.weekday())
        for index, cell in enumerate(self.day_cells):
            current = first_monday + timedelta(days=(index // 5) * 7 + index % 5)
            if current.month != month.month:
                cell.set_empty()
                continue
            number = current.day
            in_range = self._first <= current <= self._last
            row = self._rows.get(current.isoformat()) if in_range else None
            amount = (row["daily_pnl"] if row and self._complete and
                      row["source"] != "live" else None)
            percent = _pnl_percent([row]) if amount is not None else None
            text = f"{number}" + (f"\n{self._money(amount)}\n{self._percent(percent)}"
                                    if amount is not None else
                                    "\nProvisional" if row and row["source"] == "live" else "")
            detail = (f"{current:%d %b %Y}\nDaily adjusted P&L: {self._money(amount)}"
                      + f"\nP&L %: {self._percent(percent)}"
                      if amount is not None else
                      f"{current:%d %b %Y}\nDaily P&L unavailable" if row else
                      f"{current:%d %b %Y}")
            cell.set_content(text, detail, amount,
                             active=in_range and row is not None)

    def _render_periods(self, page, interval):
        count = 8 if interval == "Quarterly" else 12
        columns = 2 if interval == "Quarterly" else 3
        self.period_grid.arrange(count, columns)
        self.period_label.setText(
            f"{page:%d %b %Y} – {page + timedelta(weeks=12, days=-1):%d %b %Y}"
            if interval == "Weekly" else
            f"{page.year}–{page.year + 1}" if interval == "Quarterly" else
            f"{page.year}–{page.year + 11}" if interval == "Yearly" else str(page.year))
        for index, cell in enumerate(self.period_cells):
            if index >= count:
                continue
            if interval == "Weekly":
                first = page + timedelta(weeks=index)
                last = first + timedelta(days=6)
                title = f"{first:%d %b} – {last:%d %b}"
            elif interval == "Monthly":
                first = date(page.year, index + 1, 1)
                last = date(page.year, index + 1, monthrange(page.year, index + 1)[1])
                title = first.strftime("%B")
            elif interval == "Quarterly":
                year = page.year + index // 4
                quarter = index % 4 + 1
                first = date(year, (quarter - 1) * 3 + 1, 1)
                end_month = quarter * 3
                last = date(year, end_month, monthrange(year, end_month)[1])
                title = f"Q{quarter} {year} · {first:%b}–{last:%b}"
            else:
                first, last = date(page.year + index, 1, 1), date(page.year + index, 12, 31)
                title = str(first.year)
            covered = self._covered_rows(first, last)
            amount = math.fsum(row["daily_pnl"] for row in covered) if covered else None
            percent = _pnl_percent(covered)
            overlaps = first <= self._last and last >= self._first
            text = title + (f"\n{self._money(amount)}\n{self._percent(percent)}"
                            if amount is not None else
                            "\n—" if overlaps else "")
            detail = (f"{first:%d %b %Y} to {last:%d %b %Y}\n"
                      f"{len(covered)} covered days in selected range" +
                      (f"\nNet adjusted P&L: {self._money(amount)}"
                       f"\nCompounded P&L %: {self._percent(percent)}"
                       if amount is not None else ""))
            cell.set_content(text, detail, amount, active=overlaps)
        page_last = (page + timedelta(weeks=12, days=-1) if interval == "Weekly" else
                     date(page.year + 1, 12, 31) if interval == "Quarterly" else
                     date(page.year + 11, 12, 31) if interval == "Yearly" else
                     date(page.year, 12, 31))
        self._set_summary(self._covered_rows(page, page_last))
