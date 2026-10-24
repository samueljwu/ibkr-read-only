"""Shared QChart setup and scales for account charts."""

import math
from bisect import bisect_left
from datetime import date, timedelta
from decimal import Decimal

from PySide6.QtCore import QDate, QDateTime, QMargins, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QValueAxis
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QRubberBand, QWidget

from app.views.action_icons import action_icon
from app.views.widgets import button
from app.views.ui_theme import COLORS, CONTROL_HEIGHT, TOOLBAR_ITEM_SPACING, set_zoom_height


class ChartNavigation(QWidget):
    """Shared zoom/reset actions; range charts may also select analysis dates."""

    def __init__(self, chart, parent=None):
        super().__init__(parent)
        self.setToolTip("Drag the chart to pan; use the mouse wheel or these buttons to zoom")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOOLBAR_ITEM_SPACING)
        self.zoom_in = button("", lambda: chart.zoom_by(1.25))
        self.zoom_out = button("", lambda: chart.zoom_by(0.8))
        self.reset = button("", chart.reset_view)
        self.actions = ((self.zoom_in, "zoom_in", "Zoom in"),
                        (self.zoom_out, "zoom_out", "Zoom out"),
                        (self.reset, "reset_view", "Reset view"))
        for control, _, label in self.actions:
            control.setToolTip(label)
            control.setAccessibleName(label)
            set_zoom_height(control, CONTROL_HEIGHT)
            control.setFixedWidth(CONTROL_HEIGHT)
            control.setIconSize(QSize(16, 16))
            row.addWidget(control)
        if hasattr(chart, "set_range_selection"):
            self.select_dates = button("Select dates")
            self.select_dates.setCheckable(True)
            self.select_dates.setToolTip("Show From and To dates, or drag across the chart to select; Shift-drag pans")
            self.select_dates.toggled.connect(chart.set_range_selection)
            row.addWidget(self.select_dates)
        self.set_theme("light")

    def set_theme(self, theme):
        for control, name, _ in self.actions:
            control.setIcon(action_icon(name, COLORS[theme]["text"]))


def nice_scale(values, *, zero_based=True, min_step=0):
    """Round a value scale to readable 1/2/2.5/5 intervals."""
    low, high = min(values), max(values)
    if zero_based:
        low, high = min(0, low), max(0, high)
    elif low == high:
        padding = max(1.0, abs(low) * .01)
        low, high = low - padding, high + padding
    if low == high:
        return -1.0, 1.0, 3, 0
    target = max((high - low) / 5, min_step)
    magnitude = 10 ** math.floor(math.log10(target))
    step = next(part * magnitude for part in (1, 2, 2.5, 5, 10)
                if part * magnitude >= target)
    bottom = math.floor(low / step) * step
    top = math.ceil(high / step) * step
    count = round((top - bottom) / step) + 1
    decimals = min(8, max(0, -Decimal(str(step)).normalize().as_tuple().exponent))
    return bottom, top, count, decimals


def price_step(span, *, min_step=0):
    """Choose a readable price interval: 0.5/1/2/5 times a power of ten."""
    target = max(span / 5, min_step, 1e-12)
    magnitude = 10 ** math.floor(math.log10(target))
    return next(part * magnitude for part in (1, 2, 5, 10)
                if part * magnitude >= target)


def price_scale(values, *, min_step=0):
    """Return nonnegative bounds, tick interval, and automatic decimals."""
    low, high = min(values), max(values)
    if low == high:
        padding = max(high * .05, min_step, 1e-6)
        low, high = max(0, low - padding), high + padding
    step = price_step(high - low, min_step=min_step)
    bottom = max(0, math.floor(low / step) * step)
    top = max(bottom + step, math.ceil(high / step) * step)
    decimals = min(8, max(0, -Decimal(str(step)).normalize().as_tuple().exponent))
    return bottom, top, step, decimals


class NavigableChartView(QChartView):
    """Pan a date/value chart by dragging and zoom both axes with the wheel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self._full_range = None
        self.setToolTip("Drag to pan; use the mouse wheel to zoom. Reset restores the full chart.")

    def setChart(self, chart):
        super().setChart(chart)
        self._drag_pos = None
        axes = self._axes()
        self._full_range = ((axes[0].min().toMSecsSinceEpoch(),
                             axes[0].max().toMSecsSinceEpoch(),
                             [(axis.min(), axis.max()) for axis in axes[1]]) if axes else None)

    def _axes(self):
        chart = self.chart()
        x_axis = next((axis for axis in chart.axes(Qt.Horizontal)
                       if isinstance(axis, QDateTimeAxis)), None)
        y_axes = [axis for axis in chart.axes(Qt.Vertical) if isinstance(axis, QValueAxis)]
        return (x_axis, y_axes) if x_axis is not None and y_axes else None

    def _set_range(self, x_min, x_max, y_ranges):
        axes = self._axes()
        if (axes is None or x_max - x_min < 1000 or len(y_ranges) != len(axes[1]) or
                any(y_max <= y_min for y_min, y_max in y_ranges)):
            return
        axes[0].setRange(QDateTime.fromMSecsSinceEpoch(round(x_min)),
                         QDateTime.fromMSecsSinceEpoch(round(x_max)))
        for axis, (y_min, y_max) in zip(axes[1], y_ranges):
            if axis.property("nonnegativePriceAxis") and y_min < 0:
                y_max -= y_min
                y_min = 0
            axis.setRange(y_min, y_max)
            if axis.property("nonnegativePriceAxis"):
                step = price_step(y_max - y_min,
                                  min_step=axis.property("priceMinStep") or 0)
                axis.setTickInterval(step)
                if axis.property("priceAutoDecimals"):
                    decimals = min(8, max(0, -Decimal(str(step)).normalize().as_tuple().exponent))
                    axis.setLabelFormat(f"%.{decimals}f")

    def zoom_by(self, factor, position=None):
        axes = self._axes()
        if axes is None or factor <= 0:
            return
        plot = self.chart().plotArea()
        if plot.width() <= 0 or plot.height() <= 0:
            return
        center = position if position is not None else plot.center()
        x_fraction = max(0.0, min(1.0, (center.x() - plot.left()) / plot.width()))
        y_fraction = max(0.0, min(1.0, (plot.bottom() - center.y()) / plot.height()))
        x_min, x_max = (axis.toMSecsSinceEpoch() for axis in (axes[0].min(), axes[0].max()))
        x_span = (x_max - x_min) / factor
        x_anchor = x_min + x_fraction * (x_max - x_min)
        y_ranges = []
        for axis in axes[1]:
            y_min, y_max = axis.min(), axis.max()
            y_span = (y_max - y_min) / factor
            y_anchor = y_min + y_fraction * (y_max - y_min)
            y_ranges.append((y_anchor - y_fraction * y_span,
                             y_anchor + (1 - y_fraction) * y_span))
        self._set_range(x_anchor - x_fraction * x_span,
                        x_anchor + (1 - x_fraction) * x_span,
                        y_ranges)

    def reset_view(self):
        if self._full_range is not None:
            self._set_range(*self._full_range)

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton and self._axes() is not None and
                self.chart().plotArea().contains(event.position())):
            self._drag_pos = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is None:
            return super().mouseMoveEvent(event)
        axes = self._axes()
        plot = self.chart().plotArea()
        if axes and plot.width() > 0 and plot.height() > 0:
            delta = event.position() - self._drag_pos
            x_min, x_max = (axis.toMSecsSinceEpoch() for axis in (axes[0].min(), axes[0].max()))
            x_shift = -delta.x() * (x_max - x_min) / plot.width()
            y_ranges = []
            for axis in axes[1]:
                y_min, y_max = axis.min(), axis.max()
                y_shift = delta.y() * (y_max - y_min) / plot.height()
                y_ranges.append((y_min + y_shift, y_max + y_shift))
            self._set_range(x_min + x_shift, x_max + x_shift,
                            y_ranges)
            self._drag_pos = event.position()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_pos is not None:
            self._drag_pos = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._axes() is not None and self.chart().plotArea().contains(event.position()):
            steps = event.angleDelta().y() / 120
            if steps:
                self.zoom_by(1.25 ** steps, event.position())
                event.accept()
                return
        super().wheelEvent(event)


class RangeChartView(QChartView):
    selectionRequested = Signal(object, object)
    panRequested = Signal(int)
    resetRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._start = None
        self._panning = False
        self._selecting_range = False
        self._analysis_range = None
        self.set_range_selection(False)
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

    def set_range_selection(self, enabled):
        self._selecting_range = enabled
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.setAccessibleDescription("Drag to select analysis dates; Shift-drag to pan" if enabled else
                                      "Drag to pan analysis dates; use the mouse wheel to zoom")

    def set_analysis_range(self, first, last):
        self._analysis_range = (first.startOfDay().toMSecsSinceEpoch(),
                                last.startOfDay().toMSecsSinceEpoch())

    def zoom_by(self, factor, position=None):
        axes = self.chart().axes(Qt.Horizontal)
        if not axes or factor <= 0:
            return
        axis = axes[0]
        axis_first, axis_last = axis.min().toMSecsSinceEpoch(), axis.max().toMSecsSinceEpoch()
        first, last = self._analysis_range or (axis_first, axis_last)
        if first == last and factor >= 1:
            return
        area = self.chart().plotArea()
        fraction = 0.5
        if position is not None and area.width() > 0 and last > first:
            stamp = axis_first + (axis_last - axis_first) * (position.x() - area.left()) / area.width()
            fraction = max(0, min(1, (stamp - first) / (last - first)))
        # Axis ticks include padding; zoom the actual analysis range instead.
        span = max(86400000, (last - first) / factor)
        anchor = first + (last - first) * fraction
        self.selectionRequested.emit(
            QDateTime.fromMSecsSinceEpoch(round(anchor - span * fraction)).date(),
            QDateTime.fromMSecsSinceEpoch(round(anchor + span * (1 - fraction))).date())

    def reset_view(self):
        self.resetRequested.emit()

    def wheelEvent(self, event):
        if self.chart().plotArea().contains(event.position()) and event.angleDelta().y():
            self.zoom_by(1.25 ** (event.angleDelta().y() / 120), event.position())
            event.accept()
        else:
            super().wheelEvent(event)

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
            self._panning = not self._selecting_range or bool(event.modifiers() & Qt.ShiftModifier)
            if self._panning:
                self.setCursor(Qt.ClosedHandCursor)
            else:
                area = self.chart().plotArea().toAlignedRect()
                self._band.setGeometry(QRect(self._start.x(), area.top(), 1, area.height()))
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
                # Only dates are selected, so the highlight spans the plot's full height.
                band = area.toAlignedRect()
                band.setLeft(min(self._start.x(), end.x()))
                band.setRight(max(self._start.x(), end.x()))
                self._band.setGeometry(band)
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
        self.setCursor(Qt.CrossCursor if self._selecting_range else Qt.ArrowCursor)
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


def base_chart(theme, *, show_legend=False):
    """Create a QChart with the app's standard background/legend styling.

    Callers add series/axes afterward; this does not assume any series exist.
    """
    chart = QChart()
    colors = COLORS[theme]
    chart.setBackgroundVisible(True)
    chart.setBackgroundBrush(QColor(colors["surface"]))
    chart.setBackgroundPen(QPen(QColor(colors["line"]), 1))
    chart.setBackgroundRoundness(6)
    chart.setMargins(QMargins(12, 8, 12, 4))
    chart.legend().setVisible(show_legend)
    chart.legend().setAlignment(Qt.AlignBottom)
    chart.legend().setLabelColor(QColor(colors["muted"]))
    return chart


def finish_chart(view, chart, theme, empty_text=None):
    """Finalize a QChartView for a chart; show empty_text centered if there's no data.

    "No data" is determined by the chart having zero series.
    """
    colors = COLORS[theme]
    font = view.font()
    chart.setTitleFont(font)
    chart.legend().setFont(font)
    for axis in chart.axes():
        axis.setLabelsFont(font)
        axis.setTitleFont(font)
    view.setBackgroundBrush(QColor(colors["canvas"]))
    view.setFrameShape(QChartView.NoFrame)
    view.setRenderHint(QPainter.Antialiasing)
    has_data = bool(chart.series())
    if not has_data and empty_text:
        chart.setTitle(empty_text)
        chart.setTitleBrush(QColor(colors["muted"]))
    view.setChart(chart)
    overlay = getattr(view, "_empty_overlay", None)
    if overlay is None:
        overlay = QLabel(view)
        overlay.setObjectName("hint")
        overlay.setAlignment(Qt.AlignCenter)
        overlay.setWordWrap(True)
        view._empty_overlay = overlay
    if not has_data and empty_text:
        overlay.setText(empty_text)
        overlay.setGeometry(view.rect())
        overlay.show()
        overlay.raise_()
    else:
        overlay.hide()
    return view


def line_pen(color, width=2):
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen
