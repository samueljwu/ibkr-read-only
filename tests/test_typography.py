"""The bundled UI font, text hierarchy and zoomed layouts stay readable."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtWidgets import QApplication, QWidget

from app.ui import MainWindow
from app.views import typography
from app.views.database_selection import DatabaseSelectionWindow
from app.views.table_ui import TABLE_ROW_HEIGHT
from app.views.typography import FONT_FAMILY, FONT_SIZES_PX, install_app_font
from app.views.ui_theme import PAGE_MARGINS, stylesheet


def _margins(layout):
    value = layout.contentsMargins()
    return value.left(), value.top(), value.right(), value.bottom()


def test_body_font_units_are_platform_specific(monkeypatch):
    app = QApplication.instance() or QApplication([])
    previous_font = QFont(app.font())
    try:
        monkeypatch.setattr(typography, "IS_MACOS", True)
        install_app_font(app)
        assert app.font().pixelSize() == FONT_SIZES_PX["body_size"]
        assert "QWidget { font-size: 14px; }" in stylesheet("light")
        assert "QWidget { font-size: 21px; }" in stylesheet("light", 150)

        monkeypatch.setattr(typography, "IS_MACOS", False)
        install_app_font(app)
        assert app.font().pointSizeF() == typography.BODY_POINT_SIZE
        assert "QWidget { font-size: 10.5pt; }" in stylesheet("light")
        assert "QWidget { font-size: 15.75pt; }" in stylesheet("light", 150)
    finally:
        app.setFont(previous_font)


def test_inter_and_text_spacing_across_windows_and_zoom(tmp_path):
    app = QApplication.instance() or QApplication([])
    previous_font = QFont(app.font())
    install_app_font(app)
    window = None
    chooser = None
    try:
        window = MainWindow(database_path=tmp_path / "typography.sqlite3")
        window.resize(900, 760)
        window.show()
        chooser = DatabaseSelectionWindow(lambda path: None, lambda: None,
                                          lambda path: None)
        app.processEvents()

        assert {widget.font().family() for widget in
                [*window.findChildren(QWidget), window, chooser]} == {FONT_FAMILY}
        assert QFontInfo(window.cash_tab.table.font()).pixelSize() == FONT_SIZES_PX["body_size"]
        assert _margins(window.cash_tab.layout()) == PAGE_MARGINS
        assert _margins(window.analytics_tab.layout()) == PAGE_MARGINS
        assert window.cash_tab.table.verticalHeader().defaultSectionSize() == TABLE_ROW_HEIGHT
        control_height = window.cash_tab.range_combo.height()

        window.set_zoom(150)
        window.tabs.setCurrentWidget(window.cash_tab)
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        assert QFontInfo(window.cash_tab.table.font()).pixelSize() == 21
        assert window.cash_tab.table.verticalHeader().defaultSectionSize() == round(TABLE_ROW_HEIGHT * 1.5)
        assert window.summary.height() >= window.summary.minimumSizeHint().height()
        assert (window.cash_tab.summary_content.height() >=
                window.cash_tab.summary_content.minimumSizeHint().height())
        assert all(row.height() >= row.minimumSizeHint().height()
                   for row in window.cash_tab.summary_content.findChildren(QWidget, "cashSummaryRow"))
        assert window.cash_tab.range_combo.height() >= control_height

        window.set_zoom(100)
        app.processEvents()
        assert window.cash_tab.range_combo.height() == control_height
    finally:
        if chooser is not None:
            chooser.close()
        if window is not None:
            window.close()
        app.setFont(previous_font)
