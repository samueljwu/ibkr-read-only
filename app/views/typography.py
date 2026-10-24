"""Shared application font for every Qt window and dialog."""

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


FONT_FAMILY = "Inter"
# Qt uses 96 logical DPI for the existing Windows layout: 14 px = 10.5 pt.
FONT_SIZES_PX = {
    "caption_size": 13,
    "body_size": 14,
    "section_size": 14,
    "title_size": 17,
    "heading_size": 21,
    "metric_size": 19,
}
BODY_POINT_SIZE = FONT_SIZES_PX["body_size"] * 0.75
FONT_FILES = ("Inter-Regular.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf")


def install_app_font(app):
    """Register bundled faces before constructing any widgets."""
    if not app.property("inter_font_registered"):
        directory = Path(__file__).resolve().parents[1] / "assets" / "fonts"
        for filename in FONT_FILES:
            font_id = QFontDatabase.addApplicationFont(str(directory / filename))
            if font_id < 0 or FONT_FAMILY not in QFontDatabase.applicationFontFamilies(font_id):
                raise RuntimeError(f"Could not load bundled font: {filename}")
        app.setProperty("inter_font_registered", True)
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(BODY_POINT_SIZE)
    app.setFont(font)
