"""Cumulative instrument returns with an auditable lot and income drill-down."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit,
                               QVBoxLayout, QWidget)

from app.finance.position_returns import position_return_rows
from app.views import widgets
from app.views.metric_summary import MetricInfoButton, MetricSummary, MetricValue
from app.views.table_ui import ReportTable, SortItem, money
from app.views.toolbar import Toolbar
from app.views.ui_theme import COLORS, style_toolbar_dropdown


class ReturnPanel(QWidget):
    def __init__(self, db, service, portfolio_id, parent=None):
        super().__init__(parent)
        self.db, self.service, self.portfolio_id = db, service, portfolio_id
        self.palette_name = "light"
        self.rows = []
        self.unattributed = []
        self._preferred_id = None
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "subpage")
        layout.setSpacing(8)

        self.toolbar = toolbar = Toolbar()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Name, ticker or contract")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(290)
        self.search.textChanged.connect(self._filter)
        self.search.setMinimumWidth(180)
        toolbar.add_control(self.search, "Search")
        self.show = QComboBox()
        style_toolbar_dropdown(self.show)
        self.show.addItems(("All", "Currently held", "Closed"))
        self.show.currentIndexChanged.connect(self._filter)
        toolbar.add_control(self.show, "Show")
        self.type_filter = QComboBox()
        style_toolbar_dropdown(self.type_filter)
        for label, security_type in (("All", "All"), ("Stocks", "STK"),
                                     ("Options", "OPT"), ("Bonds", "BOND")):
            self.type_filter.addItem(label, security_type)
        self.type_filter.currentIndexChanged.connect(self._filter)
        toolbar.add_control(self.type_filter, "Type")
        self.layout_combo = widgets.layout_choice("Returns tables")
        self._split_key = f"position_return_splitter_{portfolio_id}"
        toolbar.add_control(self.layout_combo, "Layout", trailing=True)
        self.notes_button = MetricInfoButton("Return notes")
        layout.addWidget(toolbar)

        summary = MetricSummary(framed=False)
        self.stats = {}
        blocks = []
        for label in ("Total return", "Realized P&L", "Unrealized P&L", "Income and accruals"):
            block = MetricValue(label)
            blocks.append(block)
            self.stats[label] = block.value
        summary.set_blocks(blocks)
        layout.addWidget(summary)
        footer = QHBoxLayout()
        self.scope = QLabel()
        self.scope.setObjectName("hint")
        footer.addWidget(self.scope)
        footer.addWidget(self.notes_button)
        footer.addStretch()
        layout.addLayout(footer)
        self.note = QLabel(self)
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        self.note.hide()

        self.table = ReportTable(0, 8)
        self.table.setHorizontalHeaderLabels(("Name", "Status", "Realized USD", "Unrealized USD",
                                              "Income and accruals USD", "Total return USD", "Return on cash cost %",
                                              "Data status"))
        self.table.configure_numbers(db, f"position_return_decimals_{portfolio_id}",
                                     {2: (2, "usd"), 3: (2, "usd"), 4: (2, "usd"),
                                      5: (2, "usd"), 6: (2, "percent")})
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(7, 210)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.sortItems(5, Qt.DescendingOrder)
        self.table.fit_header_widths()
        self.table.remember_layout(db, f"position_return_layout_{portfolio_id}")

        details = QWidget()
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        detail_layout.setSpacing(5)
        self.detail_title = widgets.section_title("Select an instrument to inspect its lots and income")
        detail_layout.addWidget(self.detail_title)
        self.details_table = ReportTable(0, 14)
        self.details_table.setHorizontalHeaderLabels((
            "Entry", "Date", "Sell date", "Contract / description", "Detail", "Quantity",
            "Opening/purchase USD", "Realized USD", "Unrealized USD", "Income/accrual USD",
            "Original amount", "Currency", "FX to USD", "Transaction ID"))
        self.details_table.configure_numbers(db, f"position_return_detail_decimals_{portfolio_id}",
                                             {5: (2, "plain"), 6: (2, "usd"),
                                              7: (2, "usd"), 8: (2, "usd"),
                                              9: (2, "usd"), 10: (2, "plain"),
                                              12: (6, "plain")})
        self.details_table.verticalHeader().setVisible(False)
        self.details_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.details_table.horizontalHeader().setStretchLastSection(True)
        self.details_table.setColumnWidth(3, 200)
        self.details_table.setColumnWidth(4, 135)
        self.details_table.setColumnWidth(13, 170)
        self.details_table.sortItems(1, Qt.DescendingOrder)
        self.details_table.fit_header_widths()
        self.details_table.remember_layout(db, f"position_return_detail_layout_{portfolio_id}")
        detail_layout.addWidget(self.details_table, 1)
        self.splitter = widgets.pane_splitter(
            db, self._split_key, self.table, details, layout_combo=self.layout_combo,
            description="returns and details")
        layout.addWidget(self.splitter, 1)
        self.table.horizontalHeaderItem(6).setToolTip(
            "Total return divided by all purchase cash outflows, including commissions "
            "and bond accrued interest paid; cumulative, not annualized. "
            "Unavailable for short positions.")

    def refresh(self, portfolio_rows=None, lots=None):
        portfolio_rows = portfolio_rows if portfolio_rows is not None else self.service.portfolio_rows()
        lots = lots if lots is not None else self.service.lots_rows(portfolio_rows)
        self.rows, self.unattributed = position_return_rows(
            self.db, self.portfolio_id, portfolio_rows, lots)
        self._filter()

    def _filter(self, *_):
        query = self.search.text().strip().casefold()
        mode = self.show.currentText()
        security_type = self.type_filter.currentData()
        visible = [row for row in self.rows
                   if (not query or query in row["name"].casefold() or
                       query in row["symbol"].casefold() or
                       query in str(row["con_id"] or "")) and
                   (security_type == "All" or row["security_type"] == security_type) and
                   (mode == "All" or mode == "Closed" and row["status"] == "Closed" or
                    mode == "Currently held" and row["status"] != "Closed")]
        selected = self._selected_id() or self._preferred_id
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(visible))
        for index, row in enumerate(visible):
            name = SortItem(row["name"] or row["symbol"] or str(row["con_id"]),
                            (row["name"] or row["symbol"]).casefold())
            name.setData(Qt.UserRole, row["position_id"])
            self.table.setItem(index, 0, name)
            self.table.setItem(index, 1, SortItem(row["status"]))
            for column, key in ((2, "realized"), (3, "unrealized"), (4, "income"),
                                (5, "total"), (6, "percent")):
                item = self.table.numeric_item(column, row[key])
                if column in (5, 6) and row["partial"] and row[key] is not None:
                    item.setText("≈ " + item.text())
                    item.setToolTip(row["return_note"])
                self._color_item(item, row[key])
                self.table.setItem(index, column, item)
            valuation_note = (row["price_source"] if row["status"] != "Closed" else "")
            baseline_status = (f"Estimated since {row['estimated_baseline']}"
                               if row.get("estimated_baseline") else "")
            status = ("; ".join(filter(None, ("Partial return" if row["partial"] else "",
                                                 baseline_status, *row["issues"])))
                      if row["issues"] else
                      (baseline_status or "Complete") +
                      (f" · {valuation_note}" if valuation_note else ""))
            item = SortItem(status)
            item.setToolTip(status + (f"\nValuation: {row['price_source']} {row['last_updated']}"
                                      if row["status"] != "Closed" else ""))
            self.table.setItem(index, 7, item)
        self.table.setSortingEnabled(True)
        target = selected if any(r["position_id"] == selected for r in visible) else (
            visible[0]["position_id"] if visible else None)
        if target is not None:
            for index in range(self.table.rowCount()):
                if self.table.item(index, 0).data(Qt.UserRole) == target:
                    self.table.selectRow(index)
                    break
        else:
            self.table.clearSelection()
            self._fill_details(None)
        for label, key in (("Realized P&L", "realized"), ("Unrealized P&L", "unrealized"),
                           ("Income and accruals", "income"), ("Total return", "total")):
            values = [row[key] for row in visible if row[key] is not None]
            complete = len(values) == len(visible)
            value = money(sum(values)) if values and (key == "total" or complete) else "—"
            if key == "total" and value != "—" and (
                    not complete or any(row["partial"] for row in visible)):
                value = "≈ " + value
            self.stats[label].setText(value)
            self.stats[label].setToolTip(
                "Sum of available position totals. Some positions are partial or unavailable; "
                "hover over each row for its limitations."
                if key == "total" and value.startswith("≈") else "")
        incomplete = sum(bool(row["issues"]) for row in visible)
        missing_income_fx = sum("Income FX missing" in row["issues"] for row in visible)
        self.scope.setText(f"{len(visible)} instruments · cumulative return in USD")
        if visible:
            notes = [
                "Scope · Since first recorded trade or estimated opening, in USD.",
                f"Coverage · {incomplete} incomplete instrument(s); "
                f"{len(self.unattributed)} unattributed income/tax row(s).",
            ]
            if missing_income_fx:
                notes.append(f"Income FX · {missing_income_fx} instrument(s) have income "
                             "with missing FX; their full return is incomplete.")
            if any(row.get("estimated_baseline") for row in visible):
                notes.append("Estimated opening · CSV closing prices are opening values, "
                             "not purchase cost or tax basis. Earlier gains and losses "
                             "on those shares are excluded.")
            if any(row["partial"] for row in visible):
                notes.append("Partial returns · ≈ sums the available components. Hover "
                             "over a figure for its missing-data limits.")
            notes.append("Full totals · Require matched trades, complete Cash history, "
                         "income FX, and valuation. Open bonds also require current "
                         "accrued interest. — means unavailable.")
            self.note.setText("\n".join(notes))
        else:
            self.note.setText("No instruments match this filter.")
        self.notes_button.set_detail(self.note.text())

    def _selected_id(self):
        selected = self.table.selectedItems()
        return self.table.item(selected[0].row(), 0).data(Qt.UserRole) if selected else None

    def _selected(self):
        position_id = self._selected_id()
        if position_id is not None:
            self._preferred_id = position_id
        self._fill_details(next((row for row in self.rows if row["position_id"] == position_id), None))

    def _fill_details(self, row):
        self.detail_title.setText(
            f"{row['name']} · Contributing lots and income" if row else
            "Select an instrument to inspect its lots and income")
        lots = row["lots"] if row else []
        income = row["income_details"] if row else []
        table = self.details_table
        table.setSortingEnabled(False)
        table.setRowCount(len(lots) + len(income))
        for index, lot in enumerate(lots):
            for column, value in ((0, "Lot"), (1, lot["buy_date"]),
                                  (2, lot.get("sell_date") or ""), (3, lot["ticker"]),
                                  (4, lot["lot_state"])):
                table.setItem(index, column, SortItem(str(value)))
            for column, value in ((5, lot["buy_qty"]),
                                  (6, (lot["buy_value_usd"] + lot["buy_commission"])
                                   if lot.get("buy_value_usd") is not None and
                                      lot.get("buy_commission") is not None else None),
                                  (7, lot.get("realized_net")), (8, lot.get("net_unrealized"))):
                item = table.numeric_item(column, value)
                if column in (7, 8):
                    self._color_item(item, value)
                table.setItem(index, column, item)
        for offset, movement in enumerate(income):
            index = len(lots) + offset
            entry = ("Tax" if movement["kind"] == "Withholding tax" else
                     "Accrual" if movement["income_type"] == "Unpaid accrued interest" else "Income")
            for column, value in ((0, entry),
                                  (1, movement["reported_day"]),
                                  (3, movement["description"]),
                                  (4, movement["income_type"] or movement["kind"]),
                                  (11, movement["currency"]),
                                  (13, movement["transaction_id"])):
                table.setItem(index, column, SortItem(str(value or "")))
            table.setItem(index, 10, table.numeric_item(10, float(movement["amount"])))
            fx = movement["fx_to_usd"] or self.db.fx_rate(
                movement["currency"], movement["reported_day"])
            table.setItem(index, 12, table.numeric_item(12, float(fx) if fx else None))
            income_item = table.numeric_item(9, movement["amount_usd"])
            self._color_item(income_item, movement["amount_usd"])
            table.setItem(index, 9, income_item)
        table.setSortingEnabled(True)

    def _color_item(self, item, value):
        if value is not None and value != 0:
            item.setForeground(QColor(COLORS[self.palette_name][
                "positive" if value > 0 else "negative"]))

    def set_theme(self, theme):
        self.palette_name = theme
        self._filter()

    def view_state(self):
        return {"search": self.search.text(), "show": self.show.currentText(),
                "type": self.type_filter.currentData(),
                "selected": self._selected_id(), "layout": self.layout_combo.currentText()}

    def apply_view_state(self, state):
        if not isinstance(state, dict):
            return
        if isinstance(state.get("selected"), int):
            self._preferred_id = state["selected"]
        if isinstance(state.get("search"), str):
            self.search.setText(state["search"])
        if self.show.findText(state.get("show")) >= 0:
            self.show.setCurrentText(state["show"])
        type_index = self.type_filter.findData(state.get("type"))
        if type_index >= 0:
            self.type_filter.setCurrentIndex(type_index)
        if self.layout_combo.findText(state.get("layout")) >= 0:
            self.layout_combo.setCurrentText(state["layout"])
        self._filter()
