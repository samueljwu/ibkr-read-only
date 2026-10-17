"""Dated Flex import controls and worker progress presentation.

Laid out as a short stack of cards: the Flex import form, a progress card
that only appears once a run has produced something to show, a permanent
"Imported periods" summary table, and an "Older CSV history" card for the
one-time legacy import/removal path.
"""

from datetime import timedelta

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (QCalendarWidget, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from app.platform.history_status import format_history_details, merge_periods
from app.flex.flex_import import flex_today, oldest_flex_date
from app.views import widgets
from app.views.table_ui import ReportTable, SortItem


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
        self._has_older_history = False
        self.setWindowTitle("Import Data — IBKR Flex history")
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "dialog")

        heading = QLabel("Import IBKR history")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        layout.addWidget(widgets.hint("Choose reports and completed dates."))

        # -- Flex import card -----------------------------------------------
        self.flex_card, flex_layout = widgets.card()
        flex_card = self.flex_card
        flex_card.setObjectName("historyImportForm")
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(5)
        self.source = QComboBox()
        for label, value in (("All reports", "all"), ("Trades", "trades"),
                             ("Analytics", "analytics"), ("Cash", "cash")):
            self.source.addItem(label, value)
        yesterday = flex_today() - timedelta(days=1)
        first_day = yesterday - timedelta(days=29)
        self.from_date = HistoryDateInput(QDate(first_day.year, first_day.month, first_day.day))
        self.to_date = HistoryDateInput(QDate(yesterday.year, yesterday.month, yesterday.day))
        for date_field in (self.from_date, self.to_date):
            date_field.setMaximumDate(QDate(yesterday.year, yesterday.month, yesterday.day))
        for column, caption, field in ((0, "Source", self.source),
                                       (1, "From", self.from_date),
                                       (2, "To", self.to_date)):
            form.addWidget(widgets.field_label(caption, buddy=field), 0, column)
            form.addWidget(field, 1, column)
        form.setColumnStretch(0, 2)
        form.setColumnStretch(1, 1)
        form.setColumnStretch(2, 1)
        flex_layout.addLayout(form)
        layout.addWidget(flex_card)

        # -- Progress card: hidden until a run or a result exists -----------
        self.progress_card, progress_layout = widgets.card()
        self.progress_label = QLabel()
        self.progress_label.setObjectName("historyProgressLabel")
        self.progress_label.setTextFormat(Qt.PlainText)
        self.progress_label.setWordWrap(True)
        progress_header = QHBoxLayout()
        progress_header.addWidget(self.progress_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("syncProgressBar")
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(130)
        self.progress_bar.setFixedHeight(12)
        progress_header.addWidget(self.progress_bar, alignment=Qt.AlignVCenter)
        progress_layout.addLayout(progress_header)
        self.details = QPlainTextEdit()
        self.details.setObjectName("historyImportDetails")
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(110)
        progress_layout.addWidget(self.details)
        self.progress_card.hide()
        layout.addWidget(self.progress_card)

        # -- Imported periods card: always visible ---------------------------
        periods_card, periods_layout = widgets.card()
        periods_layout.addWidget(widgets.section_title("Imported periods"))
        self.periods_table = ReportTable(0, 2)
        self.periods_table.setHorizontalHeaderLabels(("Source", "Periods"))
        self.periods_table.horizontalHeader().setStretchLastSection(True)
        self.periods_table.setColumnWidth(0, 110)
        self.periods_table.setMaximumHeight(140)
        periods_layout.addWidget(self.periods_table)
        layout.addWidget(periods_card)

        # -- Older CSV history card -------------------------------------------
        # Always visible: it is the only entry point for starting a legacy
        # import, so it must stay discoverable even before any older history
        # has been imported. Only the Remove action inside is conditional.
        self.older_card, older_layout = widgets.card()
        self.older_card_title = widgets.section_title("Older CSV history")
        older_layout.addWidget(self.older_card_title)
        older_layout.addWidget(widgets.hint(
            "Select the PortfolioAnalyst and Activity Statement CSVs together; order does not matter."))
        older_actions = QHBoxLayout()
        self.older_import_button = widgets.button(
            "Import older CSV history…", parent.start_legacy_history_import)
        older_actions.addWidget(self.older_import_button)
        older_actions.addStretch()
        self.remove_older_button = QPushButton("Remove older CSV history…")
        self.remove_older_button.setObjectName("danger")
        self.remove_older_button.clicked.connect(parent.remove_legacy_history_import)
        self.remove_older_button.hide()
        older_actions.addWidget(self.remove_older_button)
        older_layout.addLayout(older_actions)
        layout.addWidget(self.older_card)

        self.source.currentIndexChanged.connect(self._selection_changed)
        self.from_date.dateChanged.connect(self._selection_changed)
        self.to_date.dateChanged.connect(self._selection_changed)

        # -- Help toggle -------------------------------------------------------
        self.help_button = QToolButton()
        self.help_button.setObjectName("historyHelpButton")
        self.help_button.setText("How imports work")
        self.help_button.setCheckable(True)
        self.help_button.setArrowType(Qt.RightArrow)
        self.help_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.help_button.toggled.connect(self._toggle_help)
        layout.addWidget(self.help_button, alignment=Qt.AlignLeft)
        self.help_text = widgets.hint(
            "Uses the account, query IDs, and saved Flex token in Settings. "
            "IBKR may have gaps within the available range; report validation determines coverage. "
            "Identical data is skipped; "
            "verified corrections update existing data. Cash combines adjacent saved periods "
            "into checks of up to 365 days; boundary periods may extend beyond these dates. "
            "Changed reports create local database backups.")
        self.help_text.hide()
        layout.addWidget(self.help_text)

        # -- Footer: Pause / Import / Close -------------------------------------
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch()
        self.pause_button = QPushButton("Pause import")
        self.pause_button.setObjectName("secondary")
        self.pause_button.clicked.connect(parent.pause_history_import)
        self.pause_button.setEnabled(False)
        self.pause_button.hide()
        footer.addWidget(self.pause_button)
        self.import_button = QPushButton("Import history")
        self.import_button.clicked.connect(parent.start_history_import)
        footer.addWidget(self.import_button)
        close_buttons = widgets.dialog_buttons(self, QDialogButtonBox.Close)
        close_buttons.rejected.connect(self.reject)
        footer.addWidget(close_buttons)
        layout.addLayout(footer)

        self.refresh_flex_limit()
        self.resize(640, 440)

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

    def refresh_flex_limit(self):
        oldest = oldest_flex_date()
        self.older_card_title.setText(f"Older CSV history (before {oldest.isoformat()})")

    def _sync_progress_card(self):
        # isVisible() is false for a child of a currently-hidden parent, which
        # would make this computation self-defeating right after the card
        # itself was last hidden; isHidden() instead reports each widget's
        # own explicit shown/hidden state regardless of its ancestors.
        self.progress_card.setVisible(
            not self.progress_label.isHidden() or not self.progress_bar.isHidden()
            or not self.details.isHidden())

    def set_running(self, running, *, pausable=True):
        self._pause_requested = False
        for widget in (self.source, self.from_date, self.to_date, self.import_button,
                       self.older_import_button, self.remove_older_button):
            widget.setEnabled(not running)
        self.pause_button.setEnabled(running and pausable)
        self.pause_button.setVisible(running and pausable)
        self.pause_button.setText("Pause import")
        self.progress_bar.setVisible(running)
        self._sync_progress_card()

    def set_pause_requested(self):
        self._pause_requested = True
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pausing after current report…")
        self.progress_label.setText("Pausing after the current report or Cash group…")
        self.progress_label.show()
        self._sync_progress_card()

    def show_imported_periods(self, periods_by_source, monthly=(), *, has_older_history=False):
        merged = merge_periods(periods_by_source)
        rows = []
        for source in ("trades", "analytics", "cash"):
            spans = merged[source]
            text = ", ".join(f"{first.isoformat()} to {last.isoformat()}" for first, last in spans) or "none"
            rows.append((source.capitalize(), text))
        if monthly:
            rows.append(("Older monthly history",
                         monthly[0]["month"] + " to " + monthly[-1]["month"] + f" ({len(monthly)} months)"))
        self.periods_table.setRowCount(len(rows))
        for index, (label, text) in enumerate(rows):
            self.periods_table.setItem(index, 0, SortItem(label))
            self.periods_table.setItem(index, 1, SortItem(text))
        self._has_older_history = has_older_history
        self.remove_older_button.setVisible(has_older_history)
        self.ensure_content_fits()

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
        self._sync_progress_card()
        self.ensure_content_fits()
