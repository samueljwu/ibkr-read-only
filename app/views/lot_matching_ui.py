"""Per-instrument manual lot-matching dialog, reachable from the Lots tab.

Buys are fixed on the left as a tree; matched sales (manual or automatic) are
shown as their children. Unmatched sales sit in a list on the right. Dragging
a sale (from the list, or an existing child row) onto a buy pins a manual
match there; the automatic allocator recalculates the remainder live so the
preview always shows the full resulting match set. Nothing is written to the
database until Apply, which validates the whole instrument atomically.
"""

import json
from collections import defaultdict

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from app.views import widgets
from app.views.table_ui import display_number
from app.views.ui_theme import COLORS
from app.views.widgets import enable_label_copy

MIME_TYPE = "application/x-lot-match"


def _origin_label(origin):
    return "Manual" if origin == "manual" else "Auto (short)" if origin == "auto_short" else "Auto"


class _MatchTree(QTreeWidget):
    """Buys (not draggable) with matched-sale children; manual and plain-auto
    rows can both be dragged onto a different buy to pin or re-pin a manual
    match. Auto-short rows (covering a short sale) are read-only and muted,
    per the design decision that short matches are not manually editable."""

    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.setColumnCount(5)
        self.setHeaderLabels(["Trade", "Date", "Quantity", "Price", "Origin"])
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def mimeTypes(self):
        return [MIME_TYPE]

    def mimeData(self, items):
        if not items:
            return None
        data = items[0].data(0, Qt.UserRole) or {}
        if data.get("kind") != "sell" or data.get("origin") == "auto_short":
            return None
        mime = QMimeData()
        mime.setData(MIME_TYPE, json.dumps(data).encode())
        return mime

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint() if hasattr(event, "position") else event.pos())
        data = item.data(0, Qt.UserRole) if item is not None else None
        if data and data.get("kind") == "buy":
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        item = self.itemAt(position)
        target = item.data(0, Qt.UserRole) if item is not None else None
        if not target or target.get("kind") != "buy" or not event.mimeData().hasFormat(MIME_TYPE):
            event.ignore()
            return
        payload = json.loads(bytes(event.mimeData().data(MIME_TYPE)).decode())
        self.dialog.move_match(payload["sell_id"], target["buy_id"],
                               source_buy_id=payload.get("buy_id"), qty=payload.get("qty"))
        event.acceptProposedAction()


class _SaleList(QListWidget):
    """Unmatched sales available to drag onto an eligible buy."""

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)

    def mimeTypes(self):
        return [MIME_TYPE]

    def mimeData(self, items):
        if not items:
            return None
        data = items[0].data(Qt.UserRole) or {}
        mime = QMimeData()
        mime.setData(MIME_TYPE, json.dumps(data).encode())
        return mime


class LotMatchDialog(QDialog):
    def __init__(self, service, position_id, ticker, parent=None, theme="light"):
        super().__init__(parent)
        self.service = service
        self.matching = service.matching
        self.position_id = position_id
        self.theme = theme
        self.setWindowTitle(f"Match lots — {ticker}")
        self.resize(820, 520)
        self.trades, matches = self.matching.position_state(position_id)
        self.manual_matches = [[m["buy_trade_id"], m["sell_trade_id"], m["matched_qty"]]
                               for m in matches if m["origin"] == "manual"]
        self.preview = []
        self._build_ui()
        self.refresh_preview()

    def set_theme(self, theme):
        self.theme = theme
        self._rebuild_views()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "dialog")
        layout.addWidget(widgets.hint(
            "Drag a sale onto a buy to pin a manual match, or drag an existing match to move it. "
            "Automatic matches fill in the remainder and recalculate around your manual choices."))
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hint")
        layout.addWidget(self.hint_label)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(widgets.section_title("Buys and matches"))
        self.tree = _MatchTree(self)
        self.tree.itemDoubleClicked.connect(self._edit_quantity)
        left_layout.addWidget(self.tree)
        buttons_row = QHBoxLayout()
        match_button = widgets.button("Match ->", self._match_clicked, variant="secondary")
        buttons_row.addWidget(match_button)
        unmatch_button = widgets.button("Unmatch", self._unmatch_clicked, variant="secondary")
        buttons_row.addWidget(unmatch_button)
        revert_button = widgets.button("Revert sale to auto", self._revert_clicked, variant="secondary")
        buttons_row.addWidget(revert_button)
        left_layout.addLayout(buttons_row)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(widgets.section_title("Unmatched sales"))
        self.sale_list = _SaleList()
        right_layout.addWidget(self.sale_list)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        button_box = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        button_box.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        widgets.dialog_buttons(self, button_box, primary=self.apply)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        enable_label_copy(self)

    def _hint(self, text):
        self.hint_label.setText(text)

    # -- capacity and mutation -------------------------------------------------

    def _capacity(self, manual_matches, buy_id, sell_id):
        buy_used = sum(q for b, s, q in manual_matches if b == buy_id and s != sell_id)
        sell_used = sum(q for b, s, q in manual_matches if s == sell_id and b != buy_id)
        buy_free = float(self.trades[buy_id]["quantity"]) - buy_used
        sell_free = float(self.trades[sell_id]["quantity"]) - sell_used
        return max(0.0, min(buy_free, sell_free))

    def _try_commit(self, new_manual_matches):
        try:
            preview = self.matching.plan_position(self.position_id, new_manual_matches)
        except ValueError as exc:
            QMessageBox.warning(self, "Match rejected", str(exc))
            return False
        self.manual_matches = new_manual_matches
        self.preview = preview
        self._hint("")
        self._rebuild_views()
        return True

    def assign_match(self, buy_id, sell_id, qty=None):
        """Pin a manual match between an unmatched (or already matched) sale and a buy."""
        return self.move_match(sell_id, buy_id, source_buy_id=None, qty=qty)

    def move_match(self, sell_id, target_buy_id, source_buy_id=None, qty=None):
        """Pin sell_id to target_buy_id, replacing any manual pin at source_buy_id/target_buy_id."""
        if sell_id not in self.trades or target_buy_id not in self.trades:
            self._hint("That trade is not part of this instrument.")
            return False
        new_manual = [m for m in self.manual_matches
                      if not (m[1] == sell_id and m[0] in (source_buy_id, target_buy_id))]
        cap = self._capacity(new_manual, target_buy_id, sell_id)
        chosen = cap if qty is None else max(0.0, min(float(qty), cap))
        if chosen <= 1e-9:
            QMessageBox.warning(self, "Match rejected", "No matching quantity available for that buy.")
            return False
        new_manual.append([target_buy_id, sell_id, chosen])
        return self._try_commit(new_manual)

    def unmatch(self, buy_id, sell_id):
        """Remove exactly one manual (buy, sell) pin; its quantity may return to auto-matching."""
        if not any(m[0] == buy_id and m[1] == sell_id for m in self.manual_matches):
            self._hint("Select a manual match to unmatch.")
            return False
        new_manual = [m for m in self.manual_matches if not (m[0] == buy_id and m[1] == sell_id)]
        return self._try_commit(new_manual)

    def revert_to_auto(self, sell_id):
        """Remove every manual pin for a sale, reverting it fully to automatic matching."""
        new_manual = [m for m in self.manual_matches if m[1] != sell_id]
        if len(new_manual) == len(self.manual_matches):
            self._hint("That sale has no manual matches to revert.")
            return False
        return self._try_commit(new_manual)

    def apply(self):
        pairs = [(buy_id, sell_id, qty) for buy_id, sell_id, qty in self.manual_matches]
        try:
            self.matching.apply_position_matches(self.position_id, pairs)
        except ValueError as exc:
            QMessageBox.warning(self, "Match rejected", str(exc))
            return False
        self.accept()
        return True

    # -- button handlers ---------------------------------------------------

    def _selected_buy_id(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.UserRole) or {}
        if data.get("kind") == "buy":
            return data["buy_id"]
        if data.get("kind") == "sell":
            return data.get("buy_id")
        return None

    def _match_clicked(self):
        sale_item = self.sale_list.currentItem()
        buy_id = self._selected_buy_id()
        if not sale_item or buy_id is None:
            self._hint("Select an unmatched sale and a buy to match it to.")
            return
        data = sale_item.data(Qt.UserRole)
        self.assign_match(buy_id, data["sell_id"])

    def _unmatch_clicked(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if not data or data.get("kind") != "sell":
            self._hint("Select a manual match to unmatch.")
            return
        if data.get("origin") != "manual":
            self._hint("Only manual matches can be unmatched.")
            return
        self.unmatch(data["buy_id"], data["sell_id"])

    def _revert_clicked(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.UserRole) if item is not None else None
        if not data or data.get("kind") != "sell":
            self._hint("Select a match to revert its sale to automatic matching.")
            return
        self.revert_to_auto(data["sell_id"])

    def _edit_quantity(self, item, _column):
        data = item.data(0, Qt.UserRole) or {}
        if data.get("kind") != "sell" or data.get("origin") != "manual":
            return
        without = [m for m in self.manual_matches if not (m[0] == data["buy_id"] and m[1] == data["sell_id"])]
        cap = self._capacity(without, data["buy_id"], data["sell_id"])
        value, accepted = QInputDialog.getDouble(
            self, "Edit matched quantity", "Quantity:", data["qty"], 0, max(cap, data["qty"]), 6)
        if accepted:
            self.assign_match(data["buy_id"], data["sell_id"], value)

    # -- rendering -----------------------------------------------------------

    def refresh_preview(self):
        return self._try_commit(self.manual_matches)

    def _rebuild_views(self):
        self.tree.clear()
        self.sale_list.clear()
        buys = sorted((t for t in self.trades.values() if t["side"] == "BUY"),
                      key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        children_by_buy = defaultdict(list)
        matched_sell_qty = defaultdict(float)
        for buy_id, sell_id, qty, origin in self.preview:
            children_by_buy[buy_id].append((sell_id, qty, origin))
            matched_sell_qty[sell_id] += qty
        for buy in buys:
            item = QTreeWidgetItem([f"BUY {buy['trade_id']}", buy["trade_date"],
                                    display_number(buy["quantity"]), display_number(buy["price"]), ""])
            item.setData(0, Qt.UserRole, {"kind": "buy", "buy_id": buy["id"]})
            item.setFlags(item.flags() & ~Qt.ItemIsDragEnabled)
            children = sorted(children_by_buy.get(buy["id"], ()),
                              key=lambda entry: self.trades[entry[0]]["trade_date"])
            for sell_id, qty, origin in children:
                sell = self.trades[sell_id]
                child = QTreeWidgetItem([f"SELL {sell['trade_id']}", sell["trade_date"],
                                         display_number(qty), display_number(sell["price"]),
                                         _origin_label(origin)])
                child.setData(0, Qt.UserRole, {"kind": "sell", "sell_id": sell_id,
                                               "buy_id": buy["id"], "qty": qty, "origin": origin})
                if origin == "auto_short":
                    # Short-sale matches are not manually editable at all: no
                    # direct-edit, no drag (selectable only, e.g. to inspect).
                    child.setFlags(child.flags() & ~Qt.ItemIsDragEnabled)
                if origin != "manual":
                    # Auto (and auto-short) rows are shown read-only/muted: no
                    # double-click quantity edit. A plain auto row can still be
                    # dragged onto a different buy to override it with a manual pin.
                    for column in range(5):
                        child.setForeground(column, QBrush(QColor(COLORS[self.theme]["muted"])))
                        font = child.font(column)
                        font.setItalic(True)
                        child.setFont(column, font)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        sells = sorted((t for t in self.trades.values() if t["side"] == "SELL"),
                       key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        for sell in sells:
            remaining = float(sell["quantity"]) - matched_sell_qty.get(sell["id"], 0.0)
            if remaining > 1e-9:
                list_item = QListWidgetItem(
                    f"SELL {sell['trade_id']} · {sell['trade_date']} · qty {display_number(remaining)}")
                list_item.setData(Qt.UserRole, {"sell_id": sell["id"], "qty": remaining})
                self.sale_list.addItem(list_item)
