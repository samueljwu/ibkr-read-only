"""Shared color and spacing rules for the IBKR dashboard."""

from string import Template
import re

from PySide6.QtWidgets import QComboBox, QPushButton, QStyledItemDelegate

from app.views.typography import BODY_POINT_SIZE, FONT_SIZES_PX


COLORS = {
    "light": {
        "canvas": "#f4f6f8", "surface": "#ffffff", "surface_alt": "#f8fafb",
        "text": "#192c3d", "muted": "#647789", "line": "#dfe6eb",
        "accent": "#087da8", "accent_hover": "#076789", "accent_soft": "#e5f4fa",
        "selection": "#d9edf6", "selection_text": "#15374a",
        "selection_border": "#215f8f", "warning": "#985314",
        "scroll_thumb": "#a7b7c2", "scroll_thumb_hover": "#557d99",
        "sync_action": "#087da8", "sync_action_hover": "#076789",
        "sync_action_text": "#ffffff",
        "positive": "#137333", "negative": "#b3261e", "benchmark": "#7545ad",
        "checkbox_checked": "#087da8",
        "sync_green": "#167347", "sync_green_hover": "#0f5d39",
        "sync_red": "#b72b34", "sync_red_hover": "#91222a",
        "series": ["#087da8", "#54836e", "#b8794f", "#7a6aa8", "#b78b44", "#6285a0"],
        "unclassified": "#939da5", "zero": "#647789",
        "bar_positive": "#137333", "bar_negative": "#b3261e",
        "grid_neutral_text": "#30343b", "grid_default_text": "#1d2b3a",
        "grid_warning_text": "#9b4917",
        "disabled_surface": "#e8ecef", "disabled_text": "#87939d",
        "disabled_border": "#cfd7dd",
        "calendar_range": "#eef1f3", "calendar_selected": "#d9edf6",
        "calendar_selected_text": "#15374a",
    },
    "dark": {
        "canvas": "#191919", "surface": "#171719", "surface_alt": "#222225",
        "text": "#f0f0f2", "muted": "#aaaab0", "line": "#353539",
        "accent": "#d4d4d8", "accent_hover": "#f0f0f2", "accent_soft": "#303034",
        "selection": "#263d50", "selection_text": "#ffffff",
        "selection_border": "#67a9d3", "warning": "#ffb871",
        "scroll_thumb": "#646b73", "scroll_thumb_hover": "#81b4d4",
        "sync_action": "#67a9d3", "sync_action_hover": "#8cc3e2",
        "sync_action_text": "#0b0b0d",
        "positive": "#52c7a6", "negative": "#ff8b91", "benchmark": "#c4a3f4",
        "checkbox_checked": "#77777e",
        "sync_green": "#187a4d", "sync_green_hover": "#14643f",
        "sync_red": "#c23d45", "sync_red_hover": "#a92f36",
        "series": ["#8ac5dc", "#73b69b", "#dda878", "#ae9bd1", "#d4b66f", "#94afcf"],
        "unclassified": "#8b8b92", "zero": "#aaaab0",
        "bar_positive": "#52c7a6", "bar_negative": "#ff8b91",
        "grid_neutral_text": "#f0f0f2", "grid_default_text": "#f0f0f2",
        "grid_warning_text": "#ffb871",
        "disabled_surface": "#303034", "disabled_text": "#85858d",
        "disabled_border": "#4a4a50",
        "calendar_range": "#29292d", "calendar_selected": "#d4d4d8",
        "calendar_selected_text": "#0b0b0d",
    },
}

SUMMARY_BORDER_GRADIENTS = {
    "light": {
        "rainbow": ("#087da8", "#7a6aa8", "#b8794f", "#54836e"),
        "ocean": ("#086a98", "#188cb3", "#35aaa9", "#67b8a1"),
        "sunset": ("#bb5573", "#dc7559", "#dfa54d", "#9563a2"),
    },
    "dark": {
        "rainbow": ("#8ac5dc", "#ae9bd1", "#dda878", "#73b69b"),
        "ocean": ("#71bfe1", "#68c5df", "#7bd8cf", "#a3dcc5"),
        "sunset": ("#e996ad", "#efae88", "#e9cb83", "#c4a3d5"),
    },
}

SUMMARY_BORDER_SOLIDS = (
    ("Red", "#c74747"), ("Coral", "#df704d"), ("Amber", "#b87b31"),
    ("Lime", "#839c35"), ("Green", "#4d9a4f"), ("Emerald", "#278469"),
    ("Teal", "#2a9293"), ("Cyan", "#087da8"), ("Blue", "#397ebc"),
    ("Indigo", "#6556b2"), ("Violet", "#7954ad"), ("Orchid", "#a9569e"),
    ("Rose", "#b55379"), ("Taupe", "#7d6f62"), ("Slate", "#607b8d"),
    ("Charcoal", "#4a5660"), ("Silver", "#a8adb4"), ("Sand", "#cfbca1"),
)

TOOLBAR_ITEM_SPACING = 8
TOOLBAR_GROUP_SPACING = 16

PAGE_MARGINS = (16, 14, 16, 14)
PAGE_SPACING = 10
SUBPAGE_MARGINS = (0, 12, 0, 0)
CARD_MARGINS = (14, 10, 14, 10)
DIALOG_MARGINS = (22, 20, 22, 20)
DIALOG_SPACING = 12
SPLITTER_HANDLE = 8
CONTROL_HEIGHT = 34


def style_toolbar_dropdown(widget):
    """Give tab-level filter and menu controls the same compact shape."""
    widget.setObjectName("toolbarDropdown")
    set_zoom_height(widget, CONTROL_HEIGHT)
    if isinstance(widget, QPushButton) and widget.menu() is not None:
        menu = widget.menu()
        menu.aboutToShow.connect(lambda: _set_menu_open(widget, True))
        menu.aboutToHide.connect(lambda: _set_menu_open(widget, False))
    return style_combo_selection(widget)


def set_zoom_height(widget, height):
    """Keep fixed-height controls large enough for their scaled text."""
    widget.setProperty("zoom_base_height", height)
    widget.setFixedHeight(height)
    return widget


def _set_menu_open(widget, is_open):
    widget.setProperty("menuOpen", is_open)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def style_combo_selection(widget):
    """Use Qt's stylesheet-aware renderer for combo popup choices."""
    if isinstance(widget, QComboBox):
        widget.setItemDelegate(QStyledItemDelegate(widget))
    return widget


def apply_widget_theme(widget, theme, zoom_percent=100):
    """Apply the shared stylesheet to a standalone window."""
    widget.setStyleSheet(stylesheet(theme, zoom_percent))


STYLESHEET = Template("""
QWidget { font-size: ${body_point_size}pt; }
QMainWindow, QWidget#central, QWidget#settingsPage, QWidget#helpPage, QWidget#calcsPage,
QWidget#analyticsContent, QWidget#analyticsPage, QWidget#analyticsSection,
QScrollArea#analyticsScroller, QDialog, QScrollArea, QTabWidget::pane {
    background: ${canvas}; color: ${text};
}
QTabWidget#analyticsSections, QTabWidget#analyticsSections QTabBar {
    background: ${canvas}; color: ${text};
}
QLabel { color: ${text}; }
QLabel#settingsInfo { color: ${muted}; font-size: ${caption_size}px; }
QLabel#helpHeading { color: ${text}; font-size: ${title_size}px; font-weight: 600; }
QLabel#helpSection { color: ${text}; font-size: ${section_size}px; font-weight: 600; }
QLabel#helpFieldHeading { color: ${text}; font-weight: 600; }
QLabel#helpKey { color: ${accent}; font-weight: 600; }
QToolButton#helpDisclosure {
    color: ${text}; background: transparent; border: 0;
    padding: 4px 2px;
}
QToolButton#helpDisclosure:hover { color: ${accent}; }
QFrame#helpDivider { background: ${line}; border: 0; }
QListWidget#helpNavigation, QListWidget#portfolioList {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; outline: 0;
}
QListWidget#helpNavigation::item, QListWidget#portfolioList::item {
    padding: 9px 10px; margin: 2px; border-radius: 4px;
}
QListWidget#mainNavigation {
    background: ${surface}; color: ${muted}; border: 1px solid ${line};
    border-radius: 7px; outline: 0; padding: 8px 4px;
}
QListWidget#mainNavigation::item {
    padding: 11px 12px; margin: 2px 0; border-radius: 4px;
    border: 2px solid transparent;
}
QListWidget#mainNavigation::item:hover { background: ${surface_alt}; color: ${text}; }
QListWidget#mainNavigation::item:selected {
    background: ${selection}; color: ${selection_text};
    border: 2px solid ${selection_border}; font-weight: 600;
}
QListWidget#helpNavigation::item:selected, QListWidget#portfolioList::item:selected {
    background: ${selection}; color: ${selection_text};
}
QListWidget#helpNavigation::item:hover:!selected,
QListWidget#portfolioList::item:hover:!selected { background: ${accent_soft}; }
QListWidget#portfolioList::item:disabled { color: ${disabled_text}; }
QLabel#heading { color: ${text}; font-size: ${heading_size}px; font-weight: 600; }
QLabel#sectionTitle { color: ${text}; font-size: ${section_size}px; font-weight: 600; }
QLabel#fieldLabel { color: ${muted}; font-size: ${caption_size}px; }
QLabel#metricLabel, QLabel#hint { color: ${muted}; font-size: ${caption_size}px; }
QWidget#card { background: ${surface}; border: 1px solid ${line}; border-radius: 8px; }
QWidget#tutorialCard { background: ${surface}; border: 2px solid ${accent}; border-radius: 8px; }
QWidget#tutorialCard QLabel { color: ${text}; border: none; }
QWidget#tutorialCard QLabel#tutorialProgress { color: ${muted}; }
QWidget#tutorialCard QLabel#tutorialTitle { font-size: ${title_size}px; font-weight: 600; }
QLabel#chartHoverCard {
    background: ${surface}; color: ${text}; border: 1px solid ${accent};
    border-radius: 6px; padding: 8px 10px; font-size: ${caption_size}px;
}
QLabel#calendarCell {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 5px;
}
QLabel#calendarCell[calendarState="positive"] { background: ${accent_soft}; color: ${positive}; }
QLabel#calendarCell[calendarState="negative"] { background: ${accent_soft}; color: ${negative}; }
QLabel#calendarCell[calendarState="inactive"] { background: ${canvas}; color: ${muted}; }
QLabel#calendarCell[calendarState="empty"] { background: transparent; border: 0; }
QWidget#chartHoverLine { background: ${accent}; }
QPlainTextEdit {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 4px; padding: 6px;
}
QWidget#historyImportForm { background: ${surface}; border: 1px solid ${line}; border-radius: 8px; }
QLabel#historyFieldLabel { color: ${muted}; font-size: ${caption_size}px; }
QLabel#historyProgressLabel { color: ${text}; font-size: ${caption_size}px; }
QToolButton#historyHelpButton, QToolButton#historyTextAction, QToolButton#returnNotesButton {
    color: ${accent}; border: 0; padding: 2px 0;
}
QToolButton#historyHelpButton:hover, QToolButton#historyTextAction:hover,
QToolButton#returnNotesButton:hover { color: ${accent_hover}; }
QToolButton#calendarDateButton {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; padding: 4px 6px;
}
QToolButton#calendarDateButton:hover {
    background: ${accent_soft}; border-color: ${selection_border};
}
QToolButton#calendarDateButton:focus, QToolButton#calendarDateButton[calendarOpen="true"] {
    background: ${accent_soft}; border: 2px solid ${selection_border}; padding: 3px 5px;
}
QDateEdit::drop-down {
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 28px; background: ${surface_alt}; border-left: 1px solid ${line};
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
}
QDateEdit::drop-down:hover { background: ${accent_soft}; }
QMenu#datePickerMenu { background: ${surface}; border: 1px solid ${line}; border-radius: 8px; padding: 4px; }
QCalendarWidget#datePickerCalendar { background: ${surface}; color: ${text}; border: 0; }
QCalendarWidget#datePickerCalendar QWidget#qt_calendar_navigationbar {
    background: ${surface}; border: 0; border-bottom: 1px solid ${line}; min-height: 38px;
}
QCalendarWidget#datePickerCalendar QToolButton {
    background: transparent; color: ${text}; border: 0; border-radius: 5px;
    padding: 4px 6px; margin: 3px; font-weight: 600;
}
QCalendarWidget#datePickerCalendar QToolButton:hover { background: ${surface_alt}; }
QCalendarWidget#datePickerCalendar QToolButton::menu-indicator { image: none; }
QCalendarWidget#datePickerCalendar QSpinBox#qt_calendar_yearedit {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 4px; selection-background-color: ${selection};
    selection-color: ${selection_text};
}
QCalendarWidget#datePickerCalendar QTableView {
    background: ${surface}; color: ${text}; border: 0; outline: 0;
    selection-background-color: ${surface}; selection-color: ${text};
}
QCalendarWidget#datePickerCalendar QTableView::item { border: 0; padding: 0; }
QCalendarWidget#datePickerCalendar QTableView::item:selected,
QCalendarWidget#datePickerCalendar QTableView::item:focus {
    background: ${surface}; color: ${text}; border: 0;
}
QPlainTextEdit#historyImportDetails {
    background: ${surface}; color: ${text}; border: 1px solid ${line}; border-radius: 4px;
    padding: 6px;
}
QLabel#metricValue { color: ${text}; font-size: ${metric_size}px; font-weight: 600; }
QWidget#metricSummaryRow[divider="true"] { border-bottom: 1px solid ${line}; }
QWidget#analyticsSummary { background: ${surface}; border: 1px solid ${line}; border-radius: 8px; }
QLabel#metricLabel[analyticsSelected="true"] { color: ${accent}; }
QLabel#metricLabel[metricSelected="true"] { color: ${accent}; }
QLabel#metricValue[analyticsSelected="true"] { color: ${accent}; }
QLabel#metricValue[pnlTrend="positive"] { color: ${positive}; }
QLabel#metricValue[pnlTrend="negative"] { color: ${negative}; }
QLabel#lotMatchPnl[pnlTrend="positive"] { color: ${positive}; }
QLabel#lotMatchPnl[pnlTrend="negative"] { color: ${negative}; }
QLabel#notice { color: ${warning}; font-size: ${caption_size}px; }
QLabel#syncProgressLabel, QLabel#tradeSyncStatus { color: ${text}; }
QLabel#progressPercent { color: ${muted}; font-size: ${caption_size}px; font-weight: 600; }
QProgressBar {
    background: ${surface_alt}; color: ${text}; border: 1px solid ${line};
    border-radius: 8px; text-align: center; font-weight: 600;
}
QProgressBar::chunk {
    background: ${selection_border}; width: 9px; margin: 2px; border-radius: 3px;
}
QProgressBar#compactProgress { background: transparent; border: 0; }
QWidget#calcsCashPanel { background: ${surface_alt}; border: 1px solid ${line}; border-radius: 8px; }
QLabel#calcsCashTitle { font-weight: 600; font-size: ${section_size}px; }
QLabel#calcsFieldLabel { color: ${muted}; font-size: ${caption_size}px; }
QLabel#calcsCaseResult { color: ${muted}; font-size: ${caption_size}px; }
QPushButton {
    background: ${accent}; color: ${surface}; border: 1px solid ${accent};
    border-radius: 6px; padding: 5px 6px; font-weight: 600; min-height: 19px;
}
QPushButton:hover { background: ${accent_hover}; border-color: ${accent_hover}; }
QPushButton#syncAction {
    background: ${sync_action}; color: ${sync_action_text};
    border: 1px solid ${sync_action}; border-radius: 6px; padding: 5px 12px;
}
QPushButton#syncAction:hover {
    background: ${sync_action_hover}; border-color: ${sync_action_hover};
}
QPushButton#secondary, QPushButton#themeToggle {
    background: ${surface}; color: ${text}; border-color: ${line};
}
QPushButton#secondary:hover, QPushButton#themeToggle:hover {
    background: ${accent_soft}; border-color: ${line};
}
QPushButton#secondary:checked {
    background: ${selection}; color: ${selection_text};
    border: 2px solid ${selection_border}; padding: 4px 5px;
}
QPushButton#danger {
    background: ${surface}; color: ${negative}; border-color: ${negative};
}
QPushButton#danger:hover { background: ${accent_soft}; border-color: ${negative}; }
QPushButton#link, QToolButton#link {
    background: transparent; color: ${accent}; border: 0; padding: 2px 0;
    font-weight: 600; min-height: 0;
}
QPushButton#link:hover, QToolButton#link:hover { color: ${accent_hover}; }
QComboBox#toolbarDropdown, QPushButton#toolbarDropdown {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; padding: 5px 8px; font-weight: 400;
}
QComboBox#toolbarDropdown:hover, QPushButton#toolbarDropdown:hover {
    background: ${accent_soft}; border-color: ${line};
}
QComboBox#toolbarDropdown:focus, QPushButton#toolbarDropdown:focus {
    background: ${accent_soft}; border: 1px solid ${line}; padding: 5px 8px;
}
QComboBox#toolbarDropdown:on, QPushButton#toolbarDropdown:pressed,
QPushButton#toolbarDropdown[menuOpen="true"] {
    background: ${accent_soft}; border: 2px solid ${selection_border}; padding: 4px 7px;
}
QPushButton:focus, QPushButton:pressed,
QPushButton#secondary:focus, QPushButton#secondary:pressed,
QPushButton#themeToggle:focus, QPushButton#themeToggle:pressed,
QPushButton#danger:focus, QPushButton#danger:pressed,
QPushButton#syncAction:focus, QPushButton#syncAction:pressed {
    border: 2px solid ${selection_border}; padding: 4px 5px;
}
QPushButton:disabled, QPushButton#secondary:disabled, QPushButton#themeToggle:disabled,
QPushButton#danger:disabled, QPushButton#toolbarDropdown:disabled, QPushButton#syncAction:disabled,
QToolButton:disabled, QToolButton#calendarDateButton:disabled {
    background: ${disabled_surface}; color: ${disabled_text};
    border: 1px solid ${disabled_border};
}
QPushButton#link:disabled, QToolButton#link:disabled,
QToolButton#historyHelpButton:disabled, QToolButton#historyTextAction:disabled,
QToolButton#returnNotesButton:disabled {
    background: transparent; color: ${disabled_text}; border: 0;
}
QWidget#cashSummaryPanel { background: ${canvas}; }
QScrollArea#cashBreakdownScroll {
    background: ${surface}; border: 1px solid ${line};
}
QWidget#cashBreakdownContent { background: ${surface}; }
QLabel#cashCurrencyLabel {
    color: ${muted}; font-weight: 600;
    padding: 4px 0 6px 0;
}
QWidget#cashSummaryRow { border-bottom: 1px solid ${line}; }
QWidget#cashTotalRow { border-top: 1px solid ${line}; }
QLabel#cashItemLabel, QLabel#cashAmount { color: ${text}; }
QLabel#cashTotalLabel, QLabel#cashTotalAmount { color: ${text}; font-weight: 600; }
QSplitter::handle:horizontal { background: ${line}; }
QSplitter::handle:horizontal:hover { background: ${accent}; }
QSplitter::handle:vertical { background: ${line}; }
QSplitter::handle:vertical:hover { background: ${accent}; }
QCheckBox { color: ${text}; spacing: 6px; }
QCheckBox::indicator {
    width: 14px; height: 14px; border: 1px solid ${muted};
    border-radius: 3px; background: ${surface};
}
QCheckBox::indicator:checked { background: ${checkbox_checked}; border-color: ${checkbox_checked}; }
QLineEdit, QDateEdit, QDoubleSpinBox {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; padding: 5px;
}
QComboBox {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; padding: 5px;
}
QLineEdit:focus, QDateEdit:focus, QDoubleSpinBox:focus { border-color: ${accent}; }
QDateEdit:focus { border: 2px solid ${selection_border}; padding: 4px; }
QComboBox:focus {
    background: ${accent_soft}; border: 1px solid ${line}; padding: 5px;
}
QComboBox:on {
    border: 2px solid ${selection_border}; padding: 4px;
}
QComboBox#metricDropdown {
    background: transparent; color: ${muted}; border: 1px solid transparent;
    border-radius: 4px; padding: 0 4px; min-height: 0;
    font-size: ${caption_size}px; font-weight: 400;
}
QComboBox#metricDropdown:hover, QComboBox#metricDropdown:focus, QComboBox#metricDropdown:on {
    background: ${accent_soft}; border: 1px solid ${line}; padding: 0 4px;
}
QPushButton#metricButton {
    background: transparent; border: 2px solid transparent; border-radius: 4px;
    padding: 0; min-height: 0;
}
QPushButton#metricButton:checked {
    background: ${selection}; border: 2px solid ${selection_border};
}
QPushButton#metricButton:focus, QPushButton#metricButton:pressed {
    border: 2px solid ${selection_border}; padding: 0;
}
QPushButton#metricButton QLabel { background: transparent; padding: 0; }
QToolButton#metricInfoButton {
    background: transparent; color: ${muted}; border: 1px solid ${line};
    border-radius: 10px; padding: 0; font-size: 12px; font-weight: 600;
}
QToolButton#metricInfoButton:hover, QToolButton#metricInfoButton:focus {
    background: ${accent_soft}; color: ${accent}; border-color: ${selection_border};
}
QComboBox QAbstractItemView {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 8px; outline: 0;
    selection-background-color: ${selection}; selection-color: ${selection_text};
}
QComboBox QListView::item {
    border: 2px solid transparent; border-radius: 4px; padding: 4px 8px;
}
QComboBox QListView::item:selected {
    background: ${selection}; color: ${selection_text};
    border: 2px solid ${selection_border};
}
QDoubleSpinBox#calcsTargetInput, QComboBox#calcsTargetInput {
    background: ${accent_soft}; color: ${text}; border: 1px solid ${accent};
    border-radius: 2px; padding: 1px 4px;
}
QComboBox#calcsTargetInput[activeCell="true"],
QDoubleSpinBox#calcsTargetInput[activeCell="true"] {
    border: 1px solid ${selection_border};
}
QComboBox#calcsTargetInput:focus, QComboBox#calcsTargetInput:on,
QDoubleSpinBox#calcsTargetInput:focus {
    border: 2px solid ${selection_border}; border-radius: 4px; padding: 0 3px;
}
QMenuBar { background: ${canvas}; color: ${text}; }
QMenuBar::item:selected { background: ${accent_soft}; color: ${text}; }
QMenu { background: ${surface}; color: ${text}; border: 1px solid ${line}; }
QMenu::item:selected { background: ${accent_soft}; color: ${text}; }
QMenu#columnsMenu {
    border-radius: 8px; padding: 6px;
}
QMenu#columnsMenu::item {
    border: 2px solid transparent; border-radius: 6px; padding: 6px 32px 6px 24px;
}
QMenu#columnsMenu::item:selected {
    background: ${selection}; color: ${selection_text}; border: 2px solid ${selection_border};
}
QMenu#columnsMenu::separator { height: 1px; background: ${line}; margin: 5px 10px; }
QTableView {
    background: ${surface}; color: ${text}; alternate-background-color: ${surface};
    gridline-color: ${line}; selection-background-color: ${selection};
    selection-color: ${selection_text}; border: 1px solid ${line};
    border-radius: 8px; outline: 0;
}
QTableView::item { border-bottom: 1px solid ${line}; padding: 0px 4px; }
QTableView::item:selected { background: ${selection}; color: ${selection_text}; }
QTableView::item:hover:!selected { background: ${surface_alt}; }
QTableView::item:focus {
    border: 2px solid ${selection_border}; border-radius: 4px; outline: 0;
}
QHeaderView { background: ${surface_alt}; }
QHeaderView::section {
    background: ${surface_alt}; color: ${muted}; padding: 9px 10px;
    border: 0; border-bottom: 2px solid ${line}; font-weight: 600;
}
QHeaderView::section:horizontal { color: ${text}; }
QHeaderView::section:horizontal:hover { background: ${accent_soft}; color: ${selection_border}; }
QHeaderView::section:horizontal:pressed {
    background: ${selection}; color: ${selection_text}; border-bottom-color: ${selection_border};
}
QHeaderView::section:vertical {
    padding: 0 8px; border-right: 1px solid ${line};
    border-bottom: 1px solid ${line};
}
QTableCornerButton::section { background: ${surface_alt}; border: 0; border-bottom: 1px solid ${line}; }
QScrollBar:vertical {
    background: transparent; border: 0; width: 14px; margin: 0;
}
QScrollBar::handle:vertical {
    background: ${scroll_thumb}; border-radius: 4px;
    min-height: 32px; margin: 2px 3px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {
    background: ${scroll_thumb_hover}; margin: 2px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; border: 0; height: 14px; margin: 0; }
QScrollBar::handle:horizontal {
    background: ${scroll_thumb}; border-radius: 4px;
    min-width: 32px; margin: 3px 2px;
}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {
    background: ${scroll_thumb_hover}; margin: 2px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QTabWidget::pane { border: 0; border-top: 1px solid ${line}; }
QTabWidget#mainPages::pane { border: 0; }
QTabWidget::tab-bar { subcontrol-position: left; }
QTabBar { alignment: left; }
QTabBar::tab {
    background: ${canvas}; color: ${muted}; padding: 11px 18px;
    border: 0; border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: ${text}; border-bottom: 2px solid ${accent}; font-weight: 600; }
QTabBar::tab:hover { color: ${accent}; }
QStatusBar { background: ${canvas}; color: ${text}; border-top: 1px solid ${line}; }
QStatusBar::item { border: 0; }
QToolTip { background: ${surface}; color: ${text}; border: 1px solid ${line}; }
""")


def stylesheet(theme, zoom_percent=100):
    point_size = BODY_POINT_SIZE * zoom_percent / 100
    style = STYLESHEET.substitute(COLORS[theme] | FONT_SIZES_PX |
                                 {"body_point_size": f"{point_size:g}"})
    if zoom_percent == 100:
        return style
    return re.sub(r"(font-size:\s*)(\d+)px",
                  lambda match: f"{match[1]}{max(1, round(int(match[2]) * zoom_percent / 100))}px",
                  style)
