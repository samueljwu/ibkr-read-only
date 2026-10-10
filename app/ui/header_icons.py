"""Theme-colored vector icons for the main header actions."""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.ui.ui_theme import COLORS


SHAPES = {
    "sync": ('<path d="M20 7v5h-5M4 17v-5h5"/>'
             '<path d="M6.1 6.1A8 8 0 0 1 19.6 10L20 12'
             'M4 12l.4 2A8 8 0 0 0 17.9 17.9"/>'),
    "help": ('<circle cx="12" cy="12" r="9"/>'
             '<path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2-3 4"/>'
             '<path d="M12 17h.01"/>'),
    "settings": ('<path d="M9 3h6l.5 3 2 .9 2.7-1.1 3 5.2-2.2 2'
                 'v2l2.2 2-3 5.2-2.7-1.1-2 .9-.5 3H9l-.5-3-2-.9'
                 '-2.7 1.1-3-5.2 2.2-2v-2l-2.2-2 3-5.2 2.7 1.1 2-.9z" '
                 'transform="translate(2 0) scale(.83)"/>'
                 '<circle cx="12" cy="12" r="3"/>'),
    "moon": '<path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13z"/>',
    "sun": ('<circle cx="12" cy="12" r="4"/>'
            '<path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4'
            'm11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
}


def header_icon(name, theme):
    """Render crisp icons at standard and high-DPI sizes, including disabled state."""
    icon = QIcon()
    for mode, color in ((QIcon.Normal, COLORS[theme]["text"]),
                        (QIcon.Disabled, COLORS[theme]["muted"])):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
               f'fill="none" stroke="{color}" stroke-width="1.8" '
               f'stroke-linecap="round" stroke-linejoin="round">{SHAPES[name]}</svg>')
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        for size in (18, 36, 54):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon
