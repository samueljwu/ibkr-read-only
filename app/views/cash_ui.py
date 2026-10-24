"""Dated Cash Flex transactions and per-currency activity totals."""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from PySide6.QtCore import QByteArray, QDate, QTimer, Qt
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout,
                               QHeaderView, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy, QSplitter, QVBoxLayout,
                               QWidget)

from app.views import widgets
from app.views.date_input import CalendarDateEdit
from app.views.table_ui import ReportTable, SortItem, money
from app.views.ui_theme import (PAGE_MARGINS, PAGE_SPACING, TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING,
                          style_toolbar_dropdown)


SUMMARY_LINES = (
    ("Deposits", ("Deposit",)),
    ("Withdrawals", ("Withdrawal",)),
    ("Dividends", ("Dividend", "Payment in lieu")),
    ("Interest", ("Interest", "Credit interest", "Debit interest", "Stock lending interest")),
    ("Tax", ("Withholding tax",)),
    ("Other fees", ("Other fee",)),
)


def _money(value):
    return money(value, symbol=False)


class CashMovementsTab(QWidget):
    def __init__(self, db, portfolio_id, parent=None):
        super().__init__(parent)
        self.db, self.portfolio_id = db, portfolio_id
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(PAGE_SPACING)

        self.toolbar = QWidget()
        self.toolbar_grid = QGridLayout(self.toolbar)
        self.toolbar_grid.setContentsMargins(0, 0, 0, 0)
        self.toolbar_grid.setHorizontalSpacing(TOOLBAR_GROUP_SPACING)
        self.toolbar_grid.setVerticalSpacing(7)
        self.range_controls = QWidget()
        range_layout = QVBoxLayout(self.range_controls)
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(6)
        range_layout.setAlignment(Qt.AlignTop)
        controls = QHBoxLayout()
        controls.setSpacing(TOOLBAR_ITEM_SPACING)
        controls.addWidget(QLabel("Range"))
        self.range_combo = QComboBox()
        style_toolbar_dropdown(self.range_combo)
        self.range_combo.addItems(("All imported", "MTD", "YTD", "Last 12 months", "Custom"))
        self.range_combo.setFixedWidth(205)
        self.range_combo.currentTextChanged.connect(self._range_changed)
        controls.addWidget(self.range_combo)
        controls.addStretch()
        range_layout.addLayout(controls)
        self.date_controls = QWidget()
        dates = QHBoxLayout(self.date_controls)
        dates.setContentsMargins(0, 0, 0, 0)
        dates.setSpacing(TOOLBAR_ITEM_SPACING)
        self.from_label = QLabel("From")
        self.start_date = CalendarDateEdit(QDate.currentDate().addYears(-1))
        self.to_label = QLabel("To")
        self.end_date = CalendarDateEdit(QDate.currentDate())
        for editor in (self.start_date, self.end_date):
            editor.dateChanged.connect(self.refresh)
        for widget in (self.from_label, self.start_date, self.to_label, self.end_date):
            dates.addWidget(widget)
        dates.addStretch()
        range_layout.addWidget(self.date_controls)
        self.date_controls.hide()
        self.toolbar_grid.addWidget(self.range_controls, 0, 0)
        layout.addWidget(self.toolbar)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self._summary_user_resized = False
        self._setting_splitter = False
        self._splitter_stacked = None
        self.summary_pane = QWidget()
        self.summary_pane.setObjectName("cashSummaryPanel")
        self.summary_pane.setMinimumWidth(250)
        self.summary_pane.setMinimumHeight(90)
        summary_layout = QVBoxLayout(self.summary_pane)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(6)

        self.summary_heading = widgets.section_title("Cash activity")
        summary_layout.addWidget(self.summary_heading)
        self.summary_scroll = QScrollArea()
        self.summary_scroll.setObjectName("cashBreakdownScroll")
        self.summary_scroll.setFrameShape(QFrame.NoFrame)
        self.summary_scroll.setWidgetResizable(True)
        self.summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.summary_content = QWidget()
        self.summary_content.setObjectName("cashBreakdownContent")
        self.summary_list = QVBoxLayout(self.summary_content)
        self.summary_list.setContentsMargins(14, 12, 14, 12)
        self.summary_list.setSpacing(10)
        self.summary_scroll.setWidget(self.summary_content)
        summary_layout.addWidget(self.summary_scroll, 1)
        self.summary_values = {}
        self.splitter.addWidget(self.summary_pane)

        self.transactions_pane = QWidget()
        transactions_layout = QVBoxLayout(self.transactions_pane)
        transactions_layout.setContentsMargins(0, 0, 0, 0)
        transactions_layout.setSpacing(6)
        self.transactions_pane.setMinimumHeight(110)
        self.transactions_pane.setMinimumWidth(360)

        self.transactions_heading = widgets.section_title("Transactions · USD")
        transactions_layout.addWidget(self.transactions_heading)
        self.empty_status = QLabel()
        self.empty_status.setObjectName("hint")
        transactions_layout.addWidget(self.empty_status)
        self.table = ReportTable(0, 5)
        self.table.setHorizontalHeaderLabels(("Reported date", "Type", "Description", "Currency", "Amount"))
        self.table.configure_numbers(self.db, f"cash_transactions_decimals_{portfolio_id}",
                                     {4: (2, "plain")})
        header = self.table.horizontalHeader()
        for column in (0, 1, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for column, width in ((0, 112), (1, 170), (3, 82), (4, 130)):
            self.table.setColumnWidth(column, width)
        transactions_layout.addWidget(self.table, 1)
        self.splitter.addWidget(self.transactions_pane)
        self.splitter.handle(1).setToolTip("Drag to resize the cash breakdown")
        self.splitter.splitterMoved.connect(self._splitter_moved)
        layout.addWidget(self.splitter, 1)
        self._arrange_for_width(self.width())
        self.refresh()

    def _range_changed(self, *_):
        custom = self.range_combo.currentText() == "Custom"
        self.date_controls.setVisible(custom)
        self.refresh()

    def view_state(self):
        header = self.table.horizontalHeader()
        sort_column = header.sortIndicatorSection()
        return {
            "version": 1,
            "range": self.range_combo.currentText(),
            "start": self.start_date.date().toString("yyyy-MM-dd"),
            "end": self.end_date.date().toString("yyyy-MM-dd"),
            "header": bytes(header.saveState().toBase64()).decode("ascii"),
            "sort_column": sort_column if 0 <= sort_column < self.table.columnCount() else 0,
            "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
            "amount_decimals": self.table.decimals_for(4),
        }

    def apply_view_state(self, state):
        self.start_date.setDate(QDate.fromString(state["start"], "yyyy-MM-dd"))
        self.end_date.setDate(QDate.fromString(state["end"], "yyyy-MM-dd"))
        self.range_combo.setCurrentText(state["range"])
        self.refresh()
        header = self.table.horizontalHeader()
        header.restoreState(QByteArray.fromBase64(state["header"].encode("ascii")))
        self.table.set_column_decimals(4, state["amount_decimals"])
        self.table.sortItems(state["sort_column"],
                             Qt.DescendingOrder if state["sort_order"] == "desc" else Qt.AscendingOrder)

    def showEvent(self, event):
        super().showEvent(event)
        self._arrange_for_width(self.width())
        QTimer.singleShot(0, self._fit_summary)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toolbar_grid") and hasattr(self, "splitter"):
            self._arrange_for_width(self.width())

    def _splitter_moved(self, *_):
        if not self._setting_splitter:
            self._summary_user_resized = True

    def _arrange_for_width(self, width):
        splitter_stacked = width < 1160
        if splitter_stacked != self._splitter_stacked:
            self._setting_splitter = True
            try:
                self.splitter.setOrientation(Qt.Vertical if splitter_stacked else Qt.Horizontal)
            finally:
                self._setting_splitter = False
            self._splitter_stacked = splitter_stacked
            self._summary_user_resized = False
            QTimer.singleShot(0, self._fit_summary)

    def _fit_summary(self):
        if self._summary_user_resized or not self.isVisible():
            return
        stacked = self.splitter.orientation() == Qt.Vertical
        available = ((self.splitter.height() if stacked else self.splitter.width()) -
                     self.splitter.handleWidth())
        if available <= 0:
            return
        target = (min(280, max(self.summary_pane.minimumHeight(), int(available * .38)))
                  if stacked else min(325, max(self.summary_pane.minimumWidth(), int(available * .29))))
        self._setting_splitter = True
        try:
            self.splitter.setSizes([target, max(0, available - target)])
        finally:
            self._setting_splitter = False

    def _bounds(self):
        today = date.today()
        selection = self.range_combo.currentText()
        if selection == "Custom":
            return self.start_date.date().toPython(), self.end_date.date().toPython()
        if selection == "MTD":
            return today.replace(day=1), today
        if selection == "YTD":
            return today.replace(month=1, day=1), today
        if selection == "Last 12 months":
            return QDate.currentDate().addYears(-1).toPython(), today
        return None, None

    def _visible_currencies(self, all_rows):
        return sorted({"USD"} | {row["currency"] for row in all_rows},
                      key=lambda code: (code != "USD", code))

    def _show_summary(self, rows, currencies):
        while self.summary_list.count():
            item = self.summary_list.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.summary_values = {}
        by_currency = defaultdict(lambda: defaultdict(Decimal))
        for row in rows:
            by_currency[row["currency"]][row["kind"]] += Decimal(row["amount"])
        known_kinds = {kind for _, kinds in SUMMARY_LINES for kind in kinds}
        tips = {
            "Dividends": "Includes dividends and payments in lieu.",
            "Interest": "Credit, debit, and stock lending interest, netted in this currency.",
            "Other fees": "Other statement fees; trade commissions and refunds remain with trades.",
            "Total cash effect": ("Sum of cash transactions in this currency and date range, "
                                  "including separately reported commission adjustments. "
                                  "Excludes security trade cash flows; it is not a NAV return measure."),
        }

        def add_row(box, code, label, amount, *, total=False):
            row = QWidget()
            row.setObjectName("cashTotalRow" if total else "cashSummaryRow")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 9 if total else 4, 0, 5 if total else 4)
            line.setSpacing(8)
            caption = QLabel(label)
            caption.setObjectName("cashTotalLabel" if total else "cashItemLabel")
            caption.setWordWrap(True)
            caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            value = QLabel(_money(amount))
            value.setObjectName("cashTotalAmount" if total else "cashAmount")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setMinimumWidth(80)
            value.setAccessibleName(f"{label} {code}")
            if label in tips:
                caption.setToolTip(tips[label])
                value.setToolTip(tips[label])
            line.addWidget(caption, 1)
            line.addWidget(value)
            box.addWidget(row)
            self.summary_values[(label, code)] = value

        for code in currencies:
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(0)
            if len(currencies) > 1:
                currency_label = QLabel(code)
                currency_label.setObjectName("cashCurrencyLabel")
                section_layout.addWidget(currency_label)
            totals = by_currency[code]
            for label, kinds in SUMMARY_LINES:
                add_row(section_layout, code, label,
                        sum((totals[kind] for kind in kinds), Decimal(0)))
            extras = [(kind, amount) for kind, amount in sorted(totals.items())
                      if kind not in known_kinds and amount]
            for label, amount in extras:
                add_row(section_layout, code, label, amount)
            add_row(section_layout, code, "Total cash effect",
                    sum(totals.values(), Decimal(0)), total=True)
            self.summary_list.addWidget(section)
        self.summary_list.addStretch()
        self.fit_summary_text()
        QTimer.singleShot(0, self.fit_summary_text)

    def fit_summary_text(self):
        """Let the scroll area scroll instead of compressing summary rows."""
        self.summary_list.activate()
        self.summary_content.setMinimumHeight(self.summary_content.sizeHint().height())

    def _show_transactions(self, rows):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row["reported_day"], row["kind"], row["description"],
                      row["currency"], _money(row["amount"]))
            for column, value in enumerate(values):
                item = (self.table.numeric_item(4, Decimal(row["amount"])) if column == 4
                        else SortItem(value))
                if column == 4:
                    item.setData(Qt.UserRole, float(row["amount"]))
                item.setToolTip(f"IBKR {row['source_section']} · statement {row['period_from']} to {row['period_to']}")
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)

    def refresh(self, *_):
        all_rows = self.db.cash_movement_rows(self.portfolio_id)
        first, last = self._bounds()
        valid = first is None or first <= last
        currencies = self._visible_currencies(all_rows)
        shown = ", ".join(currencies)
        self.summary_heading.setText(f"Cash activity · {currencies[0]}" if len(currencies) == 1
                                     else "Cash activity")
        self.transactions_heading.setText(f"Transactions · {shown}")
        rows = [row for row in all_rows
                if valid and row["currency"] in currencies
                and (first is None or first.isoformat() <= row["reported_day"] <= last.isoformat())]
        self._show_summary(rows, currencies)
        self._show_transactions(rows)
        self.table.setColumnHidden(3, len(currencies) == 1)
        empty = valid and not rows
        self.empty_status.setText("No cash transactions in this range." if empty else
                                  "Choose a valid date range." if not valid else "")
        self.empty_status.setVisible(empty or not valid)
