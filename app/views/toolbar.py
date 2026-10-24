"""Small wrapping toolbars that keep labels with their controls."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QSizePolicy, QWidget

from app.views.ui_theme import TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING


class _ToolbarLayout(QLayout):
    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self.items.append(item)
        self.invalidate()

    def count(self):
        return len(self.items)

    def itemAt(self, index):
        return self.items[index] if 0 <= index < len(self.items) else None

    def takeAt(self, index):
        return self.items.pop(index) if 0 <= index < len(self.items) else None

    def expandingDirections(self):
        return Qt.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def minimumSize(self):
        size = QSize()
        for item in self.items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        return size

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        # Two linear passes over a handful of control groups; no data-row scans.
        rows, row, used = [], [], 0
        for item in self.items:
            if item.isEmpty():
                continue
            size = item.sizeHint().expandedTo(item.minimumSize())
            if size.width() > rect.width() and item.hasHeightForWidth():
                size.setWidth(max(item.minimumSize().width(), rect.width()))
                size.setHeight(max(item.minimumSize().height(), item.heightForWidth(size.width())))
            gap = TOOLBAR_GROUP_SPACING if row else 0
            if row and used + gap + size.width() > rect.width():
                rows.append((row, used))
                row, used, gap = [], 0, 0
            row.append((item, size))
            used += gap + size.width()
        if row:
            rows.append((row, used))
        y = rect.y()
        for row, used in rows:
            height = max(size.height() for _, size in row)
            x = rect.x()
            for item, size in row:
                if item.widget().property("toolbarTrailing"):
                    x += max(0, rect.width() - used)
                    used = rect.width()
                if apply:
                    item.setGeometry(QRect(x, y + (height - size.height()) // 2,
                                           size.width(), size.height()))
                x += size.width() + TOOLBAR_GROUP_SPACING
            y += height + TOOLBAR_ITEM_SPACING
        return max(0, y - rect.y() - TOOLBAR_ITEM_SPACING)


class Toolbar(QWidget):
    """Wrap whole control groups; trailing controls align to the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.row = _ToolbarLayout(self)

    def add_control(self, widget, label=None, *, trailing=False):
        if label:
            caption = QLabel(label)
            caption.setBuddy(widget)
            if not widget.accessibleName():
                widget.setAccessibleName(label)
            return self.add_group(caption, widget, trailing=trailing)
        return self.add_group(widget, trailing=trailing)

    def add_group(self, *widgets, trailing=False):
        group = QWidget()
        group.setProperty("toolbarTrailing", trailing)
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOOLBAR_ITEM_SPACING)
        for widget in widgets:
            row.addWidget(widget)
        self.row.addWidget(group)
        return group
