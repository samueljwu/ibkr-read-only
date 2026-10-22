"""Two focused account analytics views with a shared, draggable date range."""

import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date, timedelta

from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QLineSeries, QValueAxis
from PySide6.QtCore import QByteArray, QDate, QDateTime, QLocale, QMargins, QPointF, QRect, QRectF, Qt, QTime, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDateEdit, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QRubberBand, QScrollArea, QSizePolicy,
                               QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from app.finance.analytics import (capital_flow_rows, cash_share_rows, covered_daily_pnl_rows,
                                   daily_pnl_summary,
                                   flex_monthly_history, performance_window_rows)
from app.views import widgets
from app.views.buy_sell_levels_ui import BuySellLevelsPanel
from app.views.chart_ui import nice_scale as _nice_scale
from app.views.exposure_ui import ExposurePanel
from app.views.pnl_calendar import PnlCalendar
from app.views.returns_ui import ReturnPanel
from app.views.table_ui import ReportTable, SortItem
from app.views.themed_checkbox import ThemedCheckBox
from app.views.ui_theme import (COLORS, TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING,
                          style_toolbar_dropdown)


def _timestamp(day):
    return QDateTime(QDate.fromString(day, "yyyy-MM-dd"), QTime(0, 0)).toMSecsSinceEpoch()


def _capital_activity_buckets(rows, first, last):
    """Group trade-day values for a readable number of activity bars."""
    span = (last - first).days + 1
    period = ("day" if span <= 31 else "week" if span <= 180 else
              "month" if span <= 730 else "quarter")
    totals = defaultdict(lambda: [0.0, 0.0])
    for row in rows:
        day = date.fromisoformat(row["day"])
        if period == "week":
            start = day - timedelta(days=day.weekday())
        elif period == "month":
            start = day.replace(day=1)
        elif period == "quarter":
            start = day.replace(month=1 + 3 * ((day.month - 1) // 3), day=1)
        else:
            start = day
        totals[start][0] += row["bought"]
        totals[start][1] += row["sold"]
    buckets = []
    for start, (bought, sold) in sorted(totals.items()):
        if period == "week":
            end = start + timedelta(days=6)
        elif period in ("month", "quarter"):
            months = 1 if period == "month" else 3
            next_month = QDate(start.year, start.month, 1).addMonths(months).toPython()
            end = next_month - timedelta(days=1)
        else:
            end = start
        shown_start, shown_end = max(start, first), min(end, last)
        midpoint = shown_start + timedelta(days=(shown_end - shown_start).days // 2)
        buckets.append((shown_start, shown_end, midpoint, bought, sold))
    return period, buckets


def _benchmark_window(prices, first, last):
    """Price change from the most recent S&P close before the selected range."""
    baseline = None
    points = []
    for row in prices:
        day = date.fromisoformat(row["day"])
        close = row["close"]
        if day < first:
            baseline = (day, close)
        elif day <= last and baseline is not None:
            points.append((day, close, (close / baseline[1] - 1) * 100))
    return baseline, points


def _rebased_benchmark_window(prices, first, last):
    """Index percentage change from its first close inside the selected range."""
    visible = [(date.fromisoformat(row["day"]), row["close"]) for row in prices
               if first.isoformat() <= row["day"] <= last.isoformat()]
    if not visible:
        return None, []
    baseline = visible[0]
    return baseline, [(day, close, (close / baseline[1] - 1) * 100)
                      for day, close in visible]


def _rebased_pnl_points(rows, field):
    """Account metric as percent change from its first usable observation."""
    usable = [row for row in rows if row.get(field) is not None and row.get("nav") is not None]
    if not usable or usable[0]["nav"] <= 0:
        return []
    initial = usable[0][field]
    start_nav = usable[0]["nav"]
    if field == "performance":
        growth = 1 + initial / 100
        if growth <= 0:
            return []
        value = lambda current: ((1 + current / 100) / growth - 1) * 100
    else:
        value = lambda current: (current - initial) / start_nav * 100
    return [(_timestamp(row["day"]), value(row[field])) for row in usable]


class RangeChartView(QChartView):
    selectionRequested = Signal(object, object)
    panRequested = Signal(int)

    def __init__(self):
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._start = None
        self._panning = False
        self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._hover_primary = []
        self._hover_benchmark = []
        self._hover_primary_stamps = []
        self._hover_benchmark_stamps = []
        self._hover_primary_mode = "nearest"
        self._hover_line = QWidget(self.viewport())
        self._hover_line.setObjectName("chartHoverLine")
        self._hover_line.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hover_line.hide()
        self._hover_label = QLabel(self.viewport())
        self._hover_label.setObjectName("chartHoverCard")
        self._hover_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hover_label.setTextFormat(Qt.PlainText)
        self._hover_label.hide()

    def set_hover_data(self, primary, benchmark=(), theme="light", primary_mode="nearest"):
        self._hover_primary = sorted(primary)
        self._hover_benchmark = sorted(benchmark)
        self._hover_primary_stamps = [stamp for stamp, _ in self._hover_primary]
        self._hover_benchmark_stamps = [stamp for stamp, _ in self._hover_benchmark]
        self._hover_primary_mode = primary_mode
        self._hide_hover()

    def _hide_hover(self):
        self._hover_label.hide()
        self._hover_line.hide()

    @staticmethod
    def _nearest(rows, stamps, stamp, *, previous=False):
        if not rows:
            return None
        index = min(bisect_left(stamps, stamp), len(rows) - 1)
        if index and (previous and stamps[index] > stamp or
                      not previous and abs(stamps[index - 1] - stamp) < abs(stamps[index] - stamp)):
            index -= 1
        return rows[index][1]

    def _show_hover(self, position):
        area = self.chart().plotArea()
        axes = self.chart().axes(Qt.Horizontal)
        if (not axes or area.width() < 1 or not area.contains(position) or
                not (self._hover_primary or self._hover_benchmark)):
            self._hide_hover()
            return
        axis = axes[0]
        fraction = (position.x() - area.left()) / area.width()
        first = axis.min().toMSecsSinceEpoch()
        stamp = first + round(fraction * (axis.max().toMSecsSinceEpoch() - first))
        details = [item for item in (
            self._nearest(self._hover_primary, self._hover_primary_stamps, stamp,
                          previous=self._hover_primary_mode == "previous"),
            self._nearest(self._hover_benchmark, self._hover_benchmark_stamps, stamp))
            if item]
        self._hover_label.setText("\n\n".join(details))
        self._hover_label.adjustSize()
        point = position.toPoint()
        width, height = self._hover_label.width(), self._hover_label.height()
        x = point.x() + 16
        if x + width > self.viewport().width() - 8:
            x = point.x() - width - 16
        y = point.y() + 16
        if y + height > self.viewport().height() - 8:
            y = point.y() - height - 16
        self._hover_label.move(max(8, x), max(8, y))
        self._hover_line.setGeometry(round(position.x()), round(area.top()),
                                     1, round(area.height()))
        self._hover_line.show()
        self._hover_label.show()
        self._hover_label.raise_()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.chart().plotArea().contains(event.position()):
            self._hide_hover()
            self._start = event.position().toPoint()
            self._panning = bool(event.modifiers() & Qt.ShiftModifier)
            if not self._panning:
                self._band.setGeometry(QRect(self._start, self._start))
                self._band.show()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._start is not None:
            self._hide_hover()
            if not self._panning:
                area = self.chart().plotArea()
                end = event.position().toPoint()
                end.setX(max(int(area.left()), min(int(area.right()), end.x())))
                self._band.setGeometry(QRect(self._start, end).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)
        self._show_hover(event.position())

    def leaveEvent(self, event):
        self._hide_hover()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._start is None or event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._band.hide()
        start = self._start
        self._start = None
        area = self.chart().plotArea()
        if area.width() < 1 or abs(event.position().x() - start.x()) < 8:
            event.accept()
            return
        axes = self.chart().axes(Qt.Horizontal)
        if not axes:
            event.accept()
            return
        axis = axes[0]
        span = axis.max().toMSecsSinceEpoch() - axis.min().toMSecsSinceEpoch()
        if self._panning:
            days = round((start.x() - event.position().x()) * span / area.width() / 86400000)
            if days:
                self.panRequested.emit(days)
        else:
            def selected_day(x):
                ratio = (max(area.left(), min(area.right(), x)) - area.left()) / area.width()
                stamp = axis.min().toMSecsSinceEpoch() + round(ratio * span)
                return QDateTime.fromMSecsSinceEpoch(stamp).date()

            first, last = sorted((selected_day(start.x()), selected_day(event.position().x())))
            self.selectionRequested.emit(first, last)
        event.accept()


class TimeRangeNavigator(QWidget):
    """Overview timeline with a movable selection and draggable end handles."""

    selectionRequested = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(85)
        self.setMinimumWidth(0)
        self.setMouseTracking(True)
        self.setToolTip("Drag the middle to move the range, or drag either end to resize it")
        self.palette_name = "light"
        self.first = self.last = date.today()
        self.start = self.end = date.today()
        self._drag = None
        self._press_x = 0
        self._original = (self.start, self.end)

    def set_range(self, first, last, start, end, palette_name):
        self.first, self.last = first, max(first, last)
        self.start = max(self.first, min(start, self.last))
        self.end = max(self.start, min(end, self.last))
        self.palette_name = palette_name
        self._update_tooltip()
        self.update()

    def _update_tooltip(self):
        self.setToolTip(f"{self._selection_label()}\n"
                        "Drag the middle to move the range, or either end to resize it")

    def _selection_label(self):
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"

    def _x(self, day):
        span = max(1, (self.last - self.first).days)
        return 14 + (self.width() - 28) * (day - self.first).days / span

    def _day(self, x):
        span = max(1, (self.last - self.first).days)
        ratio = max(0, min(1, (x - 14) / max(1, self.width() - 28)))
        return self.first + timedelta(days=round(ratio * span))

    def _bar(self):
        left, right = self._x(self.start), self._x(self.end)
        if right - left < 36:
            center = (left + right) / 2
            left = max(14, min(center - 18, self.width() - 50))
            right = left + 36
        return left, right

    def paintEvent(self, _event):
        colors = COLORS[self.palette_name]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        left, right = 14, max(15, self.width() - 14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors["line"]))
        painter.drawRoundedRect(QRectF(left, 38, right - left, 8), 4, 4)
        selected = QColor(colors["accent"])
        selected.setAlpha(120)
        painter.setBrush(selected)
        painter.drawRoundedRect(QRectF(self._x(self.start), 38,
                                       max(3, self._x(self.end) - self._x(self.start)), 8), 4, 4)
        bar_left, bar_right = self._bar()
        painter.setBrush(QColor(colors["accent_soft"]))
        painter.setPen(QPen(QColor(colors["accent"]), 1))
        painter.drawRoundedRect(QRectF(bar_left, 7, bar_right - bar_left, 22), 5, 5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors["accent"]))
        painter.drawRoundedRect(QRectF(bar_left, 7, 9, 22), 4, 4)
        painter.drawRoundedRect(QRectF(bar_right - 9, 7, 9, 22), 4, 4)
        painter.setPen(QColor(colors["muted"]))
        painter.drawText(QRectF(left, 49, (right - left) / 2, 16),
                         Qt.AlignLeft | Qt.AlignVCenter, self.first.strftime("%b %Y"))
        painter.drawText(QRectF(left + (right - left) / 2, 49, (right - left) / 2, 16),
                         Qt.AlignRight | Qt.AlignVCenter, self.last.strftime("%b %Y"))
        painter.setPen(QColor(colors["text"]))
        painter.drawText(QRectF(left, 66, right - left, 17),
                         Qt.AlignCenter | Qt.AlignVCenter, self._selection_label())

    def _handle(self, x, y):
        left, right = self._bar()
        if 4 <= y <= 32 and 0 <= x <= self.width():
            if abs(x - left) <= 12:
                return "left"
            if abs(x - right) <= 12:
                return "right"
            if left < x < right:
                return "move"
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        self._drag = self._handle(event.position().x(), event.position().y())
        if self._drag is None:
            # Clicking the lower overview track re-centres the current window.
            target = self._day(event.position().x())
            span = (self.end - self.start).days
            start = max(self.first, min(target - timedelta(days=span // 2),
                                        self.last - timedelta(days=span)))
            self.start, self.end = start, start + timedelta(days=span)
            self._update_tooltip()
            self.selectionRequested.emit(QDate(self.start.year, self.start.month, self.start.day),
                                         QDate(self.end.year, self.end.month, self.end.day))
            self.update()
            return
        self._press_x = event.position().x()
        self._original = (self.start, self.end)
        self.setCursor(Qt.ClosedHandCursor if self._drag == "move" else Qt.SizeHorCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            mode = self._handle(event.position().x(), event.position().y())
            self.setCursor(Qt.SizeHorCursor if mode in ("left", "right") else
                           Qt.OpenHandCursor if mode == "move" else Qt.ArrowCursor)
            return
        day_delta = round((event.position().x() - self._press_x) *
                          max(1, (self.last - self.first).days) / max(1, self.width() - 28))
        first, last = self._original
        if self._drag == "left":
            self.start = max(self.first, min(first + timedelta(days=day_delta), self.end))
        elif self._drag == "right":
            self.end = min(self.last, max(self.start, last + timedelta(days=day_delta)))
        else:
            day_delta = max((self.first - first).days,
                            min(day_delta, (self.last - last).days))
            self.start, self.end = (first + timedelta(days=day_delta),
                                    last + timedelta(days=day_delta))
        self._update_tooltip()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is None:
            return super().mouseReleaseEvent(event)
        self._drag = None
        self.unsetCursor()
        if (self.start, self.end) != self._original:
            self.selectionRequested.emit(QDate(self.start.year, self.start.month, self.start.day),
                                         QDate(self.end.year, self.end.month, self.end.day))
        event.accept()


class AnalyticsTab(QWidget):
    SECTION_IDS = ("pnl", "return", "capital", "exposure", "monthly", "levels")
    METRICS = (
        ("NAV", "nav", "USD"),
        ("NAV change", "nav_change", "USD"),
        ("Adjusted NAV change", "adjusted_change", "USD"),
        ("Adjusted performance", "performance", "%"),
    )
    DAILY_EXTREMES = (("Max USD gain", "max_usd_gain"),
                      ("Max USD loss", "max_usd_loss"),
                      ("Max % gain", "max_pct_gain"),
                      ("Max % loss", "max_pct_loss"))

    def __init__(self, db, service, portfolio_id, parent=None, request_price_history=None):
        super().__init__(parent)
        self.setObjectName("analyticsPage")
        self.db, self.service, self.portfolio_id = db, service, portfolio_id
        self.palette_name = "light"
        self._updating_range = False
        self._last_preset = "3 months"
        self._earliest_day = date.today()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        self.controls_panel = QWidget()
        controls = QVBoxLayout(self.controls_panel)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        main_controls = QHBoxLayout()
        main_controls.setSpacing(TOOLBAR_GROUP_SPACING)
        detail_controls = QHBoxLayout()
        detail_controls.setSpacing(TOOLBAR_GROUP_SPACING)
        chart_controls = QHBoxLayout()
        chart_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        self.chart_label = QLabel("Chart")
        chart_controls.addWidget(self.chart_label)
        self.metric_combo = QComboBox()
        style_toolbar_dropdown(self.metric_combo)
        self.metric_combo.addItems([metric[0] for metric in self.METRICS])
        self.metric_combo.setCurrentText("Adjusted NAV change")
        self.metric_combo.setFixedWidth(280)
        self.metric_combo.currentIndexChanged.connect(self._render_pnl_chart)
        chart_controls.addWidget(self.metric_combo)
        main_controls.addLayout(chart_controls)
        self.capital_combo = QComboBox()
        style_toolbar_dropdown(self.capital_combo)
        self.capital_combo.addItems(("Net deployed", "Buy / sell activity", "Cash / portfolio"))
        self.capital_combo.setFixedWidth(280)
        self.capital_combo.currentIndexChanged.connect(self._capital_chart_changed)
        main_controls.addWidget(self.capital_combo)
        self.benchmark_toggle = ThemedCheckBox("S&&P 500")
        self.benchmark_toggle.setAccessibleName("S&P 500")
        self.benchmark_toggle.setChecked(True)
        self.benchmark_toggle.setToolTip("Show S&P 500 price change on the right percent axis")
        self.benchmark_toggle.toggled.connect(self._benchmark_toggled)
        main_controls.addWidget(self.benchmark_toggle)
        range_controls = QHBoxLayout()
        range_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        self.range_label = QLabel("Range")
        range_controls.addWidget(self.range_label)
        self.range_combo = QComboBox()
        style_toolbar_dropdown(self.range_combo)
        self.range_combo.addItems(("5 days", "1 month", "3 months", "6 months",
                                   "YTD", "1 year", "All", "Custom"))
        self.range_combo.setCurrentText("3 months")
        self.range_combo.setFixedWidth(140)
        range_controls.addWidget(self.range_combo)
        main_controls.addLayout(range_controls)
        main_controls.addStretch()
        controls.addLayout(main_controls)
        self.date_summary = QLabel()
        self.date_summary.setObjectName("hint")
        detail_controls.addWidget(self.date_summary)
        start_controls = QHBoxLayout()
        start_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        self.start_label = QLabel("From")
        start_controls.addWidget(self.start_label)
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-3))
        start_controls.addWidget(self.start_date)
        detail_controls.addLayout(start_controls)
        end_controls = QHBoxLayout()
        end_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        self.end_label = QLabel("To")
        end_controls.addWidget(self.end_label)
        self.end_date = QDateEdit(QDate.currentDate())
        end_controls.addWidget(self.end_date)
        detail_controls.addLayout(end_controls)
        for editor in (self.start_date, self.end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setFixedWidth(170)
            editor.dateChanged.connect(self._manual_range_changed)
        self.reset_button = QPushButton("Reset")
        self.reset_button.setObjectName("secondary")
        self.reset_button.clicked.connect(lambda: self.range_combo.setCurrentText(self._last_preset))
        detail_controls.addWidget(self.reset_button)
        detail_controls.addStretch()
        controls.addLayout(detail_controls)

        self.sections = QTabWidget()
        self.sections.tabBar().setExpanding(False)
        self.sections.setObjectName("analyticsSections")
        self.sections.setMinimumWidth(0)
        self.sections.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.pnl_page = QWidget()
        self.return_page = ReturnPanel(db, service, portfolio_id)
        self.capital_page = QWidget()
        self.levels_page = BuySellLevelsPanel(
            db, portfolio_id, request_price_history=request_price_history,
            portfolio_rows_provider=service.portfolio_rows,
            trade_rows_provider=getattr(service, "trade_rows", None))
        self.exposure_page = ExposurePanel(db, service, portfolio_id)
        self.monthly_page = QWidget()
        for page in (self.pnl_page, self.return_page, self.capital_page, self.levels_page,
                     self.exposure_page, self.monthly_page):
            page.setObjectName("analyticsSection")
            page.setMinimumWidth(0)
            page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.sections.addTab(self.pnl_page, "P&&L")
        self.sections.addTab(self.return_page, "Returns")
        self.sections.addTab(self.capital_page, "Capital deployment")
        self.sections.addTab(self.exposure_page, "Exposure")
        self.sections.addTab(self.monthly_page, "Monthly P&&L")
        self.sections.addTab(self.levels_page, "Buy / sell levels")
        widgets.persist_tab_order(self.sections, db, f"analytics_section_layout_{portfolio_id}",
                                  ids=list(self.SECTION_IDS))
        scroller = QScrollArea()
        scroller.setObjectName("analyticsScroller")
        scroller.setFrameShape(QScrollArea.NoFrame)
        scroller.setWidgetResizable(True)
        scroller.setMinimumWidth(0)
        scroller.setMinimumHeight(0)
        scroller.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        scroller.setWidget(self.sections)
        outer.addWidget(scroller, 1)
        self._build_pnl()
        self._build_capital()
        self._build_monthly()
        self.sections.currentChanged.connect(self._update_section_controls)
        self._update_section_controls()
        self.range_combo.currentTextChanged.connect(self._preset_changed)
        self._preset_changed("3 months")
        self._initialized = True

    def _metric_summary(self, layout, labels, *, show_close=False):
        summary = QWidget()
        summary.setObjectName("analyticsSummary")
        row = QHBoxLayout(summary)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(12)
        values = {}
        for label in labels:
            block = QVBoxLayout()
            block.setSpacing(2)
            caption = QLabel(label)
            caption.setObjectName("metricLabel")
            value = QLabel("—")
            value.setObjectName("metricValue")
            block.addWidget(caption)
            block.addWidget(value)
            if show_close and label == "NAV":
                self.as_of_label = QLabel()
                self.as_of_label.setObjectName("hint")
                block.addWidget(self.as_of_label)
                self.sync_status = QLabel()
                self.sync_status.setObjectName("hint")
                self.sync_status.setWordWrap(True)
                block.addWidget(self.sync_status)
            block.addStretch()
            row.addLayout(block, 1)
            values[label] = value
        if show_close:
            extreme = QVBoxLayout()
            extreme.setSpacing(2)
            self.max_daily_combo = QComboBox()
            style_toolbar_dropdown(self.max_daily_combo)
            self.max_daily_combo.addItems([label for label, _ in self.DAILY_EXTREMES])
            self.max_daily_combo.setMinimumWidth(180)
            self.max_daily_combo.setToolTip("Largest completed daily flow-adjusted gain or loss in the selected range")
            self.max_daily_combo.currentIndexChanged.connect(self._update_daily_summary)
            self.max_daily_value = QLabel("—")
            self.max_daily_value.setObjectName("metricValue")
            self.max_daily_detail = QLabel()
            self.max_daily_detail.setObjectName("hint")
            extreme.addWidget(self.max_daily_combo)
            extreme.addWidget(self.max_daily_value)
            extreme.addWidget(self.max_daily_detail)
            extreme.addStretch()
            row.addLayout(extreme, 1)
            sharpe = QVBoxLayout()
            sharpe.setSpacing(2)
            sharpe_caption = QLabel("Sharpe ratio")
            sharpe_caption.setObjectName("metricLabel")
            sharpe_caption.setToolTip("Annualized from completed daily flow-adjusted returns, using zero risk-free return")
            self.sharpe_value = QLabel("—")
            self.sharpe_value.setObjectName("metricValue")
            self.sharpe_detail = QLabel()
            self.sharpe_detail.setObjectName("hint")
            sharpe.addWidget(sharpe_caption)
            sharpe.addWidget(self.sharpe_value)
            sharpe.addWidget(self.sharpe_detail)
            sharpe.addStretch()
            row.addLayout(sharpe, 1)
        layout.addWidget(summary)
        return summary, values

    def _update_section_controls(self, *_):
        pnl_visible = self.sections.currentWidget() is self.pnl_page
        capital_visible = self.sections.currentWidget() is self.capital_page
        pnl_chart_visible = pnl_visible and self.pnl_view_combo.currentIndex() == 0
        show_controls = pnl_chart_visible or capital_visible
        if show_controls:
            page = self.pnl_page if pnl_visible else self.capital_page
            if self.controls_panel.parentWidget() is not page:
                previous = self.controls_panel.parentWidget()
                if previous is not None:
                    previous.layout().removeWidget(self.controls_panel)
                page.layout().insertWidget(0, self.controls_panel)
            self.controls_panel.show()
        else:
            self.controls_panel.hide()
        self.metric_combo.setVisible(pnl_chart_visible)
        self.capital_combo.setVisible(capital_visible)
        self.benchmark_toggle.setVisible(pnl_chart_visible or capital_visible)
        self.chart_label.setVisible(pnl_chart_visible or capital_visible)
        self.range_label.setVisible(show_controls)
        self.range_combo.setVisible(show_controls)
        self.pnl_summary_panel.setVisible(pnl_chart_visible)
        if show_controls:
            self._update_range_controls()
        else:
            for widget in (self.start_label, self.start_date, self.end_label, self.end_date,
                           self.reset_button, self.date_summary):
                widget.hide()
        if getattr(self, "_initialized", False):
            self.refresh()

    def _pnl_view_changed(self, index):
        self.pnl_views.setCurrentIndex(index)
        self.pnl_calendar.toolbar.setVisible(index == 1)
        if self.sections.currentWidget() is self.pnl_page:
            self._update_section_controls()

    def _update_range_controls(self):
        if self.sections.currentWidget() in (self.exposure_page, self.return_page):
            return
        if (self.sections.currentWidget() is self.pnl_page and
                self.pnl_view_combo.currentIndex() == 1):
            for widget in (self.start_label, self.start_date, self.end_label, self.end_date,
                           self.reset_button, self.date_summary):
                widget.hide()
            return
        custom = self.range_combo.currentText() == "Custom"
        for widget in (self.start_label, self.start_date, self.end_label, self.end_date,
                       self.reset_button):
            widget.setVisible(custom)
        self.date_summary.setVisible(not custom)

    def _table(self, headers, setting_key, formats):
        table = ReportTable(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.configure_numbers(self.db, f"{setting_key}_{self.portfolio_id}", formats)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setDefaultSectionSize(145)
        table.setColumnWidth(0, 110)
        table.setMinimumHeight(160)
        table.setMinimumWidth(0)
        table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        table.setSortingEnabled(True)
        table.sortItems(0, Qt.DescendingOrder)
        return table

    def _split_view(self, layout, chart, table, navigator, setting_key, info_label):
        splitter, layout_combo = widgets.chart_table_layout(
            layout, self.db, setting_key, chart, table,
            info_label=info_label, navigator=navigator)
        chart.selectionRequested.connect(self._select_chart_range)
        chart.panRequested.connect(self._pan_chart_range)
        navigator.selectionRequested.connect(self._select_chart_range)
        return splitter, layout_combo

    @staticmethod
    def _table_view_state(table):
        header = table.horizontalHeader()
        return {
            "header": bytes(header.saveState().toBase64()).decode("ascii"),
            "sort_column": header.sortIndicatorSection(),
            "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
            "decimals": {str(column): table.decimals_for(column)
                         for column in table._number_formats},
        }

    @staticmethod
    def _apply_table_view_state(table, state):
        if not isinstance(state, dict):
            return
        header_state = state.get("header")
        if isinstance(header_state, str):
            header = QByteArray.fromBase64(header_state.encode("ascii", "ignore"))
            table.horizontalHeader().restoreState(header)
        decimals = state.get("decimals")
        if isinstance(decimals, dict):
            for column in table._number_formats:
                places = decimals.get(str(column))
                if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 30:
                    table.set_column_decimals(column, places)
        column = state.get("sort_column")
        if (isinstance(column, int) and not isinstance(column, bool) and
                0 <= column < table.columnCount()):
            table.sortItems(column, Qt.DescendingOrder if state.get("sort_order") == "desc"
                            else Qt.AscendingOrder)

    def view_state(self):
        def pane(splitter, table):
            return {
                "orientation": "horizontal" if splitter.orientation() == Qt.Horizontal else "vertical",
                "splitter": bytes(splitter.saveState().toBase64()).decode("ascii"),
                "table": self._table_view_state(table),
            }

        return {
            "version": 2,
            "section": self.sections.currentIndex(),
            "range": self.range_combo.currentText(),
            "start": self.start_date.date().toString("yyyy-MM-dd"),
            "end": self.end_date.date().toString("yyyy-MM-dd"),
            "metric": self.metric_combo.currentText(),
            "daily_extreme": self.max_daily_combo.currentText(),
            "pnl_view": self.pnl_view_combo.currentText(),
            "pnl_interval": self.pnl_calendar.interval_combo.currentText(),
            "pnl_decimals": {"usd": self.pnl_calendar.usd_decimals.value(),
                             "percent": self.pnl_calendar.percent_decimals.value()},
            "capital_chart": self.capital_combo.currentText(),
            "benchmark": self.benchmark_toggle.isChecked(),
            "pnl": pane(self.pnl_splitter, self.pnl_table),
            "capital": {**pane(self.capital_splitter, self.capital_table),
                        "cash_table": self._table_view_state(self.cash_share_table)},
            "exposure": self.exposure_page.view_state(),
            "return": self.return_page.view_state(),
            "levels": self.levels_page.view_state(),
        }

    def apply_view_state(self, state):
        if not isinstance(state, dict) or state.get("version") not in (1, 2):
            return False
        for combo, value in ((self.metric_combo, state.get("metric")),
                             (self.max_daily_combo, state.get("daily_extreme")),
                             (self.pnl_view_combo, state.get("pnl_view")),
                             (self.pnl_calendar.interval_combo, state.get("pnl_interval")),
                             (self.capital_combo, state.get("capital_chart")),
                             (self.range_combo, state.get("range"))):
            if isinstance(value, str) and combo.findText(value) >= 0:
                combo.setCurrentText(value)
        if isinstance(state.get("benchmark"), bool):
            self.benchmark_toggle.setChecked(state["benchmark"])
        precision = state.get("pnl_decimals")
        if isinstance(precision, dict):
            for name, control in (("usd", self.pnl_calendar.usd_decimals),
                                  ("percent", self.pnl_calendar.percent_decimals)):
                places = precision.get(name)
                if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 30:
                    control.setValue(places)
        try:
            first = date.fromisoformat(state["start"])
            last = date.fromisoformat(state["end"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            self._set_dates(first, last)
            self.refresh()
        for name, combo, splitter, table, setting_key in (
                ("pnl", self.pnl_layout_combo, self.pnl_splitter, self.pnl_table,
                 "analytics_pnl_splitter"),
                ("capital", self.capital_layout_combo, self.capital_splitter,
                 self.capital_table, "analytics_capital_splitter")):
            pane = state.get(name)
            if not isinstance(pane, dict):
                continue
            if pane.get("orientation") in ("horizontal", "vertical"):
                combo.setCurrentIndex(1 if pane["orientation"] == "horizontal" else 0)
            saved = pane.get("splitter")
            if isinstance(saved, str) and splitter.restoreState(
                    QByteArray.fromBase64(saved.encode("ascii", "ignore"))):
                widgets.save_chart_table_split_state(self.db, splitter, setting_key)
            self._apply_table_view_state(table, pane.get("table"))
            if name == "capital":
                self._apply_table_view_state(self.cash_share_table, pane.get("cash_table"))
        self.exposure_page.apply_view_state(state.get("exposure"))
        self.return_page.apply_view_state(state.get("return"))
        self.levels_page.apply_view_state(state.get("levels"))
        section = state.get("section")
        if isinstance(section, int) and not isinstance(section, bool) and 0 <= section < self.sections.count():
            if state["version"] == 1 and section >= 1:
                section += 1
            self.sections.setCurrentIndex(section)
        return True

    def _build_pnl(self):
        layout = QVBoxLayout(self.pnl_page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.controls_panel)
        self.pnl_summary_panel, self.stats = self._metric_summary(
            layout, [metric[0] for metric in self.METRICS], show_close=True)
        self.reported_performance_pnl = QLabel()
        self.reported_performance_pnl.setObjectName("hint")
        self.reported_performance_pnl.setWordWrap(True)
        self.reported_performance_pnl.hide()
        layout.addWidget(self.reported_performance_pnl)
        self._daily_summary = daily_pnl_summary([], False)
        self.stats["NAV"].setToolTip("Account NAV at the latest completed close in this range")
        self.stats["NAV change"].setToolTip(
            "Change in account NAV from the closing value before this range, including external flows")
        self.stats["Adjusted NAV change"].setToolTip(
            "NAV change after removing deposits, withdrawals and asset transfers")
        self.stats["Adjusted performance"].setToolTip(
            "Compounded daily account returns using IBKR's published TWR formula: "
            "ending NAV / (previous NAV + external flow) - 1")
        self.pnl_hint = QLabel("External flows include deposits, withdrawals and asset transfers.")
        self.pnl_hint.setObjectName("hint")
        self.pnl_hint.setWordWrap(True)
        self.pnl_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.pnl_hint.hide()
        self.pnl_chart = RangeChartView()
        self.pnl_navigator = TimeRangeNavigator()
        self.pnl_table = self._table(("Date", "NAV", "NAV change", "Adjusted NAV change",
                                      "Adjusted performance", "External flow",
                                      "Daily adjusted P&L"), "analytics_pnl_decimals",
                                     {1: (2, "usd"), 2: (2, "usd"), 3: (2, "usd"),
                                      4: (2, "percent"), 5: (2, "usd"), 6: (2, "usd")})
        self.pnl_calendar = PnlCalendar(inline_toolbar=False)
        view_controls = QHBoxLayout()
        view_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        view_controls.addWidget(QLabel("View"))
        self.pnl_view_combo = style_toolbar_dropdown(QComboBox())
        self.pnl_view_combo.addItems(("Chart and table", "Calendar"))
        view_controls.addWidget(self.pnl_view_combo)
        view_controls.addSpacing(TOOLBAR_GROUP_SPACING - TOOLBAR_ITEM_SPACING)
        view_controls.addWidget(self.pnl_calendar.toolbar)
        self.pnl_calendar.toolbar.hide()
        view_controls.addStretch()
        layout.addLayout(view_controls)
        self.pnl_views = QStackedWidget()
        self.pnl_views.setMinimumWidth(0)
        self.pnl_views.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(8)
        self.pnl_splitter, self.pnl_layout_combo = self._split_view(
            chart_layout, self.pnl_chart, self.pnl_table, self.pnl_navigator,
            "analytics_pnl_splitter", self.pnl_hint)
        for name, control in (("usd", self.pnl_calendar.usd_decimals),
                              ("percent", self.pnl_calendar.percent_decimals)):
            key = f"analytics_pnl_calendar_{name}_decimals_{self.portfolio_id}"
            try:
                saved = int(self.db.setting(key))
            except (TypeError, ValueError):
                saved = 2
            if 0 <= saved <= 30:
                control.setValue(saved)
            control.valueChanged.connect(
                lambda places, setting_key=key: self.db.set_setting(setting_key, str(places)))
        self.pnl_views.addWidget(chart_page)
        self.pnl_views.addWidget(self.pnl_calendar)
        layout.addWidget(self.pnl_views, 1)
        self.pnl_view_combo.currentIndexChanged.connect(self._pnl_view_changed)
        for column in (2, 5, 6):
            self.pnl_table.hideColumn(column)
        self.pnl_table.horizontalHeader().setToolTip(
            "Right-click a column header to show all columns or adjust decimals")
        self._pnl_rows = []
        self._flow_events = []

    def _build_capital(self):
        layout = QVBoxLayout(self.capital_page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        _, self.capital_stats = self._metric_summary(layout, ("Bought", "Sold", "Net deployed"))
        note = QLabel()
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.capital_note = note
        self.capital_note.hide()
        self.capital_stats["Net deployed"].setToolTip(
            "Executed security purchases minus sale proceeds, before fees. FX conversions are excluded.")
        self.capital_stats["Net deployed"].setProperty("analyticsSelected", True)
        self.capital_caption = QLabel()
        self.capital_caption.setObjectName("hint")
        self.capital_caption.setWordWrap(True)
        self.capital_caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.capital_chart = RangeChartView()
        self.capital_navigator = TimeRangeNavigator()
        self.capital_table = self._table(("Trade date", "Bought", "Sold", "Net",
                                          "Cumulative net"), "analytics_capital_decimals",
                                         {column: (2, "usd") for column in range(1, 5)})
        self.cash_share_table = self._table(
            ("Date", "Cash USD", "NAV USD", "Cash / NAV"),
            "analytics_cash_share_decimals",
            {1: (2, "usd"), 2: (2, "usd"), 3: (2, "percent")})
        self.capital_tables = QStackedWidget()
        self.capital_tables.setMinimumWidth(0)
        self.capital_tables.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.capital_tables.addWidget(self.capital_table)
        self.capital_tables.addWidget(self.cash_share_table)
        self.capital_splitter, self.capital_layout_combo = self._split_view(
            layout, self.capital_chart, self.capital_tables, self.capital_navigator,
            "analytics_capital_splitter", self.capital_caption)
        self._capital_rows = []
        self._cash_share_rows = []
        self._capital_valid = True

    def _build_monthly(self):
        layout = QVBoxLayout(self.monthly_page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)
        self.monthly_note = QLabel(
            "Older PortfolioAnalyst rows show reported returns. Flex rows use completed "
            "daily NAV and flows from the prior close through the date shown; a month "
            "may be partial. "
            "A missing baseline or gap leaves the calculated return blank.")
        self.monthly_note.setObjectName("hint")
        self.monthly_note.setWordWrap(True)
        layout.addWidget(self.monthly_note)
        self.reported_performance_monthly = QLabel()
        self.reported_performance_monthly.setObjectName("hint")
        self.reported_performance_monthly.setWordWrap(True)
        layout.addWidget(self.reported_performance_monthly)
        self.monthly_table = self._table(
            ("Month", "Period", "NAV USD", "Monthly return", "Source"),
            "analytics_monthly_decimals", {2: (2, "usd"), 3: (2, "percent")})
        layout.addWidget(self.monthly_table, 1)

    def _capital_chart_changed(self, *_):
        self.capital_tables.setCurrentWidget(
            self.cash_share_table if self.capital_combo.currentText() == "Cash / portfolio"
            else self.capital_table)
        self._render_capital_chart()

    def _preset_bounds(self, selection):
        today = date.today()
        if selection == "5 days":
            return today - timedelta(days=4), today
        if selection == "1 month":
            return QDate.currentDate().addMonths(-1).toPython(), today
        if selection == "3 months":
            return QDate.currentDate().addMonths(-3).toPython(), today
        if selection == "6 months":
            return QDate.currentDate().addMonths(-6).toPython(), today
        if selection == "YTD":
            return date(today.year, 1, 1), today
        if selection == "1 year":
            return QDate.currentDate().addYears(-1).toPython(), today
        return self._earliest_day, today

    def _set_dates(self, first, last):
        self._updating_range = True
        try:
            self.start_date.setDate(QDate(first.year, first.month, first.day))
            self.end_date.setDate(QDate(last.year, last.month, last.day))
        finally:
            self._updating_range = False

    def _preset_changed(self, selection):
        if selection != "Custom":
            self._last_preset = selection
            self._set_dates(*self._preset_bounds(selection))
        self._update_range_controls()
        self.refresh()

    def _manual_range_changed(self, *_):
        if self._updating_range:
            return
        if self.range_combo.currentText() != "Custom":
            self.range_combo.setCurrentText("Custom")
        else:
            self.refresh()

    def _select_chart_range(self, first, last):
        first, last = first.toPython(), last.toPython()
        first = max(self._earliest_day, first)
        last = min(date.today(), last)
        if first > last:
            return
        self._set_dates(first, last)
        if self.range_combo.currentText() == "Custom":
            self.refresh()
        else:
            self.range_combo.setCurrentText("Custom")

    def _pan_chart_range(self, days):
        first = self.start_date.date().toPython() + timedelta(days=days)
        last = self.end_date.date().toPython() + timedelta(days=days)
        if first < self._earliest_day:
            last += self._earliest_day - first
            first = self._earliest_day
        if last > date.today():
            first -= last - date.today()
            last = date.today()
        self._select_chart_range(QDate(first.year, first.month, first.day),
                                 QDate(last.year, last.month, last.day))

    @staticmethod
    def _money(value):
        return "—" if value is None else f"${value:,.2f}"

    @staticmethod
    def _percent(value):
        return "—" if value is None else f"{value:+,.2f}%"

    def _update_daily_summary(self, *_):
        if not hasattr(self, "_daily_summary"):
            return
        label = self.max_daily_combo.currentText()
        key = dict(self.DAILY_EXTREMES).get(label, "max_usd_gain")
        day = self._daily_summary[key]
        if day is None:
            self.max_daily_value.setText("—")
            self.max_daily_detail.setText("")
            trend = ""
        else:
            percent_first = key.startswith("max_pct")
            amount = day["daily_pnl"]
            percent = day["daily_return"] * 100
            money = f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"
            self.max_daily_value.setText(self._percent(percent) if percent_first else money)
            secondary = money if percent_first else self._percent(percent)
            self.max_daily_detail.setText(
                f"{secondary} · {date.fromisoformat(day['day']):%d %b %Y}")
            trend = "positive" if amount > 0 else "negative"
        self.max_daily_value.setProperty("pnlTrend", trend)
        self.max_daily_value.style().unpolish(self.max_daily_value)
        self.max_daily_value.style().polish(self.max_daily_value)
        sharpe = self._daily_summary["sharpe"]
        count = self._daily_summary["return_count"]
        self.sharpe_value.setText(f"{sharpe:.2f}" if sharpe is not None else "—")
        self.sharpe_detail.setText(f"Annualized · {count} daily returns" if count else "")

    @staticmethod
    def _fill_table(table, rows):
        sorting = table.isSortingEnabled()
        if sorting:
            table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            for index, cells in enumerate(reversed(rows)):
                for column, value in enumerate(cells):
                    item = (table.numeric_item(column, value) if column in table._number_formats else
                            SortItem(str(value), str(value)[:10]))
                    table.setItem(index, column, item)
        finally:
            table.setUpdatesEnabled(True)
            if sorting:
                table.setSortingEnabled(True)

    def refresh(self, *_, portfolio_rows=None, lots=None, trade_rows=None):
        selected = self.sections.currentWidget()
        if selected is self.exposure_page:
            self.refresh_exposure(portfolio_rows)
            return
        if self.sections.currentWidget() is self.levels_page:
            self.levels_page.refresh(portfolio_rows=portfolio_rows, trade_rows=trade_rows)
            return
        if self.sections.currentWidget() is self.return_page:
            self.return_page.refresh(portfolio_rows, lots)
            return
        query = self.db.setting("analytics_flex_query_id")
        status = self.db.metadata("ibkr_analytics_sync_status") or "not synced"
        completed = self.db.metadata("ibkr_analytics_sync_completed")
        error = self.db.metadata("ibkr_analytics_sync_error")
        self.sync_status.setText(
            "History not set up" if not query else
            "History sync failed" if status == "failed" else
            f"History · {completed[:10]}" if completed else f"History · {status}")
        self.sync_status.setToolTip(error if status == "failed" and error else
                                    "Completed NAV and capital flows come from IBKR Flex.")
        snapshots, flows = self.db.analytics_history(self.portfolio_id)
        monthly = self.db.monthly_history(self.portfolio_id)
        saved_months = {row["month"] for row in monthly}
        flex_monthly = ([row for row in flex_monthly_history(snapshots, flows)
                         if row["month"] not in saved_months]
                        if selected is self.monthly_page else [])
        reported = self.db.reported_account_performance(self.portfolio_id)
        reported_text = (
            f"PortfolioAnalyst reported since-inception {reported['measure']} return: "
            f"{self._percent(float(reported['cumulative_return_pct']))} "
            f"({reported['period_from']} to {reported['period_to']})."
            if reported else "No since-inception performance report imported.")
        self.reported_performance_monthly.setText(reported_text)
        self.reported_performance_pnl.setText(reported_text)
        self.reported_performance_pnl.setVisible(bool(reported) and
                                                 self.range_combo.currentText() == "All" and
                                                 self.pnl_view_combo.currentIndex() == 0)
        if selected is self.monthly_page:
            self._fill_table(self.monthly_table, [
                (row["month"], f'{row["period_from"]} to {row["period_to"]}',
                 row["nav_usd"], row["return_pct"], source)
                for row, source in sorted(
                    [(row, "PortfolioAnalyst") for row in monthly] +
                    [(row, "Flex") for row in flex_monthly],
                    key=lambda item: item[0]["month"])])
            return
        trades = trade_rows if trade_rows is not None else self.service.matching.trade_rows()
        available = [date.fromisoformat(row["day"]) for row in snapshots]
        available += [date.fromisoformat(row["trade_date"]) for row in trades if row.get("trade_date")]
        available += [date.fromisoformat(row["period_from"]) for row in monthly]
        self._earliest_day = min(available) if available else date.today()
        if self.range_combo.currentText() == "All":
            self._set_dates(*self._preset_bounds("All"))
        first, last = self.start_date.date().toPython(), self.end_date.date().toPython()
        self.date_summary.setText(f"{first:%d %b %Y} – {last:%d %b %Y}")
        self._update_range_controls()
        for navigator in (self.pnl_navigator, self.capital_navigator):
            navigator.set_range(self._earliest_day, max(date.today(), self._earliest_day),
                                first, last, self.palette_name)
        monthly_overlap = (first <= last and any(
            row["period_from"] <= last.isoformat() and row["period_to"] >= first.isoformat()
            for row in monthly))
        if selected is self.capital_page:
            self._cash_share_rows = cash_share_rows(snapshots, first, last)
            capital_rows, capital_totals = (capital_flow_rows(trades, first, last)
                                           if first <= last else ([], None))
            if monthly_overlap:
                capital_totals = None
            for label, key in (("Bought", "bought"), ("Sold", "sold"), ("Net deployed", "net")):
                self.capital_stats[label].setText(
                    self._money(capital_totals[key] if capital_totals else None))
            self.capital_note.setText(
                "Choose a start date on or before the end date." if first > last else
                "2021 statement activity is retained as evidence; daily trade and capital "
                "analysis is unavailable for this range." if monthly_overlap else
                "Some trades have no USD value; totals are unavailable." if capital_totals is None else "")
            self.capital_note.setVisible(capital_totals is None)
            self._capital_rows = capital_rows if capital_totals is not None else []
            self._capital_valid = capital_totals is not None
            self._render_capital_chart()
            self._fill_table(self.capital_table, [
                (row["day"], row["bought"], row["sold"],
                 row["net"], row["cumulative_net"] if capital_totals else None)
                for row in capital_rows])
            self._fill_table(self.cash_share_table, [
                (row["day"] + (" · provisional" if row["source"] == "live" else ""),
                 row["cash"], row["nav"], row["percent"])
                for row in self._cash_share_rows])
            return
        if first > last:
            self.pnl_hint.setText("Choose a start date on or before the end date.")
            self.pnl_hint.show()
            self._pnl_rows = []
            self._flow_events = []
            pnl_rows, complete = [], False
        else:
            pnl_rows, complete = performance_window_rows(snapshots, flows, first, last)
            if monthly_overlap:
                complete = False
            self._pnl_rows = pnl_rows
            self._flow_events = [flow for flow in flows if first.isoformat() <= flow["day"] <= last.isoformat()]
            adjusted_dates = [row["day"] for row in pnl_rows if row["performance"] is not None]
            pending_live = bool(complete and pnl_rows and adjusted_dates and
                                pnl_rows[-1]["day"] > adjusted_dates[-1])
            self.pnl_hint.setText(
                "Daily analysis unavailable for 2021: these reports provide monthly NAV, "
                "not daily NAV. See Monthly P&L." if monthly_overlap else
                "Later live NAV is provisional." if pending_live else
                "Adjusted measures need a prior close and consecutive NAV/flow coverage."
                if not complete else "")
            self.pnl_hint.setVisible(pending_live or not complete)
        self._complete_pnl = complete
        observed = [row for row in pnl_rows if row["nav"] is not None]
        completed = [row for row in observed if row["source"] != "live"]
        latest = completed[-1] if completed else (observed[-1] if observed else None)
        self.as_of_label.setText(
            f"NAV close · {date.fromisoformat(latest['day']):%d %b %Y}"
            if latest and latest["source"] != "live" else "Live only" if latest else "")
        for label, key, unit in self.METRICS:
            metric_rows = [row for row in observed if row[key] is not None] if complete else []
            value = (latest[key] if latest else None) if key in ("nav", "nav_change") else (
                metric_rows[-1][key] if metric_rows else None)
            self.stats[label].setText(self._percent(value) if unit == "%" else self._money(value))
        self._daily_summary = daily_pnl_summary(pnl_rows, complete)
        self._update_daily_summary()
        calendar_rows = covered_daily_pnl_rows(snapshots, flows)
        calendar_first = (date.fromisoformat(calendar_rows[0]["day"])
                          if calendar_rows else date.today())
        calendar_last = (date.fromisoformat(calendar_rows[-1]["day"])
                         if calendar_rows else date.today())
        self.pnl_calendar.set_data(calendar_rows, calendar_first, calendar_last, True)
        self.pnl_calendar.set_note(
            "Daily P&L needs consecutive completed NAV and flow coverage. "
            "See Monthly P&L for monthly-only history."
            if not any(row["daily_pnl"] is not None for row in calendar_rows) else "")
        self._render_pnl_chart()
        self._fill_table(self.pnl_table, [
            (row["day"] + (" · provisional" if row["source"] == "live" else ""),
             row["nav"], row["nav_change"],
             row["adjusted_change"] if complete else None,
             row["performance"] if complete else None,
             row["external_flow"],
             row["daily_pnl"] if complete else None) for row in pnl_rows])

    def refresh_exposure(self, portfolio_rows=None):
        self.exposure_page.refresh(portfolio_rows)

    def _base_chart(self, show_legend=False):
        chart = QChart()
        colors = COLORS[self.palette_name]
        chart.setBackgroundVisible(True)
        chart.setBackgroundBrush(QColor(colors["surface"]))
        chart.setBackgroundPen(QPen(QColor(colors["line"]), 1))
        chart.setBackgroundRoundness(6)
        chart.setMargins(QMargins(12, 8, 12, 4))
        chart.legend().setVisible(show_legend)
        chart.legend().setAlignment(Qt.AlignBottom)
        chart.legend().setLabelColor(QColor(colors["muted"]))
        chart.legend().setFont(self.font())
        return chart

    def _finish_chart(self, view, chart, empty_message=None, *, hover=(), benchmark_hover=(),
                      hover_mode="nearest"):
        colors = COLORS[self.palette_name]
        view.setBackgroundBrush(QColor(colors["canvas"]))
        view.setFrameShape(QChartView.NoFrame)
        if empty_message:
            chart.setTitle(empty_message)
            chart.setTitleBrush(QColor(colors["muted"]))
            chart.setTitleFont(self.font())
        view.setChart(chart)
        view.set_hover_data(hover, benchmark_hover, self.palette_name, hover_mode)

    def _axes(self, chart, series, values, first_stamp, last_stamp, percent=False,
              zero_based=True):
        colors = COLORS[self.palette_name]
        x_axis = QDateTimeAxis()
        first_day = QDateTime.fromMSecsSinceEpoch(first_stamp).date().toPython()
        last_day = QDateTime.fromMSecsSinceEpoch(last_stamp).date().toPython()
        span = max(1, (last_day - first_day).days)
        interval = next((days for days in (1, 2, 7, 14, 28, 91, 182, 364, 728)
                         if span / days <= 7), 1456)
        anchor = date(2020, 1, 6)
        start_index = math.floor((first_day - anchor).days / interval)
        end_index = max(start_index + 1, math.ceil((last_day - anchor).days / interval))
        axis_start = anchor + timedelta(days=start_index * interval)
        axis_end = anchor + timedelta(days=end_index * interval)
        x_axis.setFormat("MMM d" if interval <= 28 else "MMM yy")
        x_axis.setTickCount(end_index - start_index + 1)
        x_axis.setRange(QDateTime(QDate(axis_start.year, axis_start.month, axis_start.day), QTime(0, 0)),
                        QDateTime(QDate(axis_end.year, axis_end.month, axis_end.day), QTime(0, 0)))
        y_axis = QValueAxis()
        low, high, count, decimals = _nice_scale(values, zero_based=zero_based)
        y_axis.setRange(low, high)
        y_axis.setTickCount(count)
        if percent:
            y_axis.setLabelFormat(f"%.{decimals}f%%")
        else:
            chart.setLocale(QLocale("en_US"))
            chart.setLocalizeNumbers(True)
            y_axis.setLabelFormat(f"%.{decimals}f")
            y_axis.setTitleText("USD")
        for axis in (x_axis, y_axis):
            axis.setLabelsFont(self.font())
            axis.setLabelsColor(QColor(colors["muted"]))
            axis.setGridLineColor(QColor(colors["line"]))
            axis.setLinePenColor(QColor(colors["line"]))
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        for line in series:
            line.attachAxis(x_axis)
            line.attachAxis(y_axis)
        return x_axis, y_axis

    def _line(self, chart, name, points, color, width=2):
        series = QLineSeries()
        series.setName(name)
        series.setPen(QPen(QColor(color), width))
        for stamp, value in points:
            series.append(stamp, value)
        chart.addSeries(series)
        return series

    def _benchmark_for_range(self, *, rebased=False):
        if not self.benchmark_toggle.isChecked():
            self.benchmark_toggle.setToolTip("Show S&P 500 price change on the right percent axis")
            return None, []
        first, last = self.start_date.date().toPython(), self.end_date.date().toPython()
        prices = self.db.benchmark_history()
        window = _rebased_benchmark_window if rebased else _benchmark_window
        baseline, points = window(prices, first, last) if first <= last else (None, [])
        if points:
            self.benchmark_toggle.setToolTip(
                f"S&P 500 {'rebased from' if rebased else 'price change from'} "
                f"the {baseline[0]:%d %b %Y} close; "
                f"latest cached close {points[-1][0]:%d %b %Y}")
        else:
            reason = (self.db.metadata("ibkr_benchmark_error") or
                      "Connect to IBKR to fetch completed S&P 500 daily closes") if not prices else (
                      "No S&P 500 close in this range" if rebased else
                      "A prior S&P 500 close is needed for this range" if baseline is None else
                      "No completed S&P 500 close in this range")
            self.benchmark_toggle.setToolTip(reason)
        return baseline, points

    def _benchmark_toggled(self, *_):
        self._render_pnl_chart()
        self._render_capital_chart()
        self._benchmark_for_range(rebased=self.sections.currentWidget() is self.pnl_page)

    @staticmethod
    def _benchmark_hover_rows(points, *, rebased=False):
        change_label = "Rebased change" if rebased else "Price change"
        return [(_timestamp(day.isoformat()),
                 f"S&P 500 · {day.isoformat()}\nClose: {close:,.2f}\n"
                 f"{change_label}: {change:+,.2f}%")
                for day, close, change in points]

    def _add_benchmark_overlay(self, chart, x_axis, points, *, scale=None,
                               rebased=False):
        if not points:
            return
        colors = COLORS[self.palette_name]
        benchmark = self._line(
            chart, "S&P 500 price change",
            [(_timestamp(day.isoformat()), change) for day, _close, change in points],
            colors["benchmark"], 2)
        benchmark.setPointsVisible(len(points) == 1)
        pen = benchmark.pen()
        pen.setStyle(Qt.DashLine)
        benchmark.setPen(pen)
        right_axis = QValueAxis()
        low, high, count, decimals = (scale or
                                      _nice_scale([change for _day, _close, change in points]))
        right_axis.setRange(low, high)
        right_axis.setTickCount(count)
        right_axis.setLabelFormat(f"%.{decimals}f")
        right_axis.setTitleText("S&P 500 rebased (%)" if rebased else
                                "S&P 500 price change (%)")
        right_axis.setTitleFont(self.font())
        right_axis.setTitleBrush(QColor(colors["benchmark"]))
        right_axis.setLabelsFont(self.font())
        right_axis.setLabelsColor(QColor(colors["benchmark"]))
        right_axis.setLinePenColor(QColor(colors["benchmark"]))
        right_axis.setGridLineVisible(False)
        chart.addAxis(right_axis, Qt.AlignRight)
        benchmark.attachAxis(x_axis)
        benchmark.attachAxis(right_axis)

    def _render_pnl_chart(self, *_):
        if not hasattr(self, "_pnl_rows"):
            return
        for label, value in self.stats.items():
            value.setProperty("analyticsSelected", label == self.metric_combo.currentText())
            value.style().unpolish(value)
            value.style().polish(value)
        colors = COLORS[self.palette_name]
        label, field, unit = self.METRICS[self.metric_combo.currentIndex()]
        raw = field in ("nav", "nav_change")
        chart = self._base_chart()
        _, benchmark_points = self._benchmark_for_range(rebased=True)
        comparison = bool(benchmark_points)
        completed = [row for row in self._pnl_rows if row["nav"] is not None
                     and row["source"] != "live"]
        chart_rows = completed if completed else [row for row in self._pnl_rows if row["nav"] is not None]
        metric_rows = [row for row in chart_rows if row[field] is not None]
        metric_rows = metric_rows if (raw or self._complete_pnl) else []
        original_points = [(_timestamp(row["day"]), row[field]) for row in metric_rows]
        points = (_rebased_pnl_points(metric_rows, field) if comparison else original_points)
        hover_by_day = {}
        if points:
            line = self._line(chart, label, points, colors["accent"], 2)
            line.setPointsVisible(len(points) == 1)
            cash_events = [flow for flow in self._flow_events
                           if flow.get("kind") in ("cash", "deposit", "withdrawal", "net external")
                           and flow["amount_usd"]]
            flow_stamps = [_timestamp(flow["day"]) for flow in cash_events]
            first_stamp = min([points[0][0], *flow_stamps]) if raw else points[0][0]
            last_stamp = max([points[-1][0], *flow_stamps]) if raw else points[-1][0]
            if benchmark_points:
                first_stamp = min(first_stamp, _timestamp(benchmark_points[0][0].isoformat()))
                last_stamp = max(last_stamp, _timestamp(benchmark_points[-1][0].isoformat()))
            if comparison:
                first_stamp = min(first_stamp, _timestamp(self.start_date.date().toString("yyyy-MM-dd")))
                last_stamp = max(last_stamp, _timestamp(self.end_date.date().toString("yyyy-MM-dd")))
            values = [value for _, value in points]
            if comparison:
                values += [change for _day, _close, change in benchmark_points]
            scale = _nice_scale(values) if comparison else None
            x_axis, y_axis = self._axes(chart, [line], values,
                                         first_stamp, last_stamp,
                                         percent=comparison or unit == "%",
                                         zero_based=comparison or field != "nav")
            y_axis.setTitleText(f"{label} rebased (%)" if comparison else
                                "USD" if unit == "USD" else "Performance (%)")
            y_axis.setTitleBrush(QColor(colors["accent"]))
            y_axis.setTitleFont(self.font())
            for row, (_stamp, plotted) in zip(metric_rows, points):
                original = row[field]
                original_text = (self._percent(original) if unit == "%" else
                                 self._money(original))
                details = ((f"{label} rebased: {plotted:+,.2f}%\n"
                            f"Original {label}: {original_text}") if comparison else
                           f"{label}: {original_text}")
                hover_by_day[row["day"]] = f"{row['day']}\n{details}"
            if raw:
                by_day = defaultdict(list)
                for flow in cash_events:
                    amount = float(flow["amount_usd"])
                    if amount:
                        by_day[flow["day"]].append(amount)
                for day, amounts in sorted(by_day.items()):
                    stamp = _timestamp(day)
                    if not first_stamp <= stamp <= last_stamp:
                        continue
                    marker = self._line(chart, f"External flow {day}",
                                        [(stamp, y_axis.min()), (stamp, y_axis.max())],
                                        colors["warning"] if sum(amounts) >= 0 else colors["negative"], 1)
                    marker.setPen(QPen(marker.pen().color(), 1, Qt.DashLine))
                    marker.attachAxis(x_axis)
                    marker.attachAxis(y_axis)
                    details = "\n".join(
                        f"{'Deposit' if amount > 0 else 'Withdrawal'}: "
                        f"{self._money(abs(amount))}" for amount in amounts)
                    hover_by_day[day] = f"{hover_by_day.get(day, day)}\n{details}"
            self._add_benchmark_overlay(chart, x_axis, benchmark_points,
                                        scale=scale, rebased=comparison)
        self._finish_chart(
            self.pnl_chart, chart,
            None if points else ("Positive starting NAV needed for rebased comparison"
                                 if comparison and original_points else
                                 "Prior closing NAV unavailable for this range"
                                 if raw and chart_rows else "No NAV observations in this range"
                                 if raw else "Completed NAV and external flows are needed"),
            hover=[(_timestamp(day), details) for day, details in hover_by_day.items()],
            benchmark_hover=self._benchmark_hover_rows(benchmark_points,
                                                       rebased=comparison) if points else ())

    def _render_capital_chart(self, *_):
        if not hasattr(self, "_capital_rows"):
            return
        if self.capital_combo.currentText() == "Cash / portfolio":
            self._render_cash_share_chart()
            return
        rows = self._capital_rows
        colors = COLORS[self.palette_name]
        chart = self._base_chart()
        first = self.start_date.date().toPython()
        last = self.end_date.date().toPython()
        activity = self.capital_combo.currentIndex() == 1
        period, buckets = _capital_activity_buckets(rows, first, last) if rows else ("day", [])
        caption = (
            f"{period.capitalize()} totals · Bought above zero · Sold below zero" if activity else
            f"Cumulative net deployed since {first:%d %b %Y}")
        _, benchmark_points = self._benchmark_for_range()
        if self.benchmark_toggle.isChecked():
            if benchmark_points:
                caption += f" · S&P 500 through {benchmark_points[-1][0]:%d %b %Y}"
            else:
                caption += " · S&P 500 unavailable"
        self.capital_caption.setText(caption)
        hover = []
        if rows or benchmark_points:
            start_stamp = _timestamp(first.isoformat())
            end_stamp = _timestamp(last.isoformat())
            series = []
            values = [0]
            if activity:
                bar_width = max(8, min(20, self.capital_chart.width() // max(12, 2 * len(buckets))))
                for bucket_start, bucket_end, midpoint, bought, sold in buckets:
                    span_label = (bucket_start.isoformat() if bucket_start == bucket_end else
                                  f"{bucket_start.isoformat()} to {bucket_end.isoformat()}")
                    details = (f"{span_label}\nBought: {self._money(bought)}\n"
                               f"Sold: {self._money(sold)}\n"
                               f"Net: {'+' if bought >= sold else '-'}"
                               f"${abs(bought - sold):,.2f}")
                    stamp = _timestamp(midpoint.isoformat())
                    hover.append((stamp, details))
                    for name, amount, color in (("Bought", bought, colors["positive"]),
                                                ("Sold", -sold, colors["warning"])):
                        if not amount:
                            continue
                        bar = self._line(chart, f"{name} {span_label}",
                                         ((stamp, 0), (stamp, amount)), color, bar_width)
                        pen = bar.pen()
                        pen.setCapStyle(Qt.FlatCap)
                        bar.setPen(pen)
                        series.append(bar)
                        values.append(amount)
            else:
                points = [(start_stamp, 0)]
                prior_value = 0
                for row in rows:
                    value = row["cumulative_net"]
                    stamp = _timestamp(row["day"])
                    points.extend(((stamp, prior_value), (stamp, value)))
                    values.append(value)
                    prior_value = value
                if end_stamp > points[-1][0]:
                    points.append((end_stamp, points[-1][1]))
                line = self._line(chart, "Net deployed", points, colors["accent"], 3)
                series.append(line)
                hover = [(start_stamp, f"{first.isoformat()}\nNet deployed: +$0.00")]
                for row in rows:
                    amount = row["cumulative_net"]
                    signed = f"{'+' if amount >= 0 else '-'}${abs(amount):,.2f}"
                    hover.append((_timestamp(row["day"]),
                                  f"{row['day']}\nNet deployed: {signed}"))
            zero = self._line(chart, "Zero", ((start_stamp, 0), (end_stamp, 0)),
                              colors["muted"], 1)
            series.append(zero)
            x_axis, _ = self._axes(chart, series, values, start_stamp, end_stamp)
            self._add_benchmark_overlay(chart, x_axis, benchmark_points)
        self._finish_chart(self.capital_chart, chart,
                           None if rows or benchmark_points else "No trades in this range",
                           hover=hover,
                           benchmark_hover=self._benchmark_hover_rows(benchmark_points),
                           hover_mode="nearest" if activity else "previous")

    def _render_cash_share_chart(self):
        rows = self._cash_share_rows
        colors = COLORS[self.palette_name]
        chart = self._base_chart()
        first = self.start_date.date().toPython()
        last = self.end_date.date().toPython()
        _, benchmark_points = self._benchmark_for_range()
        live_only = bool(rows and all(row["source"] == "live" for row in rows))
        caption = ("Cash / portfolio NAV · live value provisional" if live_only else
                   "Cash / portfolio NAV · completed closes")
        if self.benchmark_toggle.isChecked() and rows:
            caption += (f" · S&P 500 through {benchmark_points[-1][0]:%d %b %Y}"
                        if benchmark_points else " · S&P 500 unavailable")
        self.capital_caption.setText(caption)
        hover = []
        if rows:
            points = [(_timestamp(row["day"]), row["percent"]) for row in rows]
            line = self._line(chart, "Cash / portfolio", points, colors["accent"], 3)
            line.setPointsVisible(len(points) == 1)
            start_stamp = _timestamp(first.isoformat())
            end_stamp = _timestamp(last.isoformat())
            x_axis, y_axis = self._axes(
                chart, [line], [row["percent"] for row in rows],
                start_stamp, end_stamp, percent=True)
            y_axis.setTitleText("Cash / portfolio NAV (%)")
            y_axis.setTitleBrush(QColor(colors["accent"]))
            y_axis.setTitleFont(self.font())
            hover = [(_timestamp(row["day"]),
                      f"{row['day']}{' · provisional' if row['source'] == 'live' else ''}"
                      f"\nCash: {self._money(row['cash'])}"
                      f"\nNAV: {self._money(row['nav'])}"
                      f"\nCash / NAV: {row['percent']:,.2f}%")
                     for row in rows]
            self._add_benchmark_overlay(chart, x_axis, benchmark_points)
        self._finish_chart(self.capital_chart, chart,
                           None if rows else "Cash and positive NAV observations are needed",
                           hover=hover,
                           benchmark_hover=self._benchmark_hover_rows(benchmark_points) if rows else ())

    def set_theme(self, theme):
        self.palette_name = theme
        self.pnl_calendar.set_theme(theme)
        self.levels_page.set_theme(theme)
        self.exposure_page.set_theme(theme)
        self.return_page.set_theme(theme)
        self.refresh()
