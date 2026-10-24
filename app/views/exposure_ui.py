"""Current exposure inside Analytics."""

from PySide6.QtCharts import QChart, QChartView, QPieSeries
from PySide6.QtCore import QByteArray, QMargins, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QBoxLayout,
                               QComboBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from app.finance.exposure import add_custom_set, custom_sets, exposure_mix, exposure_snapshot
from app.views import widgets
from app.views.exposure_custom_ui import CustomSetDialog
from app.views.metric_summary import MetricInfoButton, MetricSummary, MetricValue
from app.views.toolbar import Toolbar
from app.views.table_ui import ReportTable, SortItem, money as _money, percent
from app.views.ui_theme import COLORS, style_toolbar_dropdown


def _percent(value, nav):
    return percent(value / nav * 100) if value is not None and nav else "—"


class ExposurePanel(QWidget):
    def __init__(self, db, service, portfolio_id, parent=None):
        super().__init__(parent)
        self.db, self.service, self.portfolio_id = db, service, portfolio_id
        self.palette_name = "light"
        self.snapshot = None
        self.calculation_errors = {}
        self.category_filter = None
        self._rendering = False
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "subpage")
        layout.setSpacing(8)
        self.toolbar = Toolbar()
        self.group_combo = style_toolbar_dropdown(QComboBox())
        self.group_combo.addItems(("Sector", "Custom"))
        self.group_combo.currentTextChanged.connect(self._group_changed)
        self.toolbar.add_control(self.group_combo, "Group by")
        self.basis_combo = style_toolbar_dropdown(QComboBox())
        self.basis_combo.addItems(("Adjusted", "Market value"))
        self.basis_combo.setToolTip("Changes the pie, category breakdown, and position % NAV column together.")
        self.basis_combo.currentTextChanged.connect(self._basis_changed)
        self.toolbar.add_control(self.basis_combo, "Exposure")
        self.custom_set_combo = style_toolbar_dropdown(QComboBox())
        self.custom_set_combo.setMinimumWidth(120)
        self.custom_set_combo.setMaximumWidth(170)
        self.custom_set_combo.currentTextChanged.connect(self._custom_set_changed)
        self.custom_controls = self.toolbar.add_control(self.custom_set_combo, "Set")
        self.assign_button = widgets.button("Manage categories", self._assign_category)
        self.assign_controls = self.toolbar.add_control(self.assign_button)
        layout.addWidget(self.toolbar)
        self.summary = MetricSummary(framed=False)
        self.market_metric = MetricValue("Market net · % NAV", info=True)
        self.adjusted_metric = MetricValue("Adjusted net · % NAV", info=True)
        self.summary.set_blocks([self.market_metric, self.adjusted_metric])
        layout.addWidget(self.summary)
        self.status = widgets.hint("")
        layout.addWidget(self.status)
        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.mix_pane = QWidget()
        mix_layout = QVBoxLayout(self.mix_pane)
        mix_layout.setContentsMargins(0, 0, 0, 0)
        mix_layout.setSpacing(5)
        self.chart_card = QWidget()
        self.chart_card.setObjectName("card")
        self.chart_card.setAttribute(Qt.WA_StyledBackground)
        card_layout = QVBoxLayout(self.chart_card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(2)
        self.mix_heading = widgets.section_title("Adjusted exposure mix")
        self.mix_info = MetricInfoButton("Exposure mix")
        heading = QHBoxLayout()
        heading.setSpacing(5)
        heading.addWidget(self.mix_heading)
        heading.addWidget(self.mix_info)
        heading.addStretch()
        card_layout.addLayout(heading)
        self.chart_layout = QBoxLayout(QBoxLayout.LeftToRight)
        self.chart_layout.setSpacing(8)
        self.chart_view = QChartView()
        self.chart_view.setMinimumHeight(165)
        card_layout.addWidget(self.chart_view, 1)
        self.chart_layout.addWidget(self.chart_card, 2)
        self.groups = ReportTable(0, 3)
        self.groups.setHorizontalHeaderLabels(("Category", "Gross USD", "Mix %"))
        self.groups.verticalHeader().setVisible(False)
        self.groups.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        self.groups.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.groups.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.groups.setSelectionMode(QAbstractItemView.SingleSelection)
        self.groups.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.groups.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.groups.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.groups.configure_numbers(db, f"exposure_groups_decimals_{portfolio_id}",
                                      {1: (2, "usd"), 2: (1, "percent")})
        self.groups.setSortingEnabled(True)
        self.groups.sortItems(1, Qt.DescendingOrder)
        self.groups.remember_layout(db, f"exposure_groups_layout_{portfolio_id}")
        self.groups.itemSelectionChanged.connect(self._category_selected)
        self.chart_layout.addWidget(self.groups, 3)
        self.chart_layout.setAlignment(self.groups, Qt.AlignTop)
        mix_layout.addLayout(self.chart_layout, 1)
        self.positions_pane = QWidget()
        positions_layout = QVBoxLayout(self.positions_pane)
        positions_layout.setContentsMargins(0, 0, 0, 0)
        positions_layout.setSpacing(5)
        positions_header = QHBoxLayout()
        self.positions_heading = widgets.section_title("Positions · All categories")
        positions_header.addWidget(self.positions_heading)
        self.clear_filter_button = QPushButton("Clear filter")
        self.clear_filter_button.setObjectName("secondary")
        self.clear_filter_button.clicked.connect(lambda: self._select_category(None))
        positions_header.addWidget(self.clear_filter_button)
        positions_header.addStretch()
        positions_layout.addLayout(positions_header)
        self.positions = ReportTable(0, 7)
        self.positions.setHorizontalHeaderLabels(("Instrument", "Type", "Qty", "Market USD",
                                                   "Adjusted USD", "% NAV", "Category"))
        self.positions.verticalHeader().setVisible(False)
        self.positions.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.positions.horizontalHeader().setStretchLastSection(True)
        self.positions.horizontalHeader().setDefaultSectionSize(125)
        self.positions.setColumnWidth(0, 160)
        self.positions.configure_numbers(db, f"exposure_decimals_{portfolio_id}",
                                         {2: (2, "plain"), 3: (2, "usd"),
                                          4: (2, "usd"), 5: (2, "percent")})
        self.positions.setSortingEnabled(True)
        self.positions.sortItems(4, Qt.DescendingOrder)
        self.positions.setColumnHidden(1, True)
        self.positions.setColumnHidden(2, True)
        positions_layout.addWidget(self.positions, 1)
        self.positions.fit_header_widths()
        self.splitter = widgets.pane_splitter(
            db, f"exposure_splitter_{portfolio_id}", self.mix_pane, self.positions_pane,
            description="exposure mix and positions", sizes=(400, 600))
        layout.addWidget(self.splitter, 1)
        self._load_custom_sets()
        self._group_changed()

    def _load_custom_sets(self, selected=None):
        current = (selected or self.custom_set_combo.currentText()).removeprefix("Custom: ")
        names = custom_sets(self.db, self.portfolio_id)
        self.custom_set_combo.blockSignals(True)
        self.custom_set_combo.clear()
        self.custom_set_combo.addItems(names or ["No sets yet"])
        self.custom_set_combo.setEnabled(bool(names))
        if current in names:
            self.custom_set_combo.setCurrentText(current)
        self.custom_set_combo.blockSignals(False)

    def _selected_custom_set(self):
        name = self.custom_set_combo.currentText()
        return name if name in custom_sets(self.db, self.portfolio_id) else ""

    def _custom_set_changed(self, *_):
        if self.group_combo.currentText() == "Custom":
            self.category_filter = None
            self.refresh()

    def _basis_changed(self, *_):
        self.positions.sortItems(4 if self.basis_combo.currentText() == "Adjusted" else 3,
                                 Qt.DescendingOrder)
        self.render()

    def _group_changed(self, *_):
        group = self.group_combo.currentText()
        self.category_filter = None
        self.custom_controls.setVisible(group == "Custom")
        self.assign_controls.setVisible(group == "Custom")
        self.assign_button.setText("Manage categories" if self._selected_custom_set() else "Create set")
        self.refresh()

    def _new_set(self):
        name, accepted = QInputDialog.getText(self, "New custom set", "Set name")
        if accepted and name.strip():
            name = name.strip()[:60]
            if name in custom_sets(self.db, self.portfolio_id):
                self.notice.setText("That custom set already exists; select it from Group by.")
                return
            add_custom_set(self.db, self.portfolio_id, name)
            self._load_custom_sets(name)
            self.group_combo.setCurrentText("Custom")
            self._group_changed()
            CustomSetDialog(self, name).exec()
            self.refresh()

    def _assign_category(self):
        set_name = self._selected_custom_set()
        if not set_name:
            self._new_set()
        else:
            CustomSetDialog(self, set_name).exec()
            self.refresh()

    def _category_selected(self):
        if self._rendering:
            return
        selected = self.groups.selectedItems()
        self.category_filter = self.groups.item(selected[0].row(), 0).data(Qt.UserRole) if selected else None
        self.positions.clearSelection()
        self.render()

    def set_theme(self, theme):
        self.palette_name = theme
        self.render()

    def view_state(self):
        header = self.positions.horizontalHeader()
        return {
            "group": self.group_combo.currentText(),
            "custom_set": self._selected_custom_set(),
            "basis": self.basis_combo.currentText(),
            "category": self.category_filter,
            "splitter": bytes(self.splitter.saveState().toBase64()).decode("ascii"),
            "positions_header": bytes(header.saveState().toBase64()).decode("ascii"),
            "sort_column": header.sortIndicatorSection(),
            "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
            "decimals": {str(column): self.positions.decimals_for(column)
                         for column in self.positions._number_formats},
        }

    def apply_view_state(self, state):
        if not isinstance(state, dict):
            return
        group = state.get("group")
        legacy_set = group.removeprefix("Custom: ") if isinstance(group, str) and group.startswith("Custom: ") else ""
        if legacy_set:
            group = "Custom"
        if group == "Market cap":
            group = "Sector"
            state = {**state, "category": None}
        if isinstance(group, str) and self.group_combo.findText(group) >= 0:
            self.group_combo.setCurrentText(group)
        selected_set = state.get("custom_set") or legacy_set
        if isinstance(selected_set, str) and self.custom_set_combo.findText(selected_set) >= 0:
            self.custom_set_combo.setCurrentText(selected_set)
        basis = state.get("basis")
        if isinstance(basis, str) and self.basis_combo.findText(basis) >= 0:
            self.basis_combo.setCurrentText(basis)
        category = state.get("category")
        if category is None or isinstance(category, str):
            self.category_filter = category
            self.render()
        for key, widget in (("splitter", self.splitter),
                            ("positions_header", self.positions.horizontalHeader())):
            saved = state.get(key)
            if isinstance(saved, str):
                widget.restoreState(QByteArray.fromBase64(saved.encode("ascii", "ignore")))
        decimals = state.get("decimals")
        if isinstance(decimals, dict):
            for column in self.positions._number_formats:
                places = decimals.get(str(column))
                if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 30:
                    self.positions.set_column_decimals(column, places)
        column = state.get("sort_column")
        if (isinstance(column, int) and not isinstance(column, bool) and
                0 <= column < self.positions.columnCount()):
            self.positions.sortItems(column, Qt.DescendingOrder if state.get("sort_order") == "desc"
                                     else Qt.AscendingOrder)

    def refresh(self, portfolio_rows=None):
        group = self.group_combo.currentText()
        custom_set = self._selected_custom_set() if group == "Custom" else ""
        self.snapshot = exposure_snapshot(self.db, self.service, self.portfolio_id,
                                          custom_set=custom_set, portfolio_rows=portfolio_rows)
        issue_count = len(self.snapshot["issues"])
        notes = []
        if issue_count:
            notes.append(f"Partial exposure: {issue_count} position(s) have missing inputs")
        if self.calculation_errors:
            notes.append(f"{len(self.calculation_errors)} IBKR option calculation error(s)")
        self.notice.setText(" · ".join(notes))
        self.notice.setVisible(bool(notes))
        self.notice.setToolTip("\n".join(self.snapshot["issues"] + [
            f"{con_id}: {message}" for con_id, message in self.calculation_errors.items()]))
        nav = self.snapshot["nav"]
        market = self.snapshot["totals"]["value"]
        adjusted = self.snapshot["totals"]["adjusted"]
        for metric, value, basis in ((self.market_metric, market, "Market net"),
                                     (self.adjusted_metric, adjusted, "Delta-adjusted net")):
            metric.value.setText(_percent(value, nav))
            metric.info.set_detail(
                f"{basis}: {_money(value)} USD\nAccount NAV: {_money(nav)} USD\n"
                "Signed net exposure divided by account NAV.")
        as_of = self.snapshot["as_of"]
        self.status.setText(f"Latest snapshot{' · ' + as_of if as_of else ''} · USD · options use IBKR delta")
        self.render()

    def render(self, *_):
        if self.snapshot is None:
            return
        group = self.group_combo.currentText()
        field = "custom" if group == "Custom" else "sector"
        value_field = "adjusted" if self.basis_combo.currentText() == "Adjusted" else "value"
        self.positions.setHorizontalHeaderLabels(("Instrument", "Type", "Qty", "Market USD",
                                                   "Adjusted USD", f"{self.basis_combo.currentText()} % NAV",
                                                   "Category"))
        self.mix_heading.setText(f"{'Delta-adjusted' if value_field == 'adjusted' else 'Market value'} mix")
        self._render_mix(field, value_field)
        self._render_positions()

    def _render_mix(self, group_field, value_field):
        positions = self.snapshot["positions"]
        no_custom_set = group_field == "custom" and not self._selected_custom_set()
        if no_custom_set:
            positions = []
        mix = exposure_mix(positions, group_field, value_field)
        categories = sorted({row[group_field] for row in positions}, key=str.casefold)
        colors = COLORS[self.palette_name]
        palette = colors["series"]
        color_by_category = {name: (colors["unclassified"] if name == "Unclassified" else
                                    palette[index % len(palette)])
                             for index, name in enumerate(categories)}
        chart = QChart()
        chart.setBackgroundVisible(False)
        chart.setMargins(QMargins(4, 4, 4, 4))
        chart.setTitleFont(self.font())
        chart.legend().hide()
        series = QPieSeries()
        series.setHoleSize(.57)
        series.setPieSize(.84)
        for category, gross in mix["slices"]:
            pie_slice = series.append(category, gross)
            pie_slice.setColor(QColor(color_by_category[category]))
            pie_slice.setBorderColor(QColor(colors["surface"]))
            pie_slice.setBorderWidth(2)
            pie_slice.setExploded(category == self.category_filter)
            pie_slice.clicked.connect(lambda c=category: self._select_category(c))
        chart.addSeries(series)
        if not mix["slices"]:
            chart.setTitle("Create a set to classify positions" if no_custom_set else
                           "No valued positions")
            chart.setTitleBrush(QColor(colors["muted"]))
        self.chart_view.setBackgroundBrush(QColor(colors["surface"]))
        self.chart_view.setFrameShape(QChartView.NoFrame)
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setChart(chart)
        self.mix_info.set_detail(
            "Use Create set to name categories and assign underlyings." if no_custom_set else
            "Share of gross absolute security exposure · cash excluded"
            + (f" · {mix['missing']} unavailable" if mix["missing"] else ""))
        gross_by_category = dict(mix["slices"])
        missing_by_category = {}
        known_by_category = set()
        for row in positions:
            category = row[group_field]
            if row[value_field] is None:
                missing_by_category[category] = missing_by_category.get(category, 0) + 1
            else:
                known_by_category.add(category)
        categories.sort(key=lambda name: (-gross_by_category.get(name, 0), name))
        self._rendering = True
        self.groups.setSortingEnabled(False)
        try:
            self.groups.setRowCount(len(categories))
            for index, category in enumerate(categories):
                missing_count = missing_by_category.get(category, 0)
                gross = (gross_by_category.get(category, 0.0)
                         if category in known_by_category else None)
                share = (gross / mix["gross"] * 100
                         if gross is not None and mix["gross"] else None)
                label = SortItem(f"● {category}", category.casefold())
                label.setForeground(QColor(color_by_category[category]))
                label.setData(Qt.UserRole, category)
                gross_item = self.groups.numeric_item(1, gross)
                share_item = self.groups.numeric_item(2, share)
                for column, item in enumerate((label, gross_item, share_item)):
                    item.setToolTip((f"{category} · {_money(gross)} gross"
                                     + (f" · {missing_count} unavailable"
                                        if missing_count else "")))
                    self.groups.setItem(index, column, item)
            self.groups.setSortingEnabled(True)
            self.groups.clearSelection()
            matched = self.groups.row_for_key(self.category_filter)
            if matched >= 0:
                self.groups.selectRow(matched)
            else:
                self.category_filter = None
        finally:
            self._rendering = False

    def _select_category(self, category):
        self.category_filter = None if self.category_filter == category else category
        self.render()

    def _render_positions(self):
        if self.snapshot is None:
            return
        self.positions_heading.setText(
            f"Positions · {self.category_filter}" if self.category_filter else "Positions · All categories")
        self.clear_filter_button.setVisible(self.category_filter is not None)
        value_field = "adjusted" if self.basis_combo.currentText() == "Adjusted" else "value"
        selected = self.positions.selectedItems()
        selected_under_id = (self.positions.item(selected[0].row(), 0).data(Qt.UserRole)
                             if selected and self.positions.item(selected[0].row(), 0) else None)
        group = self.group_combo.currentText()
        field = "custom" if group == "Custom" else "sector"
        rows = [row for row in self.snapshot["positions"]
                if self.category_filter is None or row[field] == self.category_filter]
        self.positions.setSortingEnabled(False)
        self.positions.setRowCount(len(rows))
        for index, row in enumerate(rows):
            detail = (f"Market: {_money(row['value'])} · Adjusted: {_money(row['adjusted'])}\n"
                      f"Price source: {row['price_source']}\n"
                      f"IBKR delta: {row['delta']} · {row['delta_mode'] or 'unavailable'} "
                      f"({row['delta_as_of'] or 'unavailable'})\n"
                      f"Underlying: {row['underlying_price']} · {row['underlying_mode'] or 'unavailable'} "
                      f"({row['underlying_as_of'] or 'unavailable'})\n"
                      f"{row['issue']}")
            cells = (SortItem(row["symbol"]), SortItem(row["type"]),
                     self.positions.numeric_item(2, row["quantity"]),
                     self.positions.numeric_item(3, row["value"]),
                     self.positions.numeric_item(4, row["adjusted"]),
                     self.positions.numeric_item(5, row[value_field] / self.snapshot["nav"] * 100
                                                 if row[value_field] is not None and self.snapshot["nav"] else None),
                     SortItem(row[field]))
            cells[0].setData(Qt.UserRole, row["under_con_id"])
            for column, item in enumerate(cells):
                item.setToolTip(detail)
                self.positions.setItem(index, column, item)
        self.positions.setSortingEnabled(True)
        if selected_under_id:
            for index in range(self.positions.rowCount()):
                if self.positions.item(index, 0).data(Qt.UserRole) == selected_under_id:
                    self.positions.selectRow(index)
                    break

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.chart_layout.setDirection(QBoxLayout.TopToBottom if self.width() < 610
                                       else QBoxLayout.LeftToRight)
