"""Dated Flex import controls and worker progress presentation."""

from datetime import date, timedelta

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (QCalendarWidget, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from app.platform.history_status import format_history_details, format_imported_periods
from app.flex.flex_import import HISTORY_YEARS


class HistoryDateInput(QWidget):
    """ISO date text entry with a calendar picker and QDate-compatible accessors."""

    dateChanged = Signal(QDate)

    def __init__(self, selected):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.line_edit = QLineEdit()
        self.line_edit.setInputMask("0000-00-00;_")
        self.line_edit.setText(selected.toString("yyyy-MM-dd"))
        self.line_edit.setMinimumWidth(136)
        self.line_edit.setToolTip("Type a date as YYYY-MM-DD.")
        self.setFocusProxy(self.line_edit)
        row.addWidget(self.line_edit)
        self.calendar_button = QToolButton()
        self.calendar_button.setObjectName("historyCalendarButton")
        self.calendar_button.setArrowType(Qt.DownArrow)
        self.calendar_button.setFixedWidth(28)
        self.calendar_button.setToolTip("Choose a date from the calendar")
        self.calendar_button.setFocusPolicy(Qt.NoFocus)
        self.calendar_button.clicked.connect(self._show_calendar)
        row.addWidget(self.calendar_button)
        self._maximum = QDate.currentDate().addDays(-1)
        self.line_edit.editingFinished.connect(self._edited)

    def date(self):
        return QDate.fromString(self.line_edit.text(), "yyyy-MM-dd")

    def setDate(self, selected):
        if selected.isValid():
            self.line_edit.setText(selected.toString("yyyy-MM-dd"))
            self.dateChanged.emit(selected)

    def setMaximumDate(self, maximum):
        self._maximum = maximum

    def _edited(self):
        selected = self.date()
        if selected.isValid():
            self.dateChanged.emit(selected)

    def _show_calendar(self):
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        calendar = QCalendarWidget(menu)
        calendar.setMaximumDate(self._maximum)
        selected = self.date()
        if selected.isValid() and selected <= self._maximum:
            calendar.setSelectedDate(selected)
        action = QWidgetAction(menu)
        action.setDefaultWidget(calendar)
        menu.addAction(action)
        calendar.clicked.connect(lambda chosen: (self.setDate(chosen), menu.close()))
        menu.popup(self.calendar_button.mapToGlobal(self.calendar_button.rect().bottomLeft()))


class HistoryImportDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self._pause_requested = False
        self.setWindowTitle("Import Data — IBKR Flex history")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QLabel("Import IBKR history")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        intro = QLabel("Choose reports and completed dates.")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form_panel = QWidget()
        form_panel.setObjectName("historyImportForm")
        form = QGridLayout(form_panel)
        form.setContentsMargins(14, 12, 14, 12)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(5)
        self.source = QComboBox()
        for label, value in (("All reports", "all"), ("Trades", "trades"),
                             ("Analytics", "analytics"), ("Cash", "cash")):
            self.source.addItem(label, value)
        yesterday = date.today() - timedelta(days=1)
        first_day = yesterday - timedelta(days=29)
        self.from_date = HistoryDateInput(QDate(first_day.year, first_day.month, first_day.day))
        self.to_date = HistoryDateInput(QDate(yesterday.year, yesterday.month, yesterday.day))
        for widget in (self.from_date, self.to_date):
            widget.setMaximumDate(QDate(yesterday.year, yesterday.month, yesterday.day))
        for column, caption, widget in ((0, "Source", self.source),
                                        (1, "From", self.from_date),
                                        (2, "To", self.to_date)):
            label = QLabel(caption)
            label.setObjectName("historyFieldLabel")
            label.setBuddy(widget)
            form.addWidget(label, 0, column)
            form.addWidget(widget, 1, column)
        form.setColumnStretch(0, 2)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(2, 1)
        layout.addWidget(form_panel)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.import_button = QPushButton("Import history")
        self.import_button.clicked.connect(parent.start_history_import)
        actions.addWidget(self.import_button)
        self.older_import_button = QToolButton()
        self.older_import_button.setObjectName("historyTextAction")
        self.older_import_button.setToolTip(
            "Select the PortfolioAnalyst and Activity Statement CSVs together; order does not matter.")
        self.older_import_button.clicked.connect(parent.start_legacy_history_import)
        self.pause_button = QPushButton("Pause import")
        self.pause_button.setObjectName("secondary")
        self.pause_button.clicked.connect(parent.pause_history_import)
        self.pause_button.setEnabled(False)
        self.pause_button.hide()
        actions.addWidget(self.pause_button)
        actions.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setObjectName("secondary")
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        layout.addWidget(self.older_import_button, alignment=Qt.AlignLeft)
        self.refresh_flex_limit()
        self.help_button = QToolButton()
        self.help_button.setObjectName("historyHelpButton")
        self.help_button.setText("How imports work")
        self.help_button.setCheckable(True)
        self.help_button.setArrowType(Qt.RightArrow)
        self.help_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.help_button, alignment=Qt.AlignLeft)
        self.help_text = QLabel(
            "Uses the account, query IDs, and saved Flex token in Settings. "
            "IBKR may have gaps within the available range; report validation determines coverage. "
            "Identical data is skipped; "
            "verified corrections update existing data. Cash combines adjacent saved periods "
            "into checks of up to 365 days; boundary periods may extend beyond these dates. "
            "Changed reports create local database backups.")
        self.help_text.setObjectName("hint")
        self.help_text.setWordWrap(True)
        self.help_text.hide()
        layout.addWidget(self.help_text)
        self.help_button.toggled.connect(self._toggle_help)
        self.coverage_button = QToolButton()
        self.coverage_button.setObjectName("historyHelpButton")
        self.coverage_button.setText("Imported periods (all runs)")
        self.coverage_button.setCheckable(True)
        self.coverage_button.setArrowType(Qt.RightArrow)
        self.coverage_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.coverage_button, alignment=Qt.AlignLeft)
        self.coverage_button.toggled.connect(self._toggle_coverage)
        self._has_older_history = False
        self.imported_periods_label = QLabel()
        self.imported_periods_label.setObjectName("hint")
        self.imported_periods_label.setTextFormat(Qt.PlainText)
        self.imported_periods_label.setWordWrap(True)
        self.imported_periods_label.hide()
        layout.addWidget(self.imported_periods_label)
        self.remove_older_button = QToolButton()
        self.remove_older_button.setObjectName("historyTextAction")
        self.remove_older_button.setText("Remove older CSV history…")
        self.remove_older_button.clicked.connect(parent.remove_legacy_history_import)
        self.remove_older_button.hide()
        layout.addWidget(self.remove_older_button, alignment=Qt.AlignLeft)
        self.source.currentIndexChanged.connect(self._selection_changed)
        self.from_date.dateChanged.connect(self._selection_changed)
        self.to_date.dateChanged.connect(self._selection_changed)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("historyProgressLabel")
        self.progress_label.setTextFormat(Qt.PlainText)
        self.progress_label.setWordWrap(True)
        self.progress_label.hide()
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("syncProgressBar")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.details = QPlainTextEdit()
        self.details.setObjectName("historyImportDetails")
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(110)
        self.details.hide()
        layout.addWidget(self.details)
        self.resize(640, 300)

    def ensure_content_fits(self):
        self.layout().activate()
        minimum = self.minimumSizeHint()
        self.resize(max(self.width(), minimum.width() + 24),
                    max(self.height(), minimum.height() + 40))

    def _toggle_help(self, expanded):
        self.help_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.help_text.setVisible(expanded)
        if expanded:
            self.ensure_content_fits()

    def _toggle_coverage(self, expanded):
        self.coverage_button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.imported_periods_label.setVisible(expanded)
        self.remove_older_button.setVisible(expanded and self._has_older_history)
        if expanded:
            self.ensure_content_fits()

    def refresh_flex_limit(self):
        oldest = date(date.today().year - HISTORY_YEARS, 1, 1)
        self.older_import_button.setText(
            f"Before {oldest.isoformat()}? Import older CSV history…")

    def set_running(self, running, *, pausable=True):
        self._pause_requested = False
        for widget in (self.source, self.from_date, self.to_date, self.import_button,
                       self.older_import_button, self.remove_older_button):
            widget.setEnabled(not running)
        self.pause_button.setEnabled(running and pausable)
        self.pause_button.setVisible(running and pausable)
        self.pause_button.setText("Pause import")
        self.progress_bar.setVisible(running)

    def set_pause_requested(self):
        self._pause_requested = True
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pausing after current report…")
        self.progress_label.setText("Pausing after the current report or Cash group…")
        self.progress_label.show()

    def show_imported_periods(self, periods_by_source, monthly=(), *, has_older_history=False):
        text = format_imported_periods(periods_by_source).partition("\n")[2]
        if monthly:
            text += ("\nOlder monthly history: " + monthly[0]["month"] + " to " +
                     monthly[-1]["month"] + f" ({len(monthly)} months)")
        self.imported_periods_label.setText(text)
        self.imported_periods_label.setVisible(self.coverage_button.isChecked())
        self._has_older_history = has_older_history
        self.remove_older_button.setVisible(self.coverage_button.isChecked() and has_older_history)

    def _selection_changed(self, *_args):
        if self.import_button.text() == "Resume import":
            self.import_button.setText("Import history")

    def show_status(self, status):
        state = status.get("state", "starting")
        source = status.get("active_source") or (
            "legacy" if status.get("source") == "legacy" else None)
        span = status.get("current_window")
        label = {"completed": "Import completed", "completed_with_gaps": "Import completed with gaps",
                 "paused": "Import paused",
                 "interrupted": "Import interrupted",
                 "needs_review": "Import needs review",
                 "failed": "Import failed"}.get(state, "Importing history…")
        if status.get("source") == "legacy" and status.get("action") == "remove":
            label = {"completed": "Older CSV history removed",
                     "needs_review": "Removal needs review", "failed": "Removal failed"}.get(
                         state, "Removing older CSV history…")
        if state == "paused":
            self.import_button.setText("Resume import")
        elif state != "running":
            self.import_button.setText("Import history")
        if source and status.get("action") != "remove":
            label += f" · {'Older CSV' if source == 'legacy' else source.capitalize()}"
        if span:
            label += f" · {span[0]} to {span[1]}"
        if state in ("starting", "running") and self._pause_requested:
            label = "Pausing after the current report or Cash group…"
        self.progress_label.setText(label)
        self.progress_label.show()
        details = format_history_details(status)
        if details != self.details.toPlainText():
            self.details.setPlainText(details)
        self.details.setVisible(bool(details))
        self.ensure_content_fits()
