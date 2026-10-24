"""Screenshots of real controls for the built-in guide."""

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget


class HelpScreenshot(QWidget):
    """Show a synthetic-data capture at the guide's available width."""

    def __init__(self, name, description):
        super().__init__()
        self.name = name
        self.setAccessibleName(description)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_theme("light")

    def set_theme(self, theme):
        assets = Path(__file__).with_name("help_assets")
        path = assets / f"{self.name}_{theme}@2x.png"
        if not path.is_file():
            path = assets / f"{self.name}_{theme}.png"
        self._original = QPixmap(str(path))
        if self._original.isNull():
            self.setFixedHeight(0)
            return
        self._fit()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._fit)

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
        native = self._original.deviceIndependentSize()
        width = min(max(self.width(), 1), native.width())
        height = max(1, round(native.height() * width / native.width()))
        if self.height() != height:
            self.setFixedHeight(height)
        self.update()

    def paintEvent(self, event):
        if not self._original.isNull():
            native = self._original.deviceIndependentSize()
            width = min(self.width(), native.width())
            height = native.height() * width / native.width()
            painter = QPainter(self)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.drawPixmap(QRectF(0, 0, width, height), self._original,
                               QRectF(self._original.rect()))
