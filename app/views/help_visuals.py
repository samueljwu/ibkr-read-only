"""Screenshots of real controls for the built-in guide."""

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget


class HelpScreenshot(QWidget):
    """Show a synthetic-data capture at the guide's available width."""

    LIGHT_CAPTURE_IN_DARK = frozenset({
        "matched_lots", "portfolio_overview", "portfolio_trades",
        "portfolio_cash_range", "portfolio_cash",
    })

    def __init__(self, name, description):
        super().__init__()
        self.name = name
        self.setAccessibleName(description)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_theme("light")

    def set_theme(self, theme):
        capture_theme = "light" if theme == "dark" and self.name in self.LIGHT_CAPTURE_IN_DARK else theme
        path = Path(__file__).with_name("help_assets") / f"{self.name}_{capture_theme}.png"
        self._original = QPixmap(str(path))
        if self._original.isNull():
            self._scaled = QPixmap()
            self.setFixedHeight(0)
            return
        self._fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def sizeHint(self):
        return QSize(0, self.height())

    def minimumSizeHint(self):
        return QSize(0, 0)

    def _fit(self):
        if self._original.isNull():
            return
        width = min(max(self.width(), 1), self._original.width())
        self._scaled = self._original.scaledToWidth(width, Qt.SmoothTransformation)
        if self.height() != self._scaled.height():
            self.setFixedHeight(self._scaled.height())
        self.update()

    def paintEvent(self, event):
        if not self._scaled.isNull():
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._scaled)
