"""Shared QChart setup for Analytics and Exposure charts."""

from PySide6.QtCore import QMargins, Qt
from PySide6.QtCharts import QChart, QChartView
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel

from app.views.ui_theme import COLORS


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
