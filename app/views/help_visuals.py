"""Small, data-free replicas of controls shown in the built-in guide."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.views.ui_theme import COLORS


def _label(text, role):
    label = QLabel(text)
    label.setObjectName("helpMock" + role)
    label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return label


def _row(*items):
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(5)
    for text, role, stretch in items:
        row.addWidget(_label(text, role), stretch)
    return row


class _ExampleChart(QWidget):
    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.setFixedHeight(70)
        self.setAccessibleName("Illustrative performance chart")

    def paintEvent(self, _event):
        colors = COLORS[self.theme]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        painter.setPen(QPen(QColor(colors["line"]), 1))
        for y in (16, 38, 60):
            painter.drawLine(8, y, width - 8, y)
        points = (0.76, 0.68, 0.72, 0.49, 0.55, 0.35, 0.41, 0.18)
        line = [QPointF(10 + index * (width - 20) / (len(points) - 1),
                        8 + value * (height - 16)) for index, value in enumerate(points)]
        painter.setPen(QPen(QColor(colors["accent"]), 2))
        for first, second in zip(line, line[1:]):
            painter.drawLine(first, second)


class HelpVisual(QWidget):
    """An illustrative, noninteractive slice of the corresponding app screen."""

    def __init__(self, kind, theme="light"):
        super().__init__()
        self.setObjectName("helpVisual" + kind.title())
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 5)
        outer.setSpacing(4)
        frame = QFrame()
        frame.setObjectName("helpMockup")
        content = QVBoxLayout(frame)
        content.setContentsMargins(9, 8, 9, 8)
        content.setSpacing(6)
        outer.addWidget(frame)
        self.chart = None

        if kind == "start":
            content.addLayout(_row(("Sample account", "Title", 1),
                                   ("Sync IBKR", "Button", 0), ("Settings", "Button", 0)))
            content.addLayout(_row(("IBKR: Connected  ·  Data: Live", "Status", 1)))
            content.addLayout(_row(("Trades synced: previous report", "Status", 1)))
            caption = "Main window header and status bar · example"
        elif kind == "portfolio":
            content.addLayout(_row(("Overview", "TabSelected", 0),
                                   ("Trades", "Tab", 0), ("Lots", "Tab", 1)))
            content.addLayout(_row(("Columns", "Control", 0),
                                   ("✓ Hide closed", "Status", 1),
                                   ("Type instrument", "Control", 1)))
            content.addLayout(_row(("Instrument", "Header", 2), ("Quantity", "Header", 1),
                                   ("Reconciliation", "Header", 2)))
            content.addLayout(_row(("Example holding", "Cell", 2), ("10", "Cell", 1),
                                   ("OK", "Cell", 2)))
            caption = "Overview tabs, filters and table · example"
        elif kind == "analytics":
            content.addLayout(_row(("P&L", "TabSelected", 0),
                                   ("Capital deployment", "Tab", 0), ("Exposure", "Tab", 1)))
            content.addLayout(_row(("Range", "Status", 0), ("3 months", "Control", 0),
                                   ("Chart", "Status", 0), ("Adjusted NAV change", "Control", 1)))
            self.chart = _ExampleChart(theme)
            content.addWidget(self.chart)
            content.addLayout(_row(("Chart and table", "Status", 0),
                                   ("Top and bottom", "Control", 1)))
            caption = "Analytics controls and illustrative chart"
        elif kind == "rebalance":
            content.addLayout(_row(("Show", "Status", 0), ("All", "Control", 1),
                                   ("Pause updates", "Button", 0)))
            content.addLayout(_row(("Add idea", "Status", 0),
                                   ("Ticker, e.g. NVDA", "Control", 1), ("Add", "Button", 0)))
            content.addLayout(_row(("Position", "Header", 2), ("Target weight", "Header", 1),
                                   ("Buy / Sell USD", "Header", 1)))
            content.addLayout(_row(("Example holding", "Cell", 2), ("20%", "Cell", 1),
                                   ("—", "Cell", 1)))
            caption = "Rebalance planning controls · example"
        else:
            raise ValueError(f"Unknown help visual: {kind}")

        caption_label = _label(caption, "Caption")
        outer.addWidget(caption_label)

    def set_theme(self, theme):
        if self.chart is not None:
            self.chart.theme = theme
            self.chart.update()
