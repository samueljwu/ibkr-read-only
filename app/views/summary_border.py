"""Paint the selectable account-summary frames."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from app.views.typography import BODY_POINT_SIZE, FONT_SIZES_PX
from app.views.ui_theme import COLORS, SUMMARY_BORDER_GRADIENTS


BORDER_STYLES = (
    ("classic", "Classic"),
    ("prism", "Prism rim"),
    ("flight", "Flight deck"),
    ("terminal", "Terminal"),
    ("holo", "Holo console"),
)
BORDER_PALETTES = (
    ("rainbow", "Rainbow"),
    ("ocean", "Ocean"),
    ("sunset", "Sunset"),
)
def _cut_corners(rect, cut):
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    return QPolygonF((QPointF(x, y + cut), QPointF(x + cut, y),
                      QPointF(x + w - cut, y), QPointF(x + w, y + cut),
                      QPointF(x + w, y + h - cut), QPointF(x + w - cut, y + h),
                      QPointF(x + cut, y + h), QPointF(x, y + h - cut)))


class SummaryBorder(QWidget):
    """One lightweight painter for every summary border style."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.style_name = "prism"
        self.palette_name = "rainbow"
        self.solid_color = COLORS["light"]["accent"]
        self.theme = "light"

    def set_appearance(self, style, palette, color, theme):
        self.style_name = style if style in dict(BORDER_STYLES) else "prism"
        self.palette_name = palette if palette in dict(BORDER_PALETTES) or palette == "solid" else "rainbow"
        self.solid_color = color if QColor(color).isValid() else COLORS[theme]["accent"]
        self.theme = theme
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(COLORS[theme]["surface"]))
        self.setPalette(palette)
        self.update()

    def _colors(self):
        if self.palette_name == "solid":
            return (QColor(self.solid_color),) * 4
        return tuple(QColor(value) for value in SUMMARY_BORDER_GRADIENTS[self.theme][self.palette_name])

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colors = self._colors()
        surface = QColor(COLORS[self.theme]["surface"])
        line = QColor(COLORS[self.theme]["line"])
        canvas = QColor(COLORS[self.theme]["canvas"])
        frame = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)

        if self.style_name == "classic":
            painter.setPen(QPen(line, 1))
            painter.setBrush(surface)
            painter.drawRoundedRect(frame, 10, 10)
            return

        gradient = QLinearGradient(frame.topLeft(), frame.bottomRight())
        for position, color in zip((0, .34, .68, 1), colors):
            gradient.setColorAt(position, color)
        painter.setPen(Qt.NoPen)

        if self.style_name == "flight":
            painter.setBrush(gradient)
            painter.drawPolygon(_cut_corners(frame, 22))
            painter.setBrush(surface)
            painter.drawPolygon(_cut_corners(frame.adjusted(5, 5, -5, -5), 18))
            painter.setBrush(canvas)
            for x, y in ((frame.right() - 83, frame.top()), (frame.left() + 35, frame.bottom() - 5)):
                painter.drawRect(QRectF(x, y, 14, 5))
                painter.drawRect(QRectF(x + 43, y, 8, 5))
            painter.setBrush(colors[2])
            for x, y in ((frame.right() - 69, frame.top()), (frame.left() + 49, frame.bottom() - 5)):
                painter.drawRect(QRectF(x, y, 29, 5))
            for x in (frame.left(), frame.right() - 5):
                for offset in (0, 9, 18):
                    painter.fillRect(QRectF(x, frame.center().y() - 12 + offset, 5, 4), colors[3])
            return

        if self.style_name == "terminal":
            painter.setBrush(surface)
            painter.setPen(QPen(colors[0], 2))
            painter.drawRect(frame.adjusted(1, 1, -1, -1))
            painter.setPen(QPen(colors[0], 1))
            painter.drawRect(frame.adjusted(6, 6, -6, -6))
            painter.drawLine(QPointF(frame.left() + 17, frame.top() + 25),
                             QPointF(frame.right() - 17, frame.top() + 25))
            painter.setPen(QColor(COLORS[self.theme]["text"]))
            font = self.font()
            font.setPointSizeF(font.pointSizeF() * FONT_SIZES_PX["caption_size"] /
                               FONT_SIZES_PX["body_size"])
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.drawText(QRectF(frame.left() + 18, frame.top() + 8, 180, 15),
                             Qt.AlignLeft | Qt.AlignVCenter, "> ACCOUNT SUMMARY")
            painter.setBrush(colors[3])
            painter.setPen(Qt.NoPen)
            painter.drawRect(QRectF(frame.right() - 48, frame.bottom() - 3, 34, 4))
            return

        if self.style_name == "holo":
            tint = QColor(colors[0])
            tint.setAlpha(36)
            painter.setBrush(tint)
            painter.drawRoundedRect(frame, 17, 17)
            painter.setBrush(surface)
            painter.setPen(QPen(line, 1))
            painter.drawRoundedRect(frame.adjusted(7, 7, -7, -7), 10, 10)
            corners = (
                (frame.left(), frame.top(), 1, 1),
                (frame.right(), frame.top(), -1, 1),
                (frame.left(), frame.bottom(), 1, -1),
                (frame.right(), frame.bottom(), -1, -1),
            )
            for color, (x, y, dx, dy) in zip(colors, corners):
                painter.setPen(QPen(color, 4, Qt.SolidLine, Qt.RoundCap))
                painter.drawLine(QPointF(x, y + 24 * dy), QPointF(x, y + 5 * dy))
                painter.drawLine(QPointF(x + 5 * dx, y), QPointF(x + 30 * dx, y))
            painter.setPen(Qt.NoPen)
            for x, color in ((frame.left() + frame.width() * .27, colors[0]),
                             (frame.left() + frame.width() * .73, colors[2])):
                painter.setBrush(canvas)
                painter.drawEllipse(QPointF(x, frame.top()), 6, 6)
                painter.setBrush(color)
                painter.drawEllipse(QPointF(x, frame.top()), 3, 3)
            return

        painter.setBrush(gradient)
        painter.drawRoundedRect(frame, 10, 10)
        painter.setBrush(surface)
        painter.drawRoundedRect(frame.adjusted(3, 3, -3, -3), 7, 7)
