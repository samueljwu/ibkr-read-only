"""Readable status, saved history, and diagnostics for the Settings dialog."""

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout, QHeaderView,
                               QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QScrollArea, QToolButton, QVBoxLayout, QWidget)

from app.sync.history_status import (format_synced_history, imported_statement_trade_summary,
                               imported_statement_transfer_summary,
                               reconciliation_status,
                               synced_history_coverage)
from app.ui.table_ui import ReportTable, SortItem


def _label(text="", name="settingsValue"):
    label = QLabel(text)
    label.setObjectName(name)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setFocusPolicy(Qt.ClickFocus)
    policy = label.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Ignored)
    label.setSizePolicy(policy)
    return label


def _set_expanded(button, content, expanded):
    """Set the state now, then reveal it after Qt updates the scroll range."""
    content.setVisible(expanded)
    _refresh_layout(content)
    QTimer.singleShot(0, button, lambda: _reveal_expansion(button, content, expanded))


def _refresh_layout(content):
    content.updateGeometry()
    parent = content.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        if parent.layout() is not None:
            parent.layout().invalidate()
            parent.layout().activate()
        parent.updateGeometry()
        parent = parent.parentWidget()


def _reveal_expansion(button, content, expanded):
    # A second click or a closed parent must not replay an old expansion.
    if button.isChecked() != expanded or not button.isVisible():
        return
    parent = button.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        return
    parent.ensureWidgetVisible(button, 0, 8)
    if expanded and content.isVisible():
        top = content.mapTo(parent.widget(), QPoint(0, 0))
        first_lines = min(content.height(), 3 * content.fontMetrics().height())
        parent.ensureVisible(top.x(), top.y() + first_lines, 0, 12)


class SettingsStatus(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsStatus")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.summary = _label(name="settingsKey")
        layout.addWidget(self.summary)
        self.warning = _label(name="settingsWarning")
        self.warning.hide()
        layout.addWidget(self.warning)

        self.history_section = SettingsSection("History & Flex")
        layout.addWidget(self.history_section)
        history = self.history_section.content_layout
        history.addWidget(_label("Saved history", "settingsHeading"))
        self.history_account = _label(name="settingsKey")
        history.addWidget(self.history_account)
        self.history_table = ReportTable(3, 3)
        self.history_table.setObjectName("settingsHistoryTable")
        self.history_table.setHorizontalHeaderLabels(("Source", "Saved dates", "Status"))
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionsMovable(False)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.history_table.verticalHeader().setDefaultSectionSize(32)
        self.history_table.setFixedHeight(132)
        history.addWidget(self.history_table)
        self.history_saved_note = _label(name="settingsKey")
        self.history_saved_note.hide()
        history.addWidget(self.history_saved_note)
        self.history_notice = _label(name="settingsWarning")
        history.addWidget(self.history_notice)
        self.history_details_button = self._toggle(history, "Show all date ranges", "Hide date ranges")
        self.history_details = _label()
        self.history_details.hide()
        history.addWidget(self.history_details)
        self.history_details_button.toggled.connect(
            lambda expanded: _set_expanded(self.history_details_button, self.history_details, expanded))

        self.diagnostics_section = SettingsSection("Diagnostics")
        layout.addWidget(self.diagnostics_section)
        diagnostics = self.diagnostics_section.content_layout
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        self.values = {}
        for row, title in enumerate(("Connection", "Market data", "Subscribed symbols", "Current prices")):
            key = _label(title, "settingsKey")
            key.setWordWrap(False)
            key.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            grid.addWidget(key, row, 0)
            value = _label()
            self.values[title] = value
            grid.addWidget(value, row, 1)
        diagnostics.addLayout(grid)
        row = QHBoxLayout()
        self.technical_button = self._toggle(row, "Technical details", "Hide technical details")
        row.addStretch()
        self.copy_button = QPushButton("Copy diagnostics")
        self.copy_button.setObjectName("secondary")
        self.copy_button.clicked.connect(self.copy_diagnostics)
        row.addWidget(self.copy_button)
        diagnostics.addLayout(row)
        self.technical_details = _label()
        self.technical_details.hide()
        diagnostics.addWidget(self.technical_details)
        self.technical_button.toggled.connect(
            lambda expanded: _set_expanded(self.technical_button, self.technical_details, expanded))
        self._history_key = None
        self._history_revision = None

    @staticmethod
    def _toggle(layout, closed, opened):
        button = QPushButton(closed)
        button.setObjectName("secondary")
        button.setCheckable(True)
        button.setAutoDefault(False)
        button.toggled.connect(lambda checked: button.setText(opened if checked else closed))
        layout.addWidget(button)
        return button

    def update_status(self, db, portfolio_id, values, warnings, details, *, refresh_history=True):
        self._reconciliation_context = db, portfolio_id
        for key, value in values.items():
            self.values[key].setText(str(value))
        connection = self.values["Connection"].text()
        mode = self.values["Market data"].text()
        self.summary.setText("Demo · Active" if connection == "Mock" else
                             " · ".join(part for part in (connection, mode) if part))
        self.warning.setText("\n".join(
            warning if len(warning) <= 180 else warning[:177] + "…" for warning in warnings))
        self.warning.setVisible(bool(warnings))
        self.technical_details.setText(details)
        if not refresh_history:
            return
        # total_changes catches this connection's writes; data_version also
        # catches commits made by the background import/sync connections.
        revision = (db.conn, portfolio_id, db.conn.total_changes,
                    db.conn.execute("PRAGMA data_version").fetchone()[0])
        if revision == self._history_revision:
            return
        self._history_revision = revision
        coverage = synced_history_coverage(db, portfolio_id)
        account = db.setting("ib_account_id").strip()
        statement_trades = imported_statement_trade_summary(db, portfolio_id)
        history_key = (account, coverage, statement_trades, imported_statement_transfer_summary(db, portfolio_id))
        if history_key == self._history_key:
            return
        self._history_key = history_key
        self.history_account.setText(f"{account} · Inclusive dates" if account else "Set an IBKR account to see saved dates.")
        self.history_table.setVisible(bool(account))
        self.history_details_button.setVisible(bool(account))
        self.history_details.setText(format_synced_history(db, portfolio_id))
        notices = []
        has_statement = False
        extra_height = 0
        for index, row in enumerate(coverage):
            ranges = row["ranges"]
            saved = "; ".join(f"{first} to {last}" for first, last in ranges[:1])
            if len(ranges) > 1:
                saved += f" (+{len(ranges) - 1} more)"
                notices.append(f"{row['label']}: gaps between saved ranges.")
            saved_lines = [saved] if saved else []
            saved_tooltip = '\n'.join(f'{first} to {last}' for first, last in ranges)
            if row['statement_dates']:
                has_statement = True
                saved_lines = [f'Flex: {saved}'] if saved else []
                for kind, statement in row['statement_dates'].items():
                    dates = statement['ranges']
                    first, last = dates[0][0], dates[-1][1]
                    label = 'CSV trades' if kind == 'trades' else 'CSV holdings'
                    saved_lines.append(f'{label}: ' + (first if first == last else f'{first} to {last}'))
                    saved_tooltip += '\n' + label + ': ' + '; '.join(
                        first if first == last else f'{first} to {last}' for first, last in dates)
                saved = '\n'.join(saved_lines)
            state = ("Needs review" if row["review"] or row['review_issues'] else
                     "Pending records" if row['pending_issues'] else
                     "Gaps" if len(ranges) > 1 else "Saved" if ranges or row['statement_dates'] else "No history")
            if row["review"]:
                dates = '; '.join(f'{first} to {last}' for first, last in row['review'])
                notices.append(f"{row['label']}: these report dates need review because they have no complete replacement: {dates}. "
                               f"Recheck them with Import history, source {row['label']}. Saved records are kept.")
            if row['review_issues'] or row['pending_issues']:
                notices.append(f"{row['label']}: " + '\n'.join(row['review_issues'] + row['pending_issues'] + row['guidance']))
            for column, text in enumerate((row["label"], saved or "No synced history", state)):
                item = SortItem(text)
                item.setToolTip(saved_tooltip.strip() if column == 1 else
                                '\n'.join(notices) if column == 2 and state in ('Needs review', 'Pending records') else text)
                self.history_table.setItem(index, column, item)
            height = max(32, len(saved_lines) * self.history_table.fontMetrics().height() + 12)
            self.history_table.setRowHeight(index, height)
            extra_height += height - 32
        self.history_table.setFixedHeight(132 + extra_height)
        self.history_saved_note.setText('CSV dates show saved trade records and holding snapshots. '
                                        'The days between those records are not verified report coverage.')
        self.history_saved_note.setVisible(has_statement)
        self.history_notice.setText("\n".join(notices))
        self.history_notice.setVisible(bool(notices))
        self.history_details.setVisible(bool(account) and self.history_details_button.isChecked())

    def copy_diagnostics(self):
        status = "\n".join(f"{key}: {label.text()}" for key, label in self.values.items())
        reconciliation = ''
        if hasattr(self, '_reconciliation_context'):
            issues = reconciliation_status(*self._reconciliation_context)
            reconciliation = '\n\n'.join(issues['reconciliation_guidance'] + issues['reconciliation_details'])
        blocks = (status, self.warning.text(), self.history_details.text(), self.technical_details.text(), reconciliation)
        QApplication.clipboard().setText("\n\n".join(block for block in blocks if block))


class SettingsSection(QFrame):
    """Keyboard-accessible disclosure for settings used occasionally."""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsSection")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.toggle = QToolButton()
        self.toggle.setObjectName("settingsDisclosure")
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.toggle.setAccessibleName(title)
        self.toggle.setText(title.replace("&", "&&"))
        layout.addWidget(self.toggle)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(2, 12, 2, 6)
        self.content_layout.setSpacing(12)
        layout.addWidget(self.content)
        self.content.hide()
        self.toggle.toggled.connect(lambda expanded: _set_expanded(self.toggle, self.content, expanded))
        self.toggle.toggled.connect(
            lambda expanded: self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow))
