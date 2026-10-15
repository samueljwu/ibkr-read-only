"""Small theme-colored icons for the main window actions."""

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_PATHS = {
    "sync": ('<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8"/>'
             '<path d="M21 3v5h-5"/>'
             '<path d="M21 12a9 9 0 0 1-15.36 6.36L3 16"/>'
             '<path d="M8 16H3v5"/>'),
    "sun": ('<circle cx="12" cy="12" r="4"/>'
            '<path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.42 1.42'
            'm11.3 11.3 1.42 1.42M19.07 4.93l-1.42 1.42M6.35 17.65l-1.42 1.42"/>'),
    "moon": '<path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z"/>',
    "help": ('<circle cx="12" cy="12" r="9"/>'
             '<path d="M9.5 9a2.5 2.5 0 1 1 4.4 1.6c-.7.6-1.9 1.2-1.9 2.4"/>'
             '<path d="M12 17h.01"/>'),
    "settings": ('<path d="M10 2h4l.5 2.3 2 1.2 2.2-.7 2 3.4-1.7 1.7v2.2'
                 'l1.7 1.7-2 3.4-2.2-.7-2 1.2L14 22h-4l-.5-2.3-2-1.2-2.2.7-2-3.4'
                 'L5 14.1v-2.2l-1.7-1.7 2-3.4 2.2.7 2-1.2L10 2Z"/>'
                 '<circle cx="12" cy="12" r="3"/>'),
}


def action_icon(name, color):
    """Render one 20-pixel vector icon at 3x for sharp high-DPI controls."""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
           f'viewBox="0 0 24 24" fill="none" stroke="{color}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
           f'{_PATHS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(60, 60)
    pixmap.setDevicePixelRatio(3)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, 20, 20))
    painter.end()
    return QIcon(pixmap)
