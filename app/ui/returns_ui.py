"""Lifetime returns table and contributing lots inside Analytics."""

from datetime import date, timedelta

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from app.sync.history_status import format_synced_history
from app.finance.returns import returns_rows, returns_summary
from app.ui.table_ui import ReportTable, SortItem, fit_option_name_column
from app.ui.ui_theme import COLORS, TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING, style_toolbar_dropdown


RETURN_HELP = ("Total trading P&L / cumulative purchase cost including buy commissions. "
               "Each purchase counts once, including repeat entries. Short histories have no "
               "purchase-cost percentage. Dividends, interest and withholding are excluded.")


class ReturnsPanel(QWidget):
    def __init__(self, db, service, portfolio_id, parent=None):
        super().__init__(parent)
        self.db, self.service, self.portfolio_id = db, service, portfolio_id
        self.palette_name = "light"
        self.rows = []
        self._selected_key = None
        self._splitter_setting = f"analytics_returns_splitter_{portfolio_id}"
        horizontal = db.setting(f"{self._splitter_setting}_orientation") == "horizontal"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.setSpacing(TOOLBAR_GROUP_SPACING)
        search_controls = QHBoxLayout()
        search_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        search_label = QLabel("Search")
        search_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        search_controls.addWidget(search_label)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Name, ticker or contract")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(32)
        self.search.setMinimumWidth(130)
        self.search.setMaximumWidth(240)
        search_controls.addWidget(self.search)
        controls.addLayout(search_controls)
        filter_controls = QHBoxLayout()
        filter_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        filter_label = QLabel("Show")
        filter_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        filter_controls.addWidget(filter_label)
        self.filter_combo = style_toolbar_dropdown(QComboBox())
        self.filter_combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.filter_combo.addItems(("All", "Currently held", "Fully closed"))
        filter_controls.addWidget(self.filter_combo)
        controls.addLayout(filter_controls)
        controls.addStretch()
        layout_controls = QHBoxLayout()
        layout_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        layout_label = QLabel("Tables")
        layout_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        layout_controls.addWidget(layout_label)
        self.layout_combo = style_toolbar_dropdown(QComboBox())
        self.layout_combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.layout_combo.addItems(("Top and bottom", "Side by side"))
        self.layout_combo.setCurrentIndex(1 if horizontal else 0)
        layout_controls.addWidget(self.layout_combo)
        controls.addLayout(layout_controls)
        layout.addLayout(controls)

        summary = QWidget()
        summary.setObjectName("analyticsSummary")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(12, 8, 12, 8)
        summary_layout.setSpacing(12)
        self.stats = {}
        for label, field in (("Realized P&L", "realized"), ("Unrealized P&L", "unrealized"),
                             ("Total P&L", "total"), ("Return on cost", "return_pct")):
            block = QVBoxLayout()
            block.setSpacing(2)
            caption = QLabel(label)
            caption.setObjectName("metricLabel")
            value = QLabel("—")
            value.setObjectName("metricValue")
            value.setToolTip(RETURN_HELP if field == "return_pct" else
                             "Lifetime trading P&L for the displayed names, after allocated commissions.")
            block.addWidget(caption)
            block.addWidget(value)
            summary_layout.addLayout(block, 1)
            self.stats[field] = value
        layout.addWidget(summary)
        self.history_label = QLabel()
        self.history_label.setObjectName("hint")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)
        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        self.table = self._table(
            ("Name", "Status", "Realized USD", "Unrealized USD", "Total P&L USD",
             "Return on cost %", "Purchase cost USD", "Trades", "Data status"),
            "analytics_returns_decimals", {**{column: (2, "usd") for column in (2, 3, 4, 6)},
                                          5: (2, "percent"), 7: (0, "plain")})
        self.table.sortItems(4, Qt.DescendingOrder)
        for column, width in enumerate((140, 70, 125, 125, 125, 135)):
            self.table.setColumnWidth(column, width)
        self.table.setColumnWidth(7, 70)
        self.table.setColumnWidth(8, 300)
        self.table.hideColumn(6)
        self.table.hideColumn(7)
        self.table.horizontalHeaderItem(5).setToolTip(RETURN_HELP)
        self.table.horizontalHeaderItem(6).setToolTip(
            "Sum of every purchase in USD plus buy commissions; each execution counts once.")
        self.table.horizontalHeader().setToolTip(
            "Right-click a header to show purchase cost and trade count, or adjust decimals.")
        self.detail_heading = QLabel("Select a name to inspect its lots")
        self.detail_heading.setObjectName("helpSection")
        self.detail_heading.setWordWrap(True)
        self.lots_table = self._table(
            ("Lot", "Buy date", "Sell date", "Quantity", "Purchase cost USD",
             "Realized USD", "Unrealized USD", "Data status", "Contract"),
            "analytics_returns_lots_decimals", {3: (2, "plain"),
                                               **{column: (2, "usd") for column in (4, 5, 6)}})
        for column, width in enumerate((110, 100, 100, 75, 125, 125, 125)):
            self.lots_table.setColumnWidth(column, width)
        self.lots_table.setColumnWidth(7, 300)
        self.lots_table.setColumnWidth(8, 240)
        self.lots_table.horizontalHeader().moveSection(8, 0)
        details = QWidget()
        details.setMinimumWidth(0)
        details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(5)
        detail_layout.addWidget(self.detail_heading)
        detail_layout.addWidget(self.lots_table, 1)
        self.splitter = QSplitter(Qt.Horizontal if horizontal else Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(11)
        self.splitter.setMinimumWidth(0)
        self.splitter.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(details)
        self.splitter.setStretchFactor(0, 1 if horizontal else 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.handle(1).setToolTip("Drag to resize names and lot details")
        self._restore_splitter(db.setting(self._splitter_key()), horizontal)
        self.splitter.splitterMoved.connect(lambda *_: self._save_splitter())
        self.layout_combo.currentIndexChanged.connect(lambda index: self._set_split_layout(index == 1))
        layout.addWidget(self.splitter, 1)
        explanation = QLabel("Trading P&L after commissions · Return on cumulative purchase cost")
        explanation.setObjectName("hint")
        explanation.setToolTip(RETURN_HELP)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.search.textChanged.connect(self._render)
        self.filter_combo.currentTextChanged.connect(self._render)
        self.table.currentCellChanged.connect(self._selection_changed)

    def _table(self, headers, setting_key, formats):
        table = ReportTable(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.configure_numbers(self.db, f"{setting_key}_{self.portfolio_id}", formats)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setDefaultSectionSize(145)
        table.setMinimumWidth(0)
        table.setMinimumHeight(110)
        table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        table.setSortingEnabled(True)
        return table

    def _splitter_key(self):
        return (f"{self._splitter_setting}_horizontal" if self.splitter.orientation() == Qt.Horizontal
                else self._splitter_setting)

    def _save_splitter(self):
        self.db.set_setting(self._splitter_key(),
                            bytes(self.splitter.saveState().toBase64()).decode("ascii"))

    def _restore_splitter(self, saved, horizontal):
        restored = bool(saved) and self.splitter.restoreState(
            QByteArray.fromBase64(saved.encode("ascii", "ignore")))
        # Qt's serialized splitter state includes orientation. Keep it aligned
        # with the layout selector, even if an old/corrupt setting disagrees.
        self.splitter.setOrientation(Qt.Horizontal if horizontal else Qt.Vertical)
        self.splitter.setStretchFactor(0, 1 if horizontal else 3)
        self.splitter.setStretchFactor(1, 1)
        if not restored:
            extent = (self.splitter.width() if horizontal else self.splitter.height()) - self.splitter.handleWidth()
            first = max(1, int(extent * (0.5 if horizontal else 0.75)))
            self.splitter.setSizes((first, max(1, extent - first)))

    def _set_split_layout(self, horizontal):
        if (self.splitter.orientation() == Qt.Horizontal) == horizontal:
            return
        self._save_splitter()
        self.splitter.blockSignals(True)
        try:
            self.splitter.setOrientation(Qt.Horizontal if horizontal else Qt.Vertical)
            self._restore_splitter(self.db.setting(self._splitter_key()), horizontal)
        finally:
            self.splitter.blockSignals(False)
        self.db.set_setting(f"{self._splitter_setting}_orientation",
                            "horizontal" if horizontal else "vertical")

    def refresh(self):
        valuations = self.service.portfolio_rows()
        facts = {row["con_id"]: dict(row) for row in self.db.conn.execute(
            "SELECT con_id,under_con_id,under_symbol FROM exposure_facts WHERE portfolio_id=?",
            (self.portfolio_id,))}
        self.rows = returns_rows(self.service.matching.trade_rows(),
                                 self.service.lots_rows(valuations), valuations, underlying_facts=facts)
        first = min((row["first_trade"] for row in self.rows if row["first_trade"]), default="")
        self.history_label.setText(
            f"All imported trades{' · since ' + first if first else ''} · Current valuations · USD")
        self.history_label.setToolTip(format_synced_history(self.db, self.portfolio_id))
        self._history_notice = ""
        account = self.db.setting("ib_account_id").strip()
        if account:
            windows = self.db.conn.execute("""SELECT from_day,to_day,status FROM broker_sync_windows
                WHERE account_id=? AND source='flex' ORDER BY from_day,to_day""", (account,)).fetchall()
            covered_until = None
            gap = False
            for window in windows:
                if window["status"] != "complete":
                    continue
                start, end = date.fromisoformat(window["from_day"]), date.fromisoformat(window["to_day"])
                if covered_until and start > covered_until + timedelta(days=1):
                    gap = True
                covered_until = max(covered_until or end, end)
            self._history_notice = (
                "Trade history has gaps or reports awaiting review; lifetime results may be incomplete."
                if gap or any(window["status"] == "review" for window in windows) else
                "Trade history coverage is unverified; results use the imported executions."
                if not covered_until else "")
        self._render()

    def _fill(self, table, rows, keys=None, tooltips=None, pnl_columns=()):
        header = table.horizontalHeader()
        column, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            for row_index, values in enumerate(rows):
                for col, value in enumerate(values):
                    item = (table.numeric_item(col, value) if col in table._number_formats
                            else SortItem(str(value or ""), str(value or "").casefold()))
                    if col in table._number_formats and value is None:
                        item.sort_value = float("-inf")
                    if keys is not None and col == 0:
                        item.setData(Qt.UserRole, keys[row_index])
                    if tooltips:
                        item.setToolTip(tooltips[row_index])
                    if col in pnl_columns and value is not None:
                        color = COLORS[self.palette_name]["positive" if value > 0 else "negative" if value < 0 else "text"]
                        item.setForeground(QColor(color))
                    if table is self.table and col == 4:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                    table.setItem(row_index, col, item)
            table.setSortingEnabled(True)
            table.sortItems(column, order)
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)

    def _render(self, *_):
        for combo in (self.filter_combo, self.layout_combo):
            combo.ensurePolished()
            combo.setMinimumWidth(combo.sizeHint().width())
        query = self.search.text().strip().casefold()
        status = {"Currently held": "Held", "Fully closed": "Closed"}.get(self.filter_combo.currentText())
        rows = [row for row in self.rows if (query in row["name"].casefold()
                or any(query in contract.casefold() for contract in (*row.get("contracts", ()), *row.get("search_terms", ()))))
                and (status is None or row["status"] == status)]
        totals = returns_summary(rows)
        for field, label in self.stats.items():
            value = totals[field]
            label.setText("—" if value is None else f"{value:+,.2f}%" if field == "return_pct" else f"${value:,.2f}")
            label.setProperty("pnlTrend", "positive" if value is not None and value > 0 else
                              "negative" if value is not None and value < 0 else "")
            label.style().unpolish(label)
            label.style().polish(label)
        messages = [getattr(self, "_history_notice", "")]
        incomplete = sum(row["total"] is None for row in rows)
        if incomplete:
            messages.append(f"{incomplete} name{'s' if incomplete != 1 else ''} incomplete; affected totals show —. See Data status.")
        if not rows:
            messages.append("No names match these filters." if self.rows else "Import trades to see lifetime returns.")
        self.notice.setText(" ".join(message for message in messages if message))
        self.notice.setVisible(bool(self.notice.text()))
        self._fill(self.table, [(row["name"], row["status"], row["realized"], row["unrealized"],
                                 row["total"], row["return_pct"], row["cost"], row["trade_count"],
                                 row["notes"] or row["valuation"] or "Complete") for row in rows],
                   keys=[row["key"] for row in rows],
                   tooltips=["\n".join(part for part in (row["instrument"],
                             ", ".join(row.get("contracts", ())), ", ".join(row.get("search_terms", ())),
                             row["notes"], row["valuation"], RETURN_HELP) if part) for row in rows],
                   pnl_columns=(2, 3, 4, 5))
        index = next((i for i in range(self.table.rowCount())
                      if self.table.item(i, 0).data(Qt.UserRole) == self._selected_key), 0)
        if rows:
            self.table.setCurrentCell(index, 0)
            self._selection_changed(index, 0, -1, -1)
        else:
            self._selected_key = None
            self.detail_heading.setText("Select a name to inspect its lots")
            self.lots_table.setRowCount(0)

    def _selection_changed(self, row, *_):
        if row < 0 or self.table.item(row, 0) is None:
            return
        self._selected_key = self.table.item(row, 0).data(Qt.UserRole)
        selected = next((item for item in self.rows if item["key"] == self._selected_key), None)
        if selected is None:
            return
        self.detail_heading.setText(f"{selected['name']} · Contributing lots")
        cells = []
        for lot in selected["lots"]:
            basis, fee = lot.get("buy_value_usd"), lot.get("buy_commission")
            cost = basis + fee if basis is not None and fee is not None else None
            cells.append((lot["lot_state"], lot.get("buy_date", ""), lot.get("sell_date", ""),
                          lot.get("buy_qty"), cost, lot.get("realized_net"),
                          lot.get("net_unrealized"), lot.get("lot_note", ""),
                          lot.get("contract") or lot.get("ticker") or selected["name"]))
        self._fill(self.lots_table, cells, pnl_columns=(5, 6))
        fit_option_name_column(self.lots_table, 8, (row[8] for row in cells),
                               self.db, f"option_name_width_Returns_contracts_{self.portfolio_id}")

    def view_state(self):
        def table_state(table):
            header = table.horizontalHeader()
            return {"columns": table.columnCount(),
                    "header": bytes(header.saveState().toBase64()).decode("ascii"),
                    "sort_column": header.sortIndicatorSection(),
                    "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
                    "decimals": {str(col): table.decimals_for(col) for col in table._number_formats}}
        return {"search": self.search.text(), "filter": self.filter_combo.currentText(),
                "table": table_state(self.table), "lots_table": table_state(self.lots_table),
                "orientation": "horizontal" if self.splitter.orientation() == Qt.Horizontal else "vertical",
                "splitter": bytes(self.splitter.saveState().toBase64()).decode("ascii")}

    def apply_view_state(self, state):
        if not isinstance(state, dict):
            return
        # Share the same validation used by the other Analytics tables.
        from app.ui.analytics_ui import AnalyticsTab
        for name, table in (("table", self.table), ("lots_table", self.lots_table)):
            saved_table = state.get(name)
            AnalyticsTab._apply_table_view_state(table, saved_table)
            if name == "lots_table" and isinstance(saved_table, dict) and saved_table.get("columns") != table.columnCount():
                # The appended column preserves old column meanings and widths.
                # Put Contract first when restoring an older eight-column view.
                table.showColumn(8)
                table.setColumnWidth(8, 240)
                table.horizontalHeader().moveSection(table.horizontalHeader().visualIndex(8), 0)
        if isinstance(state.get("search"), str):
            self.search.setText(state["search"])
        if isinstance(state.get("filter"), str) and self.filter_combo.findText(state["filter"]) >= 0:
            self.filter_combo.setCurrentText(state["filter"])
        saved = state.get("splitter")
        orientation = state.get("orientation")
        if orientation in ("horizontal", "vertical") or isinstance(saved, str):
            # Saved Returns views from before the layout selector were vertical.
            horizontal = orientation == "horizontal"
            self.layout_combo.setCurrentIndex(1 if horizontal else 0)
            self._restore_splitter(saved if isinstance(saved, str) else "", horizontal)
            self._save_splitter()
            self.db.set_setting(f"{self._splitter_setting}_orientation",
                                "horizontal" if horizontal else "vertical")

    def set_theme(self, theme):
        self.palette_name = theme
        self._render()
