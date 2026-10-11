"""Compact, shared reconciliation details for Summary and Cash."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QDialogButtonBox, QHeaderView, QHBoxLayout,
                               QLabel, QPushButton, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QTextBrowser, QVBoxLayout)

from app.ui.dialog_ui import ResizableDialog


def cash_summary(checks):
    cash = [c for c in checks if c.check == 'Independent cash']
    if not cash:
        return 'Independent cash: Unavailable'
    latest = {}
    for check in cash:
        if check.currency not in latest or check.period_to > latest[check.currency].period_to:
            latest[check.currency] = check
    status = ('Difference' if any(c.status == 'Difference' for c in cash) else
              'Partial' if any(c.status == 'Partial' for c in latest.values()) or
              any('freshness' in c.check for c in checks) else
              'Unavailable' if any(c.status == 'Unavailable' for c in latest.values()) else 'Verified')
    through = min((c.period_to for c in latest.values() if c.period_to), default='')
    return f'Independent cash: {status}' + (f' through {through}' if through else '')


class ReconciliationDialog(ResizableDialog):
    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider, self._checks = provider, None
        self.setWindowTitle('Calculation checks')
        self.resize(700, 560)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        explanation = QLabel('Verified applies only to the dated check shown. Partial means the check is incomplete '
                             'because of missing information, unresolved records or an app limitation; '
                             'Unavailable means there is no comparable evidence.')
        explanation.setObjectName('hint')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.explanation = explanation
        self.list = QTreeWidget()
        self.list.setObjectName('reconciliationChecks')
        self.list.setHeaderLabels(('Check', 'Result', 'Scope / dates'))
        self.list.setRootIsDecorated(True)
        self.list.setColumnWidth(0, 240)
        self.list.setColumnWidth(1, 90)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self.list.header().setMinimumSectionSize(60)
        self.list.currentItemChanged.connect(self._selected)
        layout.addWidget(self.list, 1)
        self.details = QTextBrowser()
        self.details.setObjectName('reconciliationDetails')
        self.details.setMinimumHeight(110)
        layout.addWidget(self.details, 1)
        diagnostic_row = QHBoxLayout()
        self.technical_toggle = QToolButton()
        self.technical_toggle.setObjectName('secondary')
        self.technical_toggle.setFocusPolicy(Qt.StrongFocus)
        self.technical_toggle.setText('Technical details')
        self.technical_toggle.setCheckable(True)
        self.technical_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.technical_toggle.setArrowType(Qt.RightArrow)
        self.technical_toggle.toggled.connect(self._toggle_technical)
        diagnostic_row.addWidget(self.technical_toggle)
        diagnostic_row.addStretch()
        self.copy_diagnostics = QPushButton('Copy diagnostics')
        self.copy_diagnostics.setObjectName('secondary')
        self.copy_diagnostics.clicked.connect(lambda: QApplication.clipboard().setText(self._technical_text))
        diagnostic_row.addWidget(self.copy_diagnostics)
        layout.addLayout(diagnostic_row)
        self.technical = QTextBrowser()
        self.technical.setObjectName('reconciliationDetails')
        self.technical.setMinimumHeight(85)
        self.technical.hide()
        self._technical_text = ''
        layout.addWidget(self.technical, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'list'):
            self._fit_summary_labels()
            self.list.setColumnWidth(0, min(260, max(140, int(self.width() * .38))))
            self.list.setColumnWidth(1, 88)

    def _fit_summary_labels(self):
        margins = self.layout().contentsMargins()
        width = max(1, self.width() - margins.left() - margins.right())
        for label in (self.summary, self.explanation):
            label.setMinimumHeight(label.heightForWidth(width))

    def refresh(self):
        checks = self.provider()
        if checks == self._checks:
            return
        selected = self.list.currentItem()
        identity = selected.data(0, Qt.UserRole) if selected else None
        old = self._checks[identity] if identity is not None and self._checks else None
        self._checks = checks
        self.summary.setText(cash_summary(checks) + '\nSelect a check for its amounts, missing inputs and next action.')
        self._fit_summary_labels()
        self.list.clear()
        target = None
        cash = [(index, c) for index, c in enumerate(checks) if c.check == 'Independent cash']
        latest = {}
        for index, check in cash:
            if check.currency not in latest or check.period_to > latest[check.currency][1].period_to:
                latest[check.currency] = (index, check)
        primary = {index for index, _ in latest.values()}
        components = {'Sales / positive FX legs', 'Purchases / negative FX legs', 'Commissions / refunds',
                      'Transaction tax', 'Non-trade movements', 'Statement arithmetic'}
        groups = {}
        quantities = None
        inputs = None
        history = None
        # Primary comparisons first, then their collapsed component rows.
        ordered = list(latest.values()) + [(i, c) for i, c in enumerate(checks)
                  if c.check != 'Independent cash' and c.check not in components and
                  not c.check.startswith(('Non-trade ·', 'Quantity ·', 'Broker-only holding ·', 'Inputs ·', 'Lot inputs ·'))]
        ordered += [(i, c) for i, c in enumerate(checks) if c.check.startswith(
                    ('Quantity ·', 'Broker-only holding ·', 'Inputs ·', 'Lot inputs ·'))]
        ordered += [(i, c) for i, c in cash if i not in primary]
        ordered += [(i, c) for i, c in enumerate(checks) if c.check in components or c.check.startswith('Non-trade ·')]
        for index, check in ordered:
            period = (f'{check.period_from} to {check.period_to}' if check.period_from else check.period_to)
            item = QTreeWidgetItem((check.check, check.status, ' · '.join(s for s in (check.currency, period) if s)))
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(2, f'Account {check.account or "unset"}\n{period}')
            scope = (check.currency, check.period_from, check.period_to)
            if check.check == 'Independent cash':
                groups[scope] = item
                if index in primary or not check.period_to:
                    self.list.addTopLevelItem(item)
                else:
                    if history is None:
                        history = QTreeWidgetItem(('Earlier saved periods', '', 'Expand to review history'))
                        self.list.addTopLevelItem(history)
                    history.addChild(item)
            elif check.check in components or check.check.startswith('Non-trade ·'):
                if scope in groups:
                    groups[scope].addChild(item)
            elif check.check == 'Holdings quantities':
                quantities = item
                self.list.addTopLevelItem(item)
            elif check.check.startswith(('Quantity ·', 'Broker-only holding ·')):
                if quantities is not None:
                    quantities.addChild(item)
            elif check.check.startswith(('Inputs ·', 'Lot inputs ·')):
                if inputs is None:
                    inputs = QTreeWidgetItem(('Missing calculation inputs', 'Partial', 'Expand for affected holdings / lots'))
                    self.list.addTopLevelItem(inputs)
                inputs.addChild(item)
            else:
                self.list.addTopLevelItem(item)
            if old and (check.check, check.currency, check.period_from, check.period_to) == (
                    old.check, old.currency, old.period_from, old.period_to):
                target = item
            elif target is None and check.check == 'Independent cash':
                target = item
        if checks:
            self.list.setCurrentItem(target or self.list.topLevelItem(0))

    def _selected(self, item, *_):
        self.technical_toggle.setChecked(False)
        if item is None or item.data(0, Qt.UserRole) is None:
            self.details.clear()
            self.technical.clear()
            self._technical_text = ''
            self.copy_diagnostics.setEnabled(False)
            self.technical_toggle.setEnabled(False)
            return
        check = self._checks[item.data(0, Qt.UserRole)]
        def amount(value):
            precision = 6 if check.check.startswith('Quantity') else 2
            return f'{value:,.{precision}f}' if value is not None else 'Unavailable'
        lines = [f'{check.check}: {check.status}']
        if check.period_from or check.period_to:
            lines.append(f'Dates: {check.period_from + " to " if check.period_from else ""}{check.period_to}' +
                         (f' · {check.currency}' if check.currency else ''))
        if check.local is not None or check.broker is not None or check.difference is not None:
            if check.local is not None or check.broker is not None:
                lines.append(f'App: {amount(check.local)}   IBKR: {amount(check.broker)} {check.currency}')
            if check.difference is not None:
                label = 'Report difference' if check.check == 'Statement arithmetic' else 'Difference (App − IBKR)'
                lines.append(f'{label}: {amount(check.difference)} {check.currency}')
        if check.reason:
            lines.append('Why: ' + check.reason)
        if check.action:
            lines.append('Next: ' + check.action)
        self.details.setPlainText('\n'.join(lines))
        metadata = f'Account {check.account or "unset"}\nSource: {check.source}\n{check.completeness}\nTolerance: {check.tolerance} {check.currency}'
        self._technical_text = '\n'.join(lines[:3]) + '\n' + metadata + '\n\n' + (check.technical_details or
                               f'Source: {check.source}\n{check.reason}\n{check.action}')
        self.technical.setPlainText(self._technical_text)
        self.copy_diagnostics.setEnabled(True)
        self.technical_toggle.setEnabled(True)

    def _toggle_technical(self, checked):
        self.technical_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.technical.setVisible(checked)
