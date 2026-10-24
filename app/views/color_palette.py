"""Compact reusable menu for gradient presets, solid swatches, and hex colors."""

from PySide6.QtCore import QSize, Signal, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QGridLayout, QLineEdit, QMenu, QToolButton, QVBoxLayout, QWidget, QWidgetAction


def _swatch_icon(colors):
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    gradient = QLinearGradient(0, 0, 22, 0)
    for index, color in enumerate(colors):
        gradient.setColorAt(index / max(1, len(colors) - 1), QColor(color))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(1, 1, 20, 20, 3, 3)
    painter.end()
    return QIcon(pixmap)


class ColorPaletteMenu(QMenu):
    colorSelected = Signal(str, str)

    def __init__(self, title, presets, swatches, selected, solid_color, parent=None):
        super().__init__(title, parent)
        self.preset_actions = {}
        self.swatch_buttons = {}
        for key, label, colors in presets:
            action = self.addAction(_swatch_icon(colors), label)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, choice=key:
                                     self.colorSelected.emit(choice, self.solid_color))
            self.preset_actions[key] = action
        self.addSeparator()

        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(7, 4, 7, 7)
        layout.setSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(3)
        for index, (name, color) in enumerate(swatches):
            button = QToolButton(panel)
            button.setAutoRaise(True)
            button.setCheckable(True)
            button.setFixedSize(28, 28)
            button.setIcon(_swatch_icon((color,)))
            button.setIconSize(QSize(22, 22))
            button.setToolTip(name)
            button.setAccessibleName(name)
            button.clicked.connect(lambda checked=False, value=color: self._choose_solid(value))
            grid.addWidget(button, index // 6, index % 6)
            self.swatch_buttons[color] = button
        layout.addLayout(grid)
        self.hex_input = QLineEdit(panel)
        self.hex_input.setMaxLength(7)
        self.hex_input.setPlaceholderText("#RRGGBB · Enter")
        self.hex_input.setAccessibleName("Custom hex color")
        self.hex_input.returnPressed.connect(self._submit_hex)
        layout.addWidget(self.hex_input)
        widget_action = QWidgetAction(self)
        widget_action.setDefaultWidget(panel)
        self.addAction(widget_action)
        self.set_selection(selected, solid_color)

    def set_selection(self, selected, solid_color):
        self.solid_color = solid_color
        for key, action in self.preset_actions.items():
            action.setChecked(key == selected)
        for color, button in self.swatch_buttons.items():
            button.setChecked(selected == "solid" and color == solid_color)
        self.hex_input.setText(solid_color)

    def _choose_solid(self, color):
        self.colorSelected.emit("solid", color)
        self.hide()

    def _submit_hex(self):
        value = self.hex_input.text().strip()
        if len(value) == 7 and value.startswith("#") and QColor(value).isValid():
            self._choose_solid(QColor(value).name())
        else:
            self.hex_input.setText(self.solid_color)
