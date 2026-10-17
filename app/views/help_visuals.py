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
        label = _label(text, role)
        if stretch:
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        row.addWidget(label, stretch)
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
            content.addLayout(_row(("1", "Step", 0), ("TWS / Gateway", "Control", 1),
                                   ("→", "Arrow", 0), ("Read-only API", "Status", 1)))
            content.addLayout(_row(("2", "Step", 0), ("Settings", "Button", 0),
                                   ("→", "Arrow", 0), ("Use IBKR · Apply Settings", "Control", 1)))
            content.addLayout(_row(("3", "Step", 0), ("Sync IBKR", "Button", 0),
                                   ("→", "Arrow", 0), ("Source status", "Status", 1)))
            caption = "Connect, configure, then sync · illustration"
        elif kind == "start_import":
            content.addLayout(_row(("Settings", "TabSelected", 0),
                                   ("Account + Flex query IDs", "Control", 1)))
            content.addLayout(_row(("Save Flex token", "Button", 0),
                                   ("→", "Arrow", 0), ("File → Import Data", "Button", 1)))
            content.addLayout(_row(("Date range", "Control", 1),
                                   ("→", "Arrow", 0), ("Progress + coverage gaps", "Status", 1)))
            caption = "Import older activity · illustration"
        elif kind == "portfolio":
            content.addLayout(_row(("Overview", "TabSelected", 0),
                                   ("Trades", "Tab", 0), ("Lots", "Tab", 1)))
            content.addLayout(_row(("Columns", "Control", 0),
                                   ("✓ Hide closed", "Status", 1),
                                   ("Type instrument", "Control", 1)))
            content.addLayout(_row(("Instrument", "Header", 2), ("Quantity", "Header", 1),
                                   ("Reconciliation", "Header", 2)))
            content.addLayout(_row(("Example holding ↗", "Cell", 2), ("10", "Cell", 1),
                                   ("OK", "Cell", 2)))
            caption = "Double-click a holding for trades and lots · illustration"
        elif kind == "cash":
            content.addLayout(_row(("Cash", "TabSelected", 0),
                                   ("Range: 3 months", "Control", 1)))
            content.addLayout(_row(("Date", "Header", 1), ("Type", "Header", 2),
                                   ("Amount", "Header", 1)))
            content.addLayout(_row(("Sample date", "Cell", 1), ("Dividend", "Cell", 2),
                                   ("+$12", "Cell", 1)))
            caption = "Choose a range to review Cash Flex activity · illustration"
        elif kind == "matched":
            content.addLayout(_row(("Lots", "TabSelected", 0),
                                   ("Status: Closed", "Control", 1),
                                   ("Match lots…", "Button", 0)))
            content.addLayout(_row(("SELL 10", "Cell", 1),
                                   ("drag ↓", "Arrow", 0),
                                   ("BUY 6 · drop here", "Control", 2)))
            content.addLayout(_row(("Chosen 6", "Status", 1),
                                   ("Auto 4", "Status", 1),
                                   ("Unmatched 0", "Status", 1)))
            content.addLayout(_row(("All matched / can reserve", "Header", 2),
                                   ("Matched sales", "Header", 1),
                                   ("Est. P&L", "Header", 1)))
            content.addLayout(_row(("6 / 6", "Cell", 2),
                                   ("↳ SELL 6", "Cell", 1),
                                   ("+$30", "Cell", 1)))
            content.addLayout(_row(("Split purchase", "Button", 1),
                                   ("Remove split", "Button", 1),
                                   ("Save changes", "Button", 1)))
            content.addLayout(_row(("Reset sale to automatic", "Button", 1),
                                   ("Cancel", "Button", 1)))
            caption = "Select a lot → drag sale to purchase → review → save · illustration"
        elif kind == "matching_actions":
            content.addLayout(_row(("Matches", "TabSelected", 1),
                                   ("Rebuild automatic matches", "Control", 2)))
            content.addLayout(_row(("Auto-match remaining", "Control", 1),
                                   ("FIFO · LIFO", "Status", 1)))
            content.addLayout(_row(("Max realized gain", "Control", 1),
                                   ("Max unrealized gain", "Control", 1)))
            content.addLayout(_row(("Clear match for selected lot", "Button", 1)))
            caption = "Other matching actions · illustration"
        elif kind == "analytics":
            content.addLayout(_row(("P&L", "TabSelected", 0),
                                   ("Capital deployment", "Tab", 0), ("Exposure", "Tab", 1)))
            content.addLayout(_row(("Range", "Status", 0), ("3 months", "Control", 0),
                                   ("Chart", "Status", 0), ("Adjusted NAV change", "Control", 1)))
            self.chart = _ExampleChart(theme)
            content.addWidget(self.chart)
            content.addLayout(_row(("Chart and table", "Status", 0),
                                   ("Top and bottom", "Control", 1)))
            caption = "Choose dates and chart; drag to select a range · illustration"
        elif kind == "analytics_more":
            content.addLayout(_row(("Returns", "TabSelected", 1),
                                   ("Position → components", "Control", 2),
                                   ("≈ partial", "Status", 1)))
            content.addLayout(_row(("Exposure", "Tab", 1),
                                   ("Pie slice → positions", "Control", 2),
                                   ("Manage categories", "Button", 1)))
            content.addLayout(_row(("Monthly P&L", "Tab", 1),
                                   ("Import older CSV history", "Button", 2)))
            content.addLayout(_row(("Buy / sell levels", "Tab", 1),
                                   ("Load older prices", "Button", 2)))
            caption = "Choose an Analytics subtab · illustration"
        elif kind == "rebalance":
            content.addLayout(_row(("Show", "Status", 0), ("All", "Control", 1)))
            content.addLayout(_row(("Add idea", "Status", 0),
                                   ("Ticker, e.g. NVDA", "Control", 1), ("Add", "Button", 0)))
            content.addLayout(_row(("Position", "Header", 2), ("Target weight", "Header", 1),
                                   ("Buy / Sell USD", "Header", 1)))
            content.addLayout(_row(("Example holding", "Cell", 2), ("20% ✎", "Control", 1),
                                   ("Buy $500", "Cell", 1)))
            caption = "Enter a target to see estimated trades · illustration"
        elif kind == "views":
            content.addLayout(_row(("Column heading ▾", "Control", 1),
                                   ("→", "Arrow", 0), ("Columns · decimals", "Status", 1)))
            content.addLayout(_row(("View", "TabSelected", 0),
                                   ("Save current tab view", "Button", 1)))
            content.addLayout(_row(("Drag tabs ↔", "Control", 1),
                                   ("View → Reset tab order", "Button", 1)))
            content.addLayout(_row(("File", "TabSelected", 0),
                                   ("Export Workbook · SQLite Backup", "Button", 1)))
            caption = "Adjust tables and keep copies · illustration"
        else:
            raise ValueError(f"Unknown help visual: {kind}")

        caption_label = _label(caption, "Caption")
        caption_label.setWordWrap(True)
        caption_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        outer.addWidget(caption_label)

    def set_theme(self, theme):
        if self.chart is not None:
            self.chart.theme = theme
            self.chart.update()
