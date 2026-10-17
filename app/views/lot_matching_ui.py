"""Drag a sale onto a purchase to choose its lot within one instrument.

Edits update a full automatic-match preview. The database changes only when the
user saves, using the matching service's existing validation and atomic write.
"""

import json
from collections import defaultdict

from PySide6.QtCore import QMimeData, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHeaderView, QHBoxLayout, QLabel, QMessageBox,
                               QScrollArea, QSplitter, QVBoxLayout, QWidget)

from app.finance.calculations import closed_lot
from app.views import widgets
from app.views.table_ui import ReportTable, SortItem, display_number
from app.views.ui_theme import COLORS, style_toolbar_dropdown
from app.views.widgets import enable_label_copy

EPSILON = 1e-9
SALE_MIME_TYPE = "application/x-lot-match-sale"


def _quantity(value):
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _pnl_money(value):
    if value is None:
        return "—"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _pnl_percent(value):
    return "—" if value is None else f"{value:+.2%}"


def _resize_match_rows(table, column):
    """Keep a sortable parent row tall enough for every visible match line."""
    table.resizeRowsToContents()
    minimum = table.verticalHeader().defaultSectionSize()
    for row in range(table.rowCount()):
        item = table.item(row, column)
        lines = item.text().count("\n") + 1 if item is not None else 1
        table.setRowHeight(row, max(table.rowHeight(row), minimum,
                                    lines * (table.fontMetrics().lineSpacing() + 4) + 6))


def _configure_match_rows(table, column):
    table.setWordWrap(True)
    resize_timer = QTimer(table)
    resize_timer.setSingleShot(True)
    resize_timer.timeout.connect(lambda: _resize_match_rows(table, column))
    header = table.horizontalHeader()
    header.sortIndicatorChanged.connect(lambda *_: resize_timer.start(0))
    header.sectionResized.connect(lambda *_: resize_timer.start(0))


class _SaleTable(ReportTable):
    """One sortable row per sale, with its current purchase matches visible."""

    def __init__(self, dialog):
        super().__init__(0, 6)
        self.dialog = dialog
        self.configure_numbers(dialog.matching.db, "lot_match_sale_decimals_v2",
                               {1: (2, "plain"), 2: (6, "plain"),
                                4: (2, "usd"), 5: (2, "percent")})
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        _configure_match_rows(self, 3)
        self.setSortingEnabled(True)

    def format_number(self, column, value):
        if value is None:
            return "—"
        places = self.decimals_for(column)
        if column == 2:
            return _quantity(value) if places == 6 else f"{value:,.{places}f}"
        if column == 4:
            if places == 2:
                return _pnl_money(value)
            sign = "+" if value > 0 else "-" if value < 0 else ""
            return f"{sign}${abs(value):,.{places}f}"
        if column == 5:
            return _pnl_percent(value) if places == 2 else f"{value:+,.{places}%}"
        return super().format_number(column, value)

    def mimeTypes(self):
        return [SALE_MIME_TYPE]

    def mimeData(self, items):
        if not items:
            return None
        item = self.item(self.row(items[0]), 0)
        sale_id = item.data(Qt.UserRole) if item is not None else None
        if sale_id is None or self.dialog._is_short_sale(sale_id):
            return None
        mime = QMimeData()
        mime.setData(SALE_MIME_TYPE, json.dumps(sale_id).encode("ascii"))
        return mime


class _PurchaseTable(ReportTable):
    """Accept a sale only on an eligible purchase row."""

    def __init__(self, dialog):
        super().__init__(0, 8)
        self.dialog = dialog
        self.configure_numbers(dialog.matching.db, "lot_match_purchase_decimals_v5",
                               {1: (2, "plain"), 2: (6, "plain"), 3: (6, "plain"),
                                6: (2, "usd"), 7: (2, "percent")})
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDropIndicatorShown(True)
        _configure_match_rows(self, 5)
        self.setSortingEnabled(True)

    def format_number(self, column, value):
        if value is None:
            return "—"
        places = self.decimals_for(column)
        if column in (2, 3):
            return _quantity(value) if places == 6 else f"{value:,.{places}f}"
        if column == 6:
            if places == 2:
                return _pnl_money(value)
            sign = "+" if value > 0 else "-" if value < 0 else ""
            return f"{sign}${abs(value):,.{places}f}"
        if column == 7:
            return _pnl_percent(value) if places == 2 else f"{value:+,.{places}%}"
        return super().format_number(column, value)

    def _target(self, event):
        if not event.mimeData().hasFormat(SALE_MIME_TYPE):
            return None
        try:
            sale_id = json.loads(
                bytes(event.mimeData().data(SALE_MIME_TYPE)).decode("ascii"))
        except (TypeError, ValueError):
            return None
        if not isinstance(sale_id, int):
            return None
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        row = self.rowAt(position.y())
        item = self.item(row, 0) if row >= 0 else None
        target = item.data(Qt.UserRole) if item is not None else None
        if target is None or not self.dialog._can_match(sale_id, *target):
            return None
        return sale_id, *target

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(SALE_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._target(event):
            position = event.position().toPoint() if hasattr(event, "position") else event.pos()
            self.selectRow(self.rowAt(position.y()))
            event.acceptProposedAction()
        else:
            self.clearSelection()
            event.ignore()

    def dragLeaveEvent(self, event):
        self.clearSelection()
        event.accept()

    def dropEvent(self, event):
        target = self._target(event)
        self.clearSelection()
        if target is None:
            self.dialog.hint_label.setText("Drop the sale on an eligible purchase row.")
            event.ignore()
        elif self.dialog.move_sale(*target):
            event.acceptProposedAction()
        else:
            event.ignore()


class LotMatchDialog(QDialog):
    def __init__(self, service, position_id, ticker, parent=None, theme="light", sale_id=None):
        super().__init__(parent)
        self.service = service
        self.matching = service.matching
        self.position_id = position_id
        self.theme = theme
        self.setWindowTitle(f"Match lots — {ticker}")
        self.resize(1120, 760)
        self.trades, matches = self.matching.position_state(position_id)
        self.manual_matches = [(m["buy_trade_id"], m["sell_trade_id"], float(m["matched_qty"]))
                               for m in matches if m["origin"] == "manual"]
        self._split_key = f"lot_match_splits_v1_{self.matching.portfolio_id}_{position_id}"
        self.buy_splits, self.line_pins = self._load_split_state()
        self.original_manual = sorted(self.manual_matches)
        self.original_splits = self._state_snapshot()
        self.preview = []
        self._build_ui()
        self.refresh_preview()
        self._select_sale(sale_id)

    def set_theme(self, theme):
        self.theme = theme
        self._render_all()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "dialog")
        layout.addWidget(widgets.hint(
            "Review each sale's matched purchases above, then drag a sale onto a purchase below. "
            "The drop reserves as much of the sale as the purchase can take; automatic matching fills any remainder. "
            "Select a purchase to split it into smaller lines."))

        splitter = QSplitter(Qt.Vertical)
        sales_panel = QWidget()
        sales_layout = QVBoxLayout(sales_panel)
        sales_layout.setContentsMargins(0, 0, 0, 0)
        sales_layout.addWidget(widgets.section_title(
            "Sales · sort a column or drag a sale to a purchase"))
        self.sale_table = _SaleTable(self)
        self.sale_table.setHorizontalHeaderLabels(("Sale", "Sale price", "Quantity",
                                                    "Matched purchases", "P&L $", "P&L %"))
        sale_header = self.sale_table.horizontalHeader()
        sale_header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in ((0, 145), (1, 90), (2, 90), (3, 520), (4, 105), (5, 90)):
            sale_header.resizeSection(column, width)
        self.sale_table.sortItems(0, Qt.AscendingOrder)
        try:
            saved_sales_layout = json.loads(self.matching.db.setting("lot_match_sales_layout_v2") or "null")
        except (TypeError, ValueError):
            saved_sales_layout = None
        self.sale_table.apply_view_state(saved_sales_layout)
        sale_header.setSectionResizeMode(QHeaderView.Interactive)
        self.sale_table.currentCellChanged.connect(lambda *_: self._render_sale())
        sales_layout.addWidget(self.sale_table)
        splitter.addWidget(sales_panel)

        allocation_panel = QWidget()
        allocation_layout = QVBoxLayout(allocation_panel)
        allocation_layout.setContentsMargins(0, 0, 0, 0)
        allocation_layout.addWidget(widgets.section_title("Purchases · drop a sale here"))
        self.sale_detail = QLabel("")
        allocation_layout.addWidget(self.sale_detail)
        self.table = _PurchaseTable(self)
        self.table.setHorizontalHeaderLabels(("Purchase", "Purchase price", "This sale chosen",
                                              "This sale auto", "All matched / can reserve",
                                              "Matched sales", "Est. P&L $", "Est. P&L %"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        for column, width in ((0, 190), (1, 85), (2, 95), (3, 80),
                              (4, 155), (5, 280), (6, 100), (7, 85)):
            header.resizeSection(column, width)
        self.table.sortItems(0, Qt.AscendingOrder)
        try:
            saved_layout = json.loads(self.matching.db.setting("lot_match_purchase_layout_v5") or "null")
        except (TypeError, ValueError):
            saved_layout = None
        self.table.apply_view_state(saved_layout)
        header.setSectionResizeMode(QHeaderView.Interactive)
        allocation_layout.addWidget(self.table)
        split_actions = QHBoxLayout()
        self.split_button = widgets.button("Split selected purchase…", self._split_selected, variant="secondary")
        self.unsplit_button = widgets.button("Remove split", self._unsplit_selected, variant="secondary")
        split_actions.addWidget(self.split_button)
        split_actions.addWidget(self.unsplit_button)
        split_actions.addStretch()
        allocation_layout.addLayout(split_actions)
        self.table.currentCellChanged.connect(lambda *_: self._update_split_buttons())
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        allocation_layout.addWidget(self.summary)
        self.pnl_summary = QLabel("")
        self.pnl_summary.setObjectName("lotMatchPnl")
        allocation_layout.addWidget(self.pnl_summary)
        allocation_layout.addWidget(widgets.hint(
            "Multiple sales may share a purchase line while the final total fits. "
            "This sale shows the selected sale's allocation; All matched / can reserve shows all sales' "
            "matches against the selected sale's reservable capacity. Matched sales lists every sale. "
            "Estimated P&L uses the quantity this line would take on drop, including fees and FX. "
            "Drop on more lines to reserve the sale's remaining quantity."))
        self.reset_button = widgets.button("Reset sale to automatic", self._reset_sale, variant="secondary")
        allocation_layout.addWidget(self.reset_button, alignment=Qt.AlignLeft)
        splitter.addWidget(allocation_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([335, 365])
        layout.addWidget(splitter, 1)

        self.hint_label = widgets.hint("")
        layout.addWidget(self.hint_label)
        footer = QHBoxLayout()
        self.dirty_label = QLabel("")
        footer.addWidget(self.dirty_label)
        footer.addStretch()
        button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = button_box.button(QDialogButtonBox.Save)
        self.save_button.setText("Save changes")
        self.save_button.clicked.connect(self.apply)
        widgets.dialog_buttons(self, button_box)
        button_box.rejected.connect(self.reject)
        footer.addWidget(button_box)
        layout.addLayout(footer)
        enable_label_copy(self)

    def _save_table_layouts(self):
        if not getattr(self, "_table_layouts_saved", False):
            self.matching.db.set_setting("lot_match_sales_layout_v2",
                                         json.dumps(self.sale_table.view_state()))
            self.matching.db.set_setting("lot_match_purchase_layout_v5",
                                         json.dumps(self.table.view_state()))
            self._table_layouts_saved = True

    def done(self, result):
        self._save_table_layouts()
        super().done(result)

    def closeEvent(self, event):
        self._save_table_layouts()
        super().closeEvent(event)

    def _sales(self):
        return sorted((t for t in self.trades.values() if t["side"] == "SELL"),
                      key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))

    def _buys(self):
        return sorted((t for t in self.trades.values() if t["side"] == "BUY"),
                      key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))

    def _parts(self, buy_id, splits=None):
        current = self.buy_splits if splits is None else splits
        return current.get(buy_id, (float(self.trades[buy_id]["quantity"]),))

    @staticmethod
    def _aggregate_pins(pins):
        totals = defaultdict(float)
        for (buy_id, sale_id, _line), qty in pins.items():
            totals[buy_id, sale_id] += qty
        return sorted((buy_id, sale_id, qty) for (buy_id, sale_id), qty in totals.items()
                      if qty > EPSILON)

    def _spread_manual(self, splits):
        """Place existing manual quantities into lines when no saved line map applies."""
        pins = {}
        used = defaultdict(float)
        for buy_id, sale_id, quantity in self.manual_matches:
            remaining = quantity
            for line, capacity in enumerate(self._parts(buy_id, splits)):
                key = (buy_id, line)
                take = min(remaining, max(0.0, capacity - used[key]))
                if take > EPSILON:
                    pin = (buy_id, sale_id, line)
                    pins[pin] = pins.get(pin, 0.0) + take
                    used[key] += take
                    remaining -= take
                if remaining <= EPSILON:
                    break
        return pins

    def _valid_pins(self, pins, splits):
        used = defaultdict(float)
        for (buy_id, sale_id, line), qty in pins.items():
            if (buy_id not in self.trades or sale_id not in self.trades or
                self.trades[buy_id]["side"] != "BUY" or self.trades[sale_id]["side"] != "SELL" or
                line < 0 or line >= len(self._parts(buy_id, splits)) or qty <= 0):
                return False
            used[buy_id, line] += qty
            if used[buy_id, line] > self._parts(buy_id, splits)[line] + EPSILON:
                return False
        expected = defaultdict(float)
        for buy_id, sale_id, qty in self.manual_matches:
            expected[buy_id, sale_id] += qty
        actual = {(buy_id, sale_id): qty for buy_id, sale_id, qty in self._aggregate_pins(pins)}
        return set(expected) == set(actual) and all(
            abs(qty - actual[key]) <= EPSILON for key, qty in expected.items())

    def _load_split_state(self):
        splits = {}
        pins = None
        try:
            saved = json.loads(self.matching.db.setting(self._split_key) or "{}")
            for key, values in saved.get("parts", {}).items():
                buy_id = int(key)
                if (buy_id not in self.trades or self.trades[buy_id]["side"] != "BUY" or
                    not isinstance(values, list) or len(values) < 2):
                    continue
                parts = tuple(float(value) for value in values)
                if (all(value > EPSILON for value in parts) and
                    abs(sum(parts) - float(self.trades[buy_id]["quantity"])) <= 1e-6):
                    splits[buy_id] = parts
            pins = {(int(b), int(s), int(line)): float(qty)
                    for b, s, line, qty in saved.get("pins", [])}
        except (TypeError, ValueError, KeyError, AttributeError):
            pins = None
        if pins is None or not self._valid_pins(pins, splits):
            pins = self._spread_manual(splits)
        return splits, pins

    def _state_snapshot(self):
        return (tuple(sorted(self.buy_splits.items())), tuple(sorted(self.line_pins.items())))

    def _line_allocations(self):
        """Distribute automatic matches into space left by pinned purchase lines."""
        allocations = defaultdict(float)
        used = defaultdict(float)
        for (buy_id, sale_id, line), qty in self.line_pins.items():
            allocations[buy_id, sale_id, line, "manual"] += qty
            used[buy_id, line] += qty
        for buy_id, sale_id, qty, origin in self.preview:
            if origin == "manual":
                continue
            remaining = qty
            for line, capacity in enumerate(self._parts(buy_id)):
                take = min(remaining, max(0.0, capacity - used[buy_id, line]))
                if take > EPSILON:
                    allocations[buy_id, sale_id, line, origin] += take
                    used[buy_id, line] += take
                    remaining -= take
                if remaining <= EPSILON:
                    break
        return allocations

    def _selected_sale_id(self):
        row = self.sale_table.currentRow()
        item = self.sale_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item is not None else None

    def _select_sale(self, sale_id):
        row = self.sale_table.row_for_key(sale_id)
        if row < 0 and self.sale_table.rowCount():
            row = 0
        if row >= 0:
            self.sale_table.setCurrentCell(row, 0)
            self.sale_table.selectRow(row)

    def _is_short_sale(self, sale_id):
        return any(s == sale_id and origin == "auto_short" for _, s, _, origin in self.preview)

    def _available(self, buy_id, sale_id, line=0):
        reserved_elsewhere = sum(q for (b, s, i), q in self.line_pins.items()
                                 if b == buy_id and i == line and s != sale_id)
        return max(0.0, self._parts(buy_id)[line] - reserved_elsewhere)

    def _eligible(self, buy, sale):
        if not self.matching._precedes(buy, sale):
            return False, "Purchase is after this sale"
        if buy["security_type"] != sale["security_type"] or self.matching._gain_per_unit(buy, sale) is None:
            return False, "Instrument, multiplier, or FX is incompatible"
        return True, ""

    def _try_commit(self, new_pins, new_splits=None):
        manual = self._aggregate_pins(new_pins)
        try:
            preview = self.matching.plan_position(self.position_id, manual)
        except ValueError as exc:
            self.hint_label.setText(str(exc))
            return False
        self.buy_splits = self.buy_splits if new_splits is None else new_splits
        self.line_pins = new_pins
        self.manual_matches = manual
        self.preview = preview
        self.hint_label.setText("")
        self._render_all()
        return True

    def _can_match(self, sale_id, buy_id, line=0):
        sale, buy = self.trades.get(sale_id), self.trades.get(buy_id)
        return bool(sale and buy and sale["side"] == "SELL" and buy["side"] == "BUY" and
                    float(sale["quantity"]) > EPSILON and
                    not self._is_short_sale(sale_id) and self._eligible(buy, sale)[0] and
                    0 <= line < len(self._parts(buy_id)) and
                    self._available(buy_id, sale_id, line) > EPSILON)

    def move_sale(self, sale_id, buy_id, line):
        """Reserve the target line from a sale's free quantity, then other chosen lines."""
        if not self._can_match(sale_id, buy_id, line):
            self.hint_label.setText("This purchase cannot take that sale.")
            return False
        pins = dict(self.line_pins)
        target_key = (buy_id, sale_id, line)
        target_used = sum(value for (b, _, i), value in pins.items()
                          if b == buy_id and i == line)
        target_space = max(0.0, self._parts(buy_id)[line] - target_used)
        pinned_sale = sum(value for (_, s, _), value in pins.items() if s == sale_id)
        take = min(target_space, max(0.0, float(self.trades[sale_id]["quantity"]) - pinned_sale))
        remaining_space = target_space - take
        for source_key in sorted(key for key in pins if key[1] == sale_id and key != target_key):
            if remaining_space <= EPSILON:
                break
            moved = min(pins[source_key], remaining_space)
            pins[source_key] -= moved
            if pins[source_key] <= EPSILON:
                del pins[source_key]
            take += moved
            remaining_space -= moved
        if take <= EPSILON:
            self.hint_label.setText("This purchase line has no room for more of that sale.")
            return False
        pins[target_key] = pins.get(target_key, 0.0) + take
        if not self._try_commit(pins):
            return False
        self._select_sale(sale_id)
        return True

    def _reset_sale(self):
        sale_id = self._selected_sale_id()
        if sale_id is not None:
            self._try_commit({key: qty for key, qty in self.line_pins.items() if key[1] != sale_id})

    def _selected_buy_id(self):
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        target = item.data(Qt.UserRole) if item is not None else None
        return target[0] if target else None

    def _update_split_buttons(self):
        buy_id = self._selected_buy_id()
        self.split_button.setEnabled(buy_id is not None and float(self.trades[buy_id]["quantity"]) >= 0.000002)
        self.unsplit_button.setEnabled(buy_id is not None and len(self._parts(buy_id)) > 1)

    def _replace_buy_parts(self, buy_id, parts):
        total = float(self.trades[buy_id]["quantity"])
        parts = tuple(float(qty) for qty in parts)
        if (not parts or any(qty <= EPSILON for qty in parts) or
            abs(sum(parts) - total) > 1e-6):
            self.hint_label.setText("Split quantities must add up to the purchase quantity.")
            return False
        new_splits = dict(self.buy_splits)
        if len(parts) == 1:
            new_splits.pop(buy_id, None)
        else:
            new_splits[buy_id] = parts
        pins = {key: qty for key, qty in self.line_pins.items() if key[0] != buy_id}
        used = [0.0] * len(parts)
        for b, sale_id, qty in self.manual_matches:
            if b != buy_id:
                continue
            remaining = qty
            for line, capacity in enumerate(parts):
                take = min(remaining, max(0.0, capacity - used[line]))
                if take > EPSILON:
                    pins[buy_id, sale_id, line] = pins.get((buy_id, sale_id, line), 0.0) + take
                    used[line] += take
                    remaining -= take
                if remaining <= EPSILON:
                    break
        return self._try_commit(pins, new_splits)

    def _split_selected(self):
        buy_id = self._selected_buy_id()
        if buy_id is None:
            return
        buy = self.trades[buy_id]
        total = float(buy["quantity"])
        dialog = QDialog(self)
        dialog.setWindowTitle("Split purchase")
        layout = QVBoxLayout(dialog)
        widgets.apply_layout(layout, "dialog")
        form = QFormLayout()
        current_parts = self._parts(buy_id)
        count = style_toolbar_dropdown(QComboBox())
        max_lines = max(len(current_parts), min(100, int(total / 0.000001)))
        count.addItems(str(number) for number in range(2, max(2, max_lines) + 1))
        count.setCurrentText(str(max(2, len(current_parts))))
        form.addRow("Number of lines", count)
        layout.addLayout(form)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        line_container = QWidget()
        line_form = QFormLayout(line_container)
        scroll.setWidget(line_container)
        layout.addWidget(scroll)
        summary = widgets.hint("")
        layout.addWidget(summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        confirm = buttons.button(QDialogButtonBox.Ok)
        confirm.setText("Split purchase")
        widgets.dialog_buttons(dialog, buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        fields = []

        def update_preview():
            entered = sum(field.value() for field in fields)
            difference = total - entered
            valid = all(field.value() > 0 for field in fields) and abs(difference) <= 0.0000005
            confirm.setEnabled(valid)
            if any(field.value() <= 0 for field in fields):
                summary.setText("Each line needs a quantity above zero.")
            elif valid:
                summary.setText(f"Total: {_quantity(total)} across {len(fields)} lines.")
            elif difference > 0:
                summary.setText(f"Entered {_quantity(entered)} of {_quantity(total)}; "
                                f"{_quantity(difference)} remaining.")
            else:
                summary.setText(f"Entered {_quantity(entered)} of {_quantity(total)}; "
                                f"over by {_quantity(-difference)}.")

        def rebuild_lines(initial_parts=None):
            while line_form.rowCount():
                line_form.removeRow(0)
            fields.clear()
            line_count = int(count.currentText())
            if initial_parts is not None:
                quantities = initial_parts
            else:
                each = round(total / line_count, 6)
                quantities = [each] * (line_count - 1)
                quantities.append(round(total - sum(quantities), 6))
            for index, quantity in enumerate(quantities):
                field = QDoubleSpinBox()
                field.setObjectName(f"splitQuantity{index + 1}")
                field.setDecimals(6)
                field.setRange(0, total)
                field.setButtonSymbols(QDoubleSpinBox.NoButtons)
                field.setAlignment(Qt.AlignRight)
                field.setValue(quantity)
                field.valueChanged.connect(update_preview)
                fields.append(field)
                line_form.addRow(f"Line {index + 1} quantity", field)
            scroll.setFixedHeight(min(320, 20 + line_count * 38))
            update_preview()

        count.currentIndexChanged.connect(lambda *_: rebuild_lines())
        rebuild_lines(current_parts if len(current_parts) > 1 else None)
        if dialog.exec() != QDialog.Accepted:
            return
        self._replace_buy_parts(buy_id, [field.value() for field in fields])

    def _unsplit_selected(self):
        buy_id = self._selected_buy_id()
        if buy_id is not None:
            self._replace_buy_parts(buy_id, (float(self.trades[buy_id]["quantity"]),))

    def refresh_preview(self):
        return self._try_commit(self.line_pins)

    def apply(self):
        if sorted(self.manual_matches) == self.original_manual and self._state_snapshot() == self.original_splits:
            return False
        try:
            split_state = json.dumps({
                "parts": {str(key): list(values) for key, values in self.buy_splits.items()},
                "pins": [[*key, qty] for key, qty in sorted(self.line_pins.items())],
            })
            if sorted(self.manual_matches) != self.original_manual:
                self.matching.apply_position_matches(
                    self.position_id, self.manual_matches,
                    settings={self._split_key: split_state})
            else:
                self.matching.db.set_setting(self._split_key, split_state)
        except ValueError as exc:
            QMessageBox.warning(self, "Match rejected", str(exc))
            return False
        self.accept()
        return True

    def _render_all(self):
        selected = self._selected_sale_id()
        matches_by_sale = defaultdict(list)
        for (buy_id, sale_id, line, origin), qty in self._line_allocations().items():
            if qty > EPSILON:
                matches_by_sale[sale_id].append((self.trades[buy_id], line, qty, origin))

        def build_row(sale):
            sale_id = sale["id"]
            sale_date = SortItem(f"{sale['trade_date']} · SELL",
                                 (sale["trade_date"], sale.get("executed_at") or "", sale_id))
            sale_date.setData(Qt.UserRole, sale_id)
            allocations = sorted(matches_by_sale[sale_id],
                                 key=lambda match: (match[0]["trade_date"], match[0]["id"],
                                                    match[1], match[3]))
            descriptions = []
            realized = basis = matched = 0.0
            pnl_complete = True
            for buy, line, qty, origin in allocations:
                parts = self._parts(buy["id"])
                split_label = f" · {line + 1}/{len(parts)}" if len(parts) > 1 else ""
                descriptions.append(
                    f"{buy['trade_date']} @ {display_number(buy['price'])}{split_label} · "
                    f"{_quantity(qty)} {'chosen' if origin == 'manual' else 'short cover' if origin == 'auto_short' else 'auto'}")
                matched += qty
                pnl = self._pnl(buy, sale, qty)
                if pnl is None:
                    pnl_complete = False
                else:
                    realized += pnl["realized_net"]
                    basis += pnl["buy_value_usd"] + pnl["buy_commission"]
            unmatched = max(0.0, float(sale["quantity"]) - matched)
            if unmatched > EPSILON:
                descriptions.append(f"{_quantity(unmatched)} unmatched")
            matched_purchases = SortItem("\n".join(f"↳ {description}" for description in descriptions)
                                         if descriptions else "—")
            matched_purchases.setToolTip("\n".join(descriptions) if descriptions else "No purchases matched")
            pnl_value = realized if pnl_complete and matched > EPSILON else None
            pnl_pct = pnl_value / basis if pnl_value is not None and basis else None
            cells = [sale_date, self.sale_table.numeric_item(1, sale["price"]),
                     self.sale_table.numeric_item(2, sale["quantity"]), matched_purchases,
                     self.sale_table.numeric_item(4, pnl_value),
                     self.sale_table.numeric_item(5, pnl_pct)]
            for column in (4, 5):
                value = cells[column].raw_value
                if value is not None and abs(value) > EPSILON:
                    cells[column].setForeground(QColor(
                        COLORS[self.theme]["positive" if value > 0 else "negative"]))
            for cell in cells:
                if cell is not matched_purchases:
                    cell.setToolTip("Drag this sale onto a purchase to change its match.")
                if self._is_short_sale(sale_id) or float(sale["quantity"]) <= EPSILON:
                    cell.setFlags(cell.flags() & ~Qt.ItemIsDragEnabled)
            return cells

        self.sale_table.blockSignals(True)
        self.sale_table.refill(self._sales(), build_row)
        _resize_match_rows(self.sale_table, 3)
        self._select_sale(selected)
        self.sale_table.blockSignals(False)
        self._render_sale()
        dirty = (sorted(self.manual_matches) != self.original_manual or
                 self._state_snapshot() != self.original_splits)
        self.dirty_label.setText("Unsaved changes" if dirty else "No changes")
        self.save_button.setEnabled(dirty)

    def _render_sale(self):
        sale_id = self._selected_sale_id()
        self.table.refill([], lambda _: [])
        self.sale_detail.setText("")
        self.summary.setText("")
        self.pnl_summary.setText("")
        self.reset_button.setEnabled(False)
        if sale_id is None:
            self.summary.setText("No sales are available for this instrument.")
            self.pnl_summary.setProperty("pnlTrend", "neutral")
            return

        sale = self.trades[sale_id]
        self.sale_detail.setText(
            f"{sale['trade_date']} · SELL · {display_number(sale['price'])} · "
            f"{_quantity(float(sale['quantity']))} shares")
        short_sale = self._is_short_sale(sale_id)
        manual_by_buy = {b: q for b, s, q in self.manual_matches if s == sale_id}
        auto_by_buy = defaultdict(float)
        short_by_buy = defaultdict(float)
        realized = 0.0
        basis = 0.0
        pnl_complete = True
        for buy_id, s, qty, origin in self.preview:
            if s == sale_id:
                if origin == "auto_short":
                    short_by_buy[buy_id] += qty
                elif origin == "auto":
                    auto_by_buy[buy_id] += qty
                pnl = self._pnl(self.trades[buy_id], sale, qty)
                if pnl is None:
                    pnl_complete = False
                else:
                    realized += pnl["realized_net"]
                    basis += pnl["buy_value_usd"] + pnl["buy_commission"]
        manual_total = sum(manual_by_buy.values())
        auto_total = sum(auto_by_buy.values())
        short_qty = sum(short_by_buy.values())
        remaining = max(0.0, float(sale["quantity"]) - manual_total - auto_total - short_qty)
        summary = (
            f"{_quantity(manual_total + auto_total + short_qty)} / "
            f"{_quantity(float(sale['quantity']))} allocated  ·  "
            f"{_quantity(manual_total)} chosen by you  ·  "
            f"{_quantity(auto_total)} automatic")
        if short_qty > EPSILON:
            summary += f"  ·  {_quantity(short_qty)} short cover"
        self.summary.setText(summary + f"  ·  {_quantity(remaining)} unmatched")
        pnl_value = realized if pnl_complete and manual_total + auto_total + short_qty > EPSILON else None
        pnl_pct = realized / basis if pnl_value is not None and basis else None
        self.pnl_summary.setText(
            f"Current matched P&L: {_pnl_money(pnl_value)}  ·  {_pnl_percent(pnl_pct)}")
        trend = "neutral"
        if pnl_value is not None:
            trend = "positive" if pnl_value > EPSILON else "negative" if pnl_value < -EPSILON else "neutral"
        self.pnl_summary.setProperty("pnlTrend", trend)
        self.pnl_summary.style().unpolish(self.pnl_summary)
        self.pnl_summary.style().polish(self.pnl_summary)
        self.reset_button.setEnabled(bool(manual_by_buy))
        if short_sale:
            self.hint_label.setText("Short-sale covers are calculated automatically and cannot be edited here.")
        else:
            self.hint_label.setText("")

        lines = [(buy, line, qty) for buy in self._buys()
                 for line, qty in enumerate(self._parts(buy["id"]))]
        line_allocations = self._line_allocations()
        matches_by_line = defaultdict(list)
        for (buy_id, matched_sale_id, line, origin), qty in line_allocations.items():
            if qty > EPSILON:
                matches_by_line[buy_id, line].append((self.trades[matched_sale_id], qty, origin))

        def build_row(entry):
            buy, line, line_qty = entry
            buy_id = buy["id"]
            available = self._available(buy_id, sale_id, line)
            chosen = line_allocations[buy_id, sale_id, line, "manual"]
            automatic = (line_allocations[buy_id, sale_id, line, "auto"] +
                         line_allocations[buy_id, sale_id, line, "auto_short"])
            eligible, reason = self._eligible(buy, sale)
            if available <= EPSILON and chosen <= EPSILON:
                reason = "Reserved by other manual allocations"
            if short_sale:
                reason = "Short-sale covers are read only"
            can_drop = eligible and not short_sale and available > EPSILON
            caption = f"{buy['trade_date']} · BUY"
            if len(self._parts(buy_id)) > 1:
                caption += f" · {line + 1}/{len(self._parts(buy_id))} · {_quantity(line_qty)}"
            lot = SortItem(caption, (buy["trade_date"], buy.get("executed_at") or "", buy_id, line))
            lot.setData(Qt.UserRole, (buy_id, line))
            lot.setToolTip(caption + f" @ {display_number(buy['price'])}" + ("\n" + reason if reason else
                                      "\nDrop a sale here to reserve this purchase line"))
            pinned_total = sum(q for (_, s, _), q in self.line_pins.items() if s == sale_id)
            other_pinned = sum(q for (b, s, i), q in self.line_pins.items()
                               if s == sale_id and (b, i) != (buy_id, line))
            sale_space = (float(sale["quantity"]) if pinned_total >= float(sale["quantity"]) - EPSILON
                          else float(sale["quantity"]) - other_pinned)
            drop_qty = min(sale_space, available) if can_drop else 0.0
            if short_sale:
                drop_qty = automatic
            projected = self._pnl(buy, sale, drop_qty)
            pnl = projected["realized_net"] if projected else None
            pct = projected["realized_pct"] if projected else None
            all_matches = sorted(matches_by_line[buy_id, line],
                                 key=lambda match: (match[0]["trade_date"], match[0]["id"], match[2]))
            descriptions = [
                f"{matched_sale['trade_date']} @ {display_number(matched_sale['price'])} · "
                f"{_quantity(qty)} {'chosen' if origin == 'manual' else 'short cover' if origin == 'auto_short' else 'auto'}"
                for matched_sale, qty, origin in all_matches]
            cells = [lot, self.table.numeric_item(1, buy["price"])]
            for column, value in ((2, chosen), (3, automatic)):
                cell = self.table.numeric_item(column, value)
                if column == 2 and chosen > EPSILON:
                    cell.setToolTip("Quantity you assigned to this purchase line")
                if column == 3 and automatic > EPSILON:
                    cell.setToolTip("Current automatic allocation for the selected sale")
                    cell.setForeground(QColor(COLORS[self.theme]["accent"]))
                cells.append(cell)
            matched_total = sum(qty for _, qty, _ in all_matches)
            matched_capacity = SortItem(f"{_quantity(matched_total)}/{_quantity(available)}",
                                        (matched_total, available))
            matched_capacity.setToolTip(
                f"{_quantity(matched_total)} shares matched across all sales; "
                f"{_quantity(available)} shares this sale can reserve after other chosen matches. "
                "Automatic matches may move.")
            cells.append(matched_capacity)
            matched_sales = SortItem("\n".join(f"↳ {description}" for description in descriptions)
                                     if descriptions else "—")
            matched_sales.setToolTip("\n".join(descriptions) if descriptions else "No sales matched")
            cells.append(matched_sales)
            for column, value in ((6, pnl), (7, pct)):
                cell = self.table.numeric_item(column, value)
                if value is not None and abs(value) > EPSILON:
                    cell.setForeground(QColor(COLORS[self.theme]["positive" if value > 0 else "negative"]))
                cells.append(cell)
            return cells

        self.table.refill(lines, build_row)
        _resize_match_rows(self.table, 5)
        self._update_split_buttons()

    def _pnl(self, buy, sale, qty):
        if qty <= EPSILON or self.matching._gain_per_unit(buy, sale) is None:
            return None
        result = closed_lot(buy, sale, qty)
        return result if result["realized_net"] is not None else None
