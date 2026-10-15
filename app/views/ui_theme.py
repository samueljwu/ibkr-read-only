"""Shared color and spacing rules for the portfolio monitor."""

from string import Template


COLORS = {
    "light": {
        "canvas": "#f4f6f8", "surface": "#ffffff", "surface_alt": "#f8fafb",
        "text": "#192c3d", "muted": "#647789", "line": "#dfe6eb",
        "accent": "#087da8", "accent_hover": "#076789", "accent_soft": "#e5f4fa",
        "selection": "#d9edf6", "selection_text": "#15374a", "warning": "#985314",
        "positive": "#137333", "negative": "#b3261e", "benchmark": "#7545ad",
        "checkbox_checked": "#087da8",
        "sync_green": "#167347", "sync_green_hover": "#0f5d39",
        "sync_red": "#b72b34", "sync_red_hover": "#91222a",
    },
    "dark": {
        "canvas": "#0b0b0d", "surface": "#171719", "surface_alt": "#222225",
        "text": "#f0f0f2", "muted": "#aaaab0", "line": "#353539",
        "accent": "#d4d4d8", "accent_hover": "#f0f0f2", "accent_soft": "#303034",
        "selection": "#3a3a40", "selection_text": "#ffffff", "warning": "#ffb871",
        "positive": "#52c7a6", "negative": "#ff8b91", "benchmark": "#c4a3f4",
        "checkbox_checked": "#77777e",
        "sync_green": "#187a4d", "sync_green_hover": "#14643f",
        "sync_red": "#c23d45", "sync_red_hover": "#a92f36",
    },
}

TOOLBAR_ITEM_SPACING = 8
TOOLBAR_GROUP_SPACING = 16


def style_toolbar_dropdown(widget):
    """Give tab-level filter and menu controls the same compact shape."""
    widget.setObjectName("toolbarDropdown")
    widget.setFixedHeight(32)
    return widget


STYLESHEET = Template("""
QMainWindow, QWidget#central, QWidget#settingsPage, QWidget#helpPage, QWidget#calcsPage,
QWidget#analyticsContent, QWidget#analyticsPage, QWidget#analyticsSection,
QScrollArea#analyticsScroller, QDialog, QScrollArea, QTabWidget::pane {
    background: ${canvas}; color: ${text};
}
QTabWidget#analyticsSections, QTabWidget#analyticsSections QTabBar {
    background: ${canvas}; color: ${text};
}
QLabel { color: ${text}; }
QLabel#settingsInfo { color: ${muted}; font-size: 12px; }
QLabel#helpHeading { color: ${text}; font-size: 18px; font-weight: 600; }
QLabel#helpSection { color: ${text}; font-size: 14px; font-weight: 600; }
QLabel#helpKey { color: ${accent}; font-weight: 600; }
QFrame#helpMockup { background: ${surface}; border: 1px solid ${line}; border-radius: 5px; }
QLabel#helpMockTitle { color: ${text}; font-size: 12px; font-weight: 600; }
QLabel#helpMockButton, QLabel#helpMockControl {
    background: ${surface_alt}; color: ${text}; border: 1px solid ${line};
    border-radius: 3px; padding: 3px 5px; font-size: 11px;
}
QLabel#helpMockTab, QLabel#helpMockTabSelected {
    color: ${muted}; padding: 3px 5px; font-size: 11px;
}
QLabel#helpMockTabSelected { color: ${text}; border-bottom: 2px solid ${accent}; font-weight: 600; }
QLabel#helpMockStatus, QLabel#helpMockCell { color: ${text}; font-size: 11px; }
QLabel#helpMockHeader { color: ${muted}; font-size: 11px; font-weight: 600; }
QLabel#helpMockCaption { color: ${muted}; font-size: 11px; }
QListWidget#helpNavigation {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 6px; outline: 0;
}
QListWidget#helpNavigation::item { padding: 9px 10px; margin: 2px; border-radius: 4px; }
QListWidget#helpNavigation::item:selected { background: ${selection}; color: ${selection_text}; }
QListWidget#helpNavigation::item:hover:!selected { background: ${accent_soft}; }
QLabel#heading { color: ${text}; font-size: 19px; font-weight: 650; }
QLabel#metricLabel, QLabel#hint { color: ${muted}; font-size: 12px; }
QWidget#historyImportForm { background: ${surface}; border: 1px solid ${line}; border-radius: 6px; }
QLabel#historyFieldLabel { color: ${muted}; font-size: 12px; }
QLabel#historyProgressLabel { color: ${text}; font-size: 12px; }
QToolButton#historyHelpButton, QToolButton#historyTextAction {
    color: ${accent}; border: 0; padding: 2px 0;
}
QToolButton#historyHelpButton:hover, QToolButton#historyTextAction:hover { color: ${accent_hover}; }
QToolButton#historyCalendarButton {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 4px; padding: 4px 6px;
}
QToolButton#historyCalendarButton:hover { background: ${accent_soft}; border-color: ${accent}; }
QPlainTextEdit#historyImportDetails {
    background: ${surface}; color: ${text}; border: 1px solid ${line}; border-radius: 4px;
    padding: 6px;
}
QLabel#metricValue { color: ${text}; font-size: 19px; font-weight: 600; }
QLabel#exposureNetSummary { color: ${text}; font-size: 13px; font-weight: 600; }
QWidget#analyticsSummary { background: ${surface}; border: 1px solid ${line}; border-radius: 6px; }
QLabel#metricLabel[analyticsSelected="true"] { color: ${accent}; }
QLabel#metricValue[analyticsSelected="true"] { color: ${accent}; font-size: 21px; }
QLabel#metricValue[pnlTrend="positive"] { color: ${positive}; }
QLabel#metricValue[pnlTrend="negative"] { color: ${negative}; }
QLabel#notice { color: ${warning}; font-size: 12px; }
QLabel#syncProgressLabel, QLabel#tradeSyncStatus { color: ${text}; }
QProgressBar#syncProgressBar {
    background: ${surface_alt}; border: 1px solid ${line}; border-radius: 4px;
}
QProgressBar#syncProgressBar::chunk { background: ${accent}; border-radius: 3px; }
QWidget#summary { background: ${surface}; border: 1px solid ${line}; border-radius: 6px; }
QWidget#calcsCashPanel { background: ${surface_alt}; border: 1px solid ${line}; border-radius: 6px; }
QLabel#calcsCashTitle { font-weight: 600; font-size: 14px; }
QLabel#calcsFieldLabel { color: ${muted}; font-size: 12px; }
QLabel#calcsCaseResult { color: ${muted}; font-size: 12px; }
QPushButton {
    background: ${accent}; color: ${surface}; border: 1px solid ${accent};
    border-radius: 4px; padding: 5px 6px; font-weight: 600; min-height: 19px;
}
QPushButton:hover { background: ${accent_hover}; border-color: ${accent_hover}; }
QPushButton#secondary, QPushButton#themeToggle {
    background: ${surface}; color: ${text}; border-color: ${line};
}
QPushButton#secondary:hover, QPushButton#themeToggle:hover {
    background: ${accent_soft}; border-color: ${accent};
}
QPushButton#rebalanceSyncButton {
    background: ${surface}; color: ${text}; border-color: ${line};
}
QPushButton#rebalanceSyncButton:hover {
    background: ${accent_soft}; border-color: ${accent};
}
QPushButton#rebalanceSyncButton:focus { border: 2px solid ${accent}; }
QComboBox#toolbarDropdown, QPushButton#toolbarDropdown {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 4px; padding: 5px 8px; font-weight: 600;
}
QComboBox#toolbarDropdown:hover, QPushButton#toolbarDropdown:hover {
    background: ${accent_soft}; border-color: ${accent};
}
QComboBox#toolbarDropdown:focus, QPushButton#toolbarDropdown:focus {
    border-color: ${accent};
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
QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox {
    background: ${surface}; color: ${text}; border: 1px solid ${line};
    border-radius: 4px; padding: 5px;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QDoubleSpinBox:focus {
    border: 1px solid ${accent};
}
QDoubleSpinBox#calcsTargetInput, QComboBox#calcsTargetInput {
    background: ${accent_soft}; color: ${text}; border: 1px solid ${accent};
    border-radius: 2px; padding: 1px 4px;
}
QDoubleSpinBox#calcsTargetInput:focus, QComboBox#calcsTargetInput:focus { border: 1px solid ${accent}; }
QMenuBar { background: ${canvas}; color: ${text}; }
QMenuBar::item:selected { background: ${accent_soft}; color: ${text}; }
QMenu { background: ${surface}; color: ${text}; border: 1px solid ${line}; }
QMenu::item:selected { background: ${accent_soft}; color: ${text}; }
QTableView {
    background: ${surface}; color: ${text}; alternate-background-color: ${surface};
    gridline-color: ${line}; selection-background-color: ${selection};
    selection-color: ${selection_text}; border: 1px solid ${line};
}
QTableView::item { border-bottom: 1px solid ${line}; padding: 0px 4px; }
QTableView::item:selected { background: ${selection}; color: ${selection_text}; }
QTableView::item:focus { border: 1px solid ${accent}; }
QHeaderView { background: ${surface_alt}; }
QHeaderView::section {
    background: ${surface_alt}; color: ${muted}; padding: 6px;
    border: 0; border-bottom: 1px solid ${line}; font-weight: 600;
}
QHeaderView::section:vertical { border-right: 1px solid ${line}; }
QTableCornerButton::section { background: ${surface_alt}; border: 0; border-bottom: 1px solid ${line}; }
QScrollBar:vertical {
    background: ${canvas}; border: 0; width: 16px; margin: 0 0 0 6px;
}
QScrollBar::handle:vertical {
    background: ${line}; border-radius: 5px; min-height: 28px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: ${canvas}; border: 0; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: ${line}; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QTabWidget::pane { border: 0; border-top: 1px solid ${line}; }
QTabBar::tab {
    background: ${canvas}; color: ${muted}; padding: 9px 18px;
    border: 0; border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: ${text}; border-bottom: 2px solid ${accent}; }
QTabBar::tab:hover { color: ${accent}; }
QStatusBar { background: ${canvas}; color: ${text}; border-top: 1px solid ${line}; }
QToolTip { background: ${surface}; color: ${text}; border: 1px solid ${line}; }
""")


def stylesheet(theme):
    return STYLESHEET.substitute(COLORS[theme])
