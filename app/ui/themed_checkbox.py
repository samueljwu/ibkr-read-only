"""Checkbox with a visible tick in both application themes."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton


class ThemedCheckBox(QCheckBox):
    """Keep the tick visible when Qt's native dark palette meets a light theme."""

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.isChecked():
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#ffffff"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        corner = QPointF(rect.left() + 6, rect.bottom() - 4)
        painter.drawLine(QPointF(rect.left() + 3, rect.center().y()), corner)
        painter.drawLine(corner, QPointF(rect.right() - 2, rect.top() + 3))
