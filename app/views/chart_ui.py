"""Shared QChart setup and scales for account charts."""

import math
from decimal import Decimal

from PySide6.QtCore import QDateTime, QMargins, Qt
from PySide6.QtCharts import QChart, QChartView, QDateTimeAxis, QValueAxis
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel

from app.views.ui_theme import COLORS


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
