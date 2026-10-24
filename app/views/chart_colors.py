"""Saved chart-series colors using the account summary's solid swatches."""

from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QMenu, QPushButton

from app.views.color_palette import ColorPaletteMenu
from app.views.ui_theme import COLORS, SUMMARY_BORDER_SOLIDS, style_toolbar_dropdown


class ChartColorControl(QPushButton):
    """A series picker whose selections survive chart redraws and app restarts."""

    def __init__(self, db, portfolio_id, series, changed, parent=None, *, selection=None):
        super().__init__("Colors", parent)
        self.db = db
        self.portfolio_id = portfolio_id
        self.series = series  # (key, label, default theme color key)
        self.changed = changed
        self.theme = "light"
        self._selection = selection if selection is not None else {}
        for key, _label, _default in series:
            if key in self._selection:
                continue
            prefix = self._setting_key(key)
            palette = db.setting(f"{prefix}_palette") or "default"
            if palette not in ("default", "solid"):
                palette = "default"
                db.set_setting(f"{prefix}_palette", palette)
            color = db.setting(f"{prefix}_color") or COLORS["light"]["accent"]
            self._selection[key] = (palette, QColor(color).name() if QColor(color).isValid()
                                    else COLORS["light"]["accent"])
        self.setAccessibleName("Chart colors")
        self.setToolTip("Change chart line and bar colors")
        self.set_theme("light")

    def _setting_key(self, key):
        return f"analytics_chart_color_{self.portfolio_id}_{key}"

    def set_theme(self, theme):
        self.theme = theme
        old_menu = self.menu()
        menu = QMenu(self) if len(self.series) > 1 else None
        self.pickers = {}
        for key, label, default in self.series:
            presets = [("default", "Default", (self._default_color(default),))]
            palette, color = self._selection[key]
            picker = ColorPaletteMenu(label, presets, SUMMARY_BORDER_SOLIDS,
                                      palette, color, menu or self)
            picker.colorSelected.connect(
                lambda selected, solid, series_key=key: self.set_color(series_key, selected, solid))
            if menu is not None:
                menu.addMenu(picker)
            self.pickers[key] = picker
        self.setMenu(menu or picker)
        style_toolbar_dropdown(self)
        if old_menu is not None:
            old_menu.deleteLater()

    def _default_color(self, default):
        return COLORS[self.theme][default]

    def set_visible_keys(self, keys):
        visible = set(keys)
        for key, picker in self.pickers.items():
            picker.menuAction().setVisible(key in visible)

    def set_color(self, key, palette, color):
        if key not in self._selection or palette not in ("solid", "default"):
            return
        if palette == "solid" and not QColor(color).isValid():
            return
        current_color = QColor(color).name() if QColor(color).isValid() else self._selection[key][1]
        self._selection[key] = (palette, current_color)
        prefix = self._setting_key(key)
        self.db.set_setting(f"{prefix}_palette", palette)
        if palette == "solid":
            self.db.set_setting(f"{prefix}_color", current_color)
        self.pickers[key].set_selection(palette, current_color)
        self.changed(key)

    def brush(self, key):
        palette, color = self._selection[key]
        if palette == "solid":
            return QBrush(QColor(color))
        default = next(default for name, _label, default in self.series if name == key)
        return QBrush(QColor(self._default_color(default)))

    def pen(self, key, width):
        return QPen(self.brush(key), width)

    def axis_color(self, key):
        return self.brush(key).color()
