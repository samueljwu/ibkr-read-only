"""Native portfolio monitor UI."""

from datetime import date, datetime, timedelta
from functools import partial
from html import escape
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QByteArray, QItemSelectionModel, QModelIndex, QSize, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
                               QGridLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QTabWidget,
                               QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTableView, QVBoxLayout, QWidget)

from app.storage.database import Database
from app.resources import project_directory
from app.imports.activity_statement_trades import (import_activity_statement_trades,
                                           preview_activity_statement_trades)
from app.finance.instrument_names import InstrumentNames
from app.storage.paths import data_root, validate_data_directory
from app.platform.runtime import window_title
from app.sync.workers import worker_launch
from app.storage.secret_store import save_flex_token
from app.sync.sync_service import trade_sync_paused, single_writer_lock
from app.updates.update_handoff import Handoff, handoff_running, setup_running
from app.updates.updater import cleanup_attempt, identity_matches
from app.sync.sync_timing import SyncTiming, duration_text, history_remaining
from app.platform.diagnostics import configure_diagnostics, diagnostic_log_path
from app.sync.history_status import (format_history_details, format_history_summary, process_alive,
                                pause_request_path, read_status, status_path, write_status,
                                current_history_result, reconciliation_status)
from app.finance.calculations import choose_price, valid_price
from app.ui.analytics_ui import AnalyticsTab
from app.ui.cash_ui import CashMovementsTab
from app.ui.calcs_ui import CalcsTab
from app.broker.broker_sync import BrokerIngestor
from app.imports.excel_io import export_workbook
from app.broker.market_data import IBKRMarketDataProvider, MarketDataProvider, MockMarketDataProvider
from app.finance.services import PortfolioService
from app.ui.table_ui import InputReportTable, ReportTable, SortItem, fit_option_name_column, handle_table_shortcut
from app.ui.ui_theme import (COLORS, TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING,
                          stylesheet, status_link, style_toolbar_dropdown)
from app.ui.themed_checkbox import ThemedCheckBox
from app.ui.help_visuals import HelpVisual
from app.ui.header_icons import header_icon
from app.ui.settings_ui import SettingsSection, SettingsStatus
from app.ui.clipboard_ui import add_table_copy_actions, install_clipboard_support


def col(key, label, kind="text"):
    return key, label, kind


def display_number(value):
    return f"{float(value):,.2f}"


PORTFOLIO = [
    col("display_ticker", "Instrument"), col("position_qty", "Quantity", "number"),
    col("average_cost", "Open cost", "price"), col("last_px", "Last price", "price"),
    col("value", "Value USD", "money"), col("daily_pnl", "Daily P&L", "money"),
    col("unrealized_pnl", "Open P&L", "money"), col("unrealized_pct", "Open P&L %", "pct"),
    col("portfolio_pct", "Weight", "pct"),
    col("one_day", "DTD", "pct"), col("mtd", "MTD", "pct"), col("ytd", "YTD", "pct"),
    col("reconciliation", "Reconciliation"), col("data_status", "Data status"),
    col("price_source", "Price source"), col("last_updated", "Updated"),
    # Append the logical column to preserve existing saved sort/header indices.
    col("open_cost_usd", "Total open cost USD", "money"),
]
TRADES = [
    col("trade_date", "Date", "date"), col("display_ticker", "Instrument"),
    col("side", "Side"), col("quantity", "Quantity", "number"),
    col("price", "Price", "price"), col("commission_usd", "Fee/rebate USD", "money"),
    col("gross_usd", "Value USD", "money"), col("match_status", "Status"),
    col("matched_qty", "Matched", "number"), col("remaining_qty", "Remaining", "number"),
    col("valuation_status", "Data status"), col("trade_id", "Reference"),
    col("source", "Origin"),
    # Append logical columns so existing saved sort and header indices stay valid.
    col("last_price", "Last price", "price"),
    col("realized_net", "Realized P&L USD", "money"),
    col("net_unrealized", "Open P&L USD", "money"),
    col("price_source", "Price source"), col("pnl_note", "P&L status"),
    col("realized_pct", "Realized P&L %", "pct"),
    col("unrealized_pct", "Open P&L %", "pct"),
    col("tax_usd", "Tax USD", "money"),
]
LOTS = [
    col("lot_state", "Status"), col("ticker", "Instrument"), col("buy_date", "Bought", "date"),
    col("buy_qty", "Shares", "number"), col("buy_price", "Buy price", "price"),
    col("sell_date", "Sold", "date"), col("sell_price", "Sell price", "price"),
    col("realized_net", "Realized USD", "money"),
    col("net_unrealized", "Open P&L USD", "money"),
    col("lot_note", "Note"),
    col("buy_commission", "Buy fees", "money"), col("sell_commission", "Sell fees", "money"),
    col("duration", "Days held", "number"),
    col("buy_tax", "Buy tax USD", "money"), col("sell_tax", "Sell tax USD", "money"),
]
COLUMNS = {"Portfolio": PORTFOLIO, "Trades": TRADES, "Lots": LOTS}
PRICE_MOVES = ("one_day", "mtd", "ytd")
BAR_COLUMNS = {"Portfolio": ("unrealized_pnl", *PRICE_MOVES),
               "Trades": (), "Lots": ()}
NUMERIC_KINDS = ("number", "price", "money", "ratio", "pct")
DEFAULT_DECIMALS = 2
QUICK_DISPLAY_DECIMALS = 3
MAX_DISPLAY_DECIMALS = 30


class GridHeader(QHeaderView):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.click_modifiers = Qt.NoModifier

    def mouseReleaseEvent(self, event):
        self.click_modifiers = event.modifiers()
        try:
            super().mouseReleaseEvent(event)
        finally:
            self.click_modifiers = Qt.NoModifier


class GridModel(QAbstractTableModel):
    def __init__(self, name, db, service):
        super().__init__()
        self.name, self.db, self.service = name, db, service
        self.theme = "light"
        self.columns = COLUMNS[name]
        self.rows = []
        self.bar_scales = {key: 0.0 for key in BAR_COLUMNS[name]}
        self.column_decimals = {}
        self.filter_text = ""
        self.side_filter = "All"
        self.hide_closed = name == "Portfolio" and self.db.setting("hide_closed_positions") == "1"
        self.lot_filter = "All"
        saved_sort = self.db.setting(f"sort_{name}")
        self.sort_column = int(saved_sort.split(":")[0]) if saved_sort and saved_sort.split(":")[0].isdigit() else 0
        if name == "Portfolio":
            columns_version = self.db.setting("portfolio_columns_version")
            if columns_version not in ("2", "3", "4"):
                # The original layout had IBKR quantity at index 11.
                self.sort_column = (1 if self.sort_column == 11 else
                                    self.sort_column - 1 if self.sort_column > 11 else self.sort_column)
            if columns_version not in ("3", "4"):
                # The next layout had Difference at index 11; Reconciliation takes its place.
                if self.sort_column > 11:
                    self.sort_column -= 1
            if columns_version != "4":
                # Open P&L % is inserted before the former Weight column.
                if self.sort_column >= 7:
                    self.sort_column += 1
                if saved_sort:
                    self.db.set_setting(f"sort_{name}",
                                        f"{self.sort_column}:{saved_sort.partition(':')[2] or 'asc'}")
        self.sort_order = Qt.DescendingOrder if saved_sort.endswith(":desc") else Qt.AscendingOrder
        self.refresh()

    def refresh(self, all_rows=None):
        self.beginResetModel()
        if all_rows is None:
            all_rows = (self.service.portfolio_rows() if self.name == "Portfolio" else
                        self.service.trade_rows() if self.name == "Trades" else
                        self.service.lots_rows())
        needle = self.filter_text.casefold()
        self.rows = [row for row in all_rows if not needle or any(needle in str(row.get(key) or "").casefold()
                     for key in ("display_ticker", "ticker", "raw_display_ticker", "local_symbol", "ib_symbol"))]
        if self.name == "Trades" and self.side_filter != "All":
            self.rows = [row for row in self.rows if row.get("side") == self.side_filter]
        if self.name == "Portfolio" and self.hide_closed:
            self.rows = [row for row in self.rows if abs(float(row.get("position_qty") or 0)) > 1e-9]
        if self.name == "Lots" and self.lot_filter != "All":
            self.rows = [row for row in self.rows if row.get("lot_state", "").startswith(self.lot_filter)]
        self._apply_sort()
        self._update_bar_scales()
        self.endResetModel()

    def _update_bar_scales(self):
        self.bar_scales = {
            key: max((abs(value) for row in self.rows
                      if isinstance(value := row.get(key), (int, float)) and math.isfinite(value)),
                     default=0.0)
            for key in BAR_COLUMNS[self.name]
        }

    def set_filter(self, value):
        self.filter_text = value.strip()
        self.refresh()

    def set_side_filter(self, value):
        if self.name == "Trades":
            self.side_filter = value if value in ("All", "BUY", "SELL") else "All"
            self.refresh()

    def set_hide_closed(self, checked):
        if self.name == "Portfolio":
            self.hide_closed = bool(checked)
            self.db.set_setting("hide_closed_positions", "1" if checked else "0")
            self.refresh()

    def set_lot_filter(self, value):
        self.lot_filter = value
        self.refresh()

    def decimals_for(self, column):
        return self.column_decimals.get(self.columns[column][0], DEFAULT_DECIMALS)

    def set_column_decimals(self, column, decimals):
        if (not 0 <= column < len(self.columns) or self.columns[column][2] not in NUMERIC_KINDS or
            not isinstance(decimals, int) or isinstance(decimals, bool) or
            not 0 <= decimals <= MAX_DISPLAY_DECIMALS):
            raise ValueError("Choose 0–30 decimal places for a numeric column")
        key = self.columns[column][0]
        if decimals == DEFAULT_DECIMALS:
            self.column_decimals.pop(key, None)
        else:
            self.column_decimals[key] = decimals
        if self.rows:
            self.dataChanged.emit(self.index(0, column), self.index(len(self.rows) - 1, column))

    def restore_column_decimals(self, choices):
        self.column_decimals = {}
        if isinstance(choices, dict):
            allowed = {key for key, _, kind in self.columns if kind in NUMERIC_KINDS}
            self.column_decimals = {
                key: places for key, places in choices.items()
                if key in allowed and isinstance(places, int) and not isinstance(places, bool)
                and 0 <= places <= MAX_DISPLAY_DECIMALS and places != DEFAULT_DECIMALS
            }
        if self.rows:
            for column, (_, _, kind) in enumerate(self.columns):
                if kind in NUMERIC_KINDS:
                    self.dataChanged.emit(self.index(0, column), self.index(len(self.rows) - 1, column))

    def update_positions(self, ids):
        if self.name != "Portfolio" or not ids:
            return
        updated = {row["id"]: row for row in self.service.portfolio_rows()}
        changed_rows = []
        for number, row in enumerate(self.rows):
            if row["id"] in ids and row["id"] in updated:
                self.rows[number] = updated[row["id"]]
                changed_rows.append(number)
        old_scales = self.bar_scales.copy()
        self._update_bar_scales()
        if changed_rows and self.bar_scales != old_scales:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))
        else:
            for number in changed_rows:
                self.dataChanged.emit(self.index(number, 0), self.index(number, len(self.columns)-1))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            return self.columns[section][1] if orientation == Qt.Horizontal else f"{section + 1:,}"
        if role == Qt.ToolTipRole and orientation == Qt.Horizontal:
            key = self.columns[section][0]
            if self.name == "Trades" and key in ("realized_pct", "unrealized_pct"):
                return ("Net P&L divided by the entry basis of matched or remaining quantity. "
                        "Long basis is purchase cost including entry fees; short basis is opening proceeds after entry fees. "
                        "Multiple matched lots use their combined basis. Non-positive or unavailable basis shows —.")
            if self.name == "Trades" and key == "realized_net":
                return ("Net P&L on matched quantities, after allocated entry and exit fees. "
                        "Each matched gain appears on both its BUY and SELL; do not sum both sides.")
            if self.name == "Trades" and key in ("net_unrealized", "last_price"):
                return ("Open P&L values only remaining quantity, after allocated entry fees. "
                        "Last price uses the current instrument valuation; Price source labels any close or cached fallback.")
            if key == "unrealized_pct":
                return "Open P&L divided by remaining purchase cost in USD, including allocated buy fees."
            if key == "open_cost_usd":
                return ("Total spent buying this position's remaining shares or contracts in USD, "
                        "including allocated entry commissions and taxes, using acquisition FX. "
                        "Sold quantities are excluded.")
            if key in BAR_COLUMNS[self.name]:
                metric = "Price return" if key in PRICE_MOVES else "Open P&L in USD"
                return f"{metric}; bar length is scaled to the largest absolute value shown in this column."
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        key, _, kind = self.columns[index.column()]
        row = self.rows[index.row()]
        value = row.get(key)
        if role == Qt.ToolTipRole:
            if key == "open_cost_usd":
                if value is None:
                    return ("Position quantity differs from broker" if row.get("reconciliation") == "DIFFERENCE" else
                            "Remaining purchase cost unavailable; verify open lots, entry fees and acquisition FX.")
                return self.headerData(index.column(), Qt.Horizontal, Qt.ToolTipRole)
            if self.name == "Trades":
                if key == "last_price":
                    return row.get("last_price_note") or "Price unavailable"
                if key in ("realized_pct", "unrealized_pct"):
                    if value is None:
                        return row.get(f"{key}_note") or "P&L percentage unavailable"
                    return self.headerData(index.column(), Qt.Horizontal, Qt.ToolTipRole)
                if key in ("realized_net", "net_unrealized"):
                    note = row.get("realized_note" if key == "realized_net" else "open_note")
                    if value is None:
                        return note or "P&L unavailable"
                    return ("Net P&L on this execution's matched quantity. The same gain appears on the other execution; do not sum both sides."
                            if key == "realized_net" else
                            "Open P&L on remaining quantity, after allocated entry fees. " + (row.get("last_price_note") or ""))
            if key in ("display_ticker", "ticker"):
                return "\n".join(dict.fromkeys(str(part) for part in
                    (value, row.get("raw_display_ticker"), row.get("local_symbol")) if part))
            if row.get("security_type") == "CASH" and key in ("side", "quantity", "price", "gross_usd", "match_status"):
                pair = str(row.get("display_ticker") or "").split(".")
                if len(pair) == 2:
                    base, quote = pair
                    bought, sold = (base, quote) if row.get("side") == "BUY" else (quote, base)
                    return (f"FX conversion: buy {bought}, sell {sold}. Quantity is {base}; "
                            f"price is {quote} per {base}. Value USD is the USD leg "
                            "before the separately shown fee when the pair contains USD.")
            if value is None:
                return (self.rows[index.row()].get("valuation_status") or
                        self.rows[index.row()].get("lot_note") or
                        self.rows[index.row()].get("data_status") or "Data unavailable")
            return {"gross_usd": "Gross trade value in USD, before commission.",
                    "realized_net": "Realized P&L in USD after allocated buy and sell commissions.",
                    "net_unrealized": "Open P&L in USD after allocated buy commission.",
                    "unrealized_pct": "Open P&L divided by remaining purchase cost in USD, including allocated buy fees.",
                    "match_status": "BUY shares stay open until matched to a sale. SELL shares need eligible buys."}.get(key)
        if role in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.EditRole:
                return "" if value is None else str(value)
            if value is None or value == "":
                if value is None and key in (
                    "gross_usd", "value", "daily_pnl", "unrealized_pnl", "unrealized_pct", "portfolio_pct", "open_cost_usd",
                    "realized_net", "net_unrealized", "last_price", "realized_pct"):
                    return "—"
                return ""
            if kind == "pct":
                return f"{float(value):,.{self.decimals_for(index.column())}%}"
            if kind in ("money", "price", "number", "ratio"):
                if row.get("security_type") == "CASH" and key in ("quantity", "price"):
                    return f"{float(value):,.{max(8, self.decimals_for(index.column()))}f}".rstrip("0").rstrip(".")
                if row.get("security_type") == "CASH" and key == "gross_usd" and 0 < abs(float(value)) < 0.01:
                    return f"{float(value):,.8f}".rstrip("0").rstrip(".")
                return f"{float(value):,.{self.decimals_for(index.column())}f}"
            return str(value)
        if role == Qt.TextAlignmentRole:
            return Qt.AlignRight | Qt.AlignVCenter if kind in ("money", "price", "number", "ratio", "pct") else Qt.AlignLeft | Qt.AlignVCenter
        if role == Qt.ForegroundRole:
            dark = self.theme == "dark"
            if key in ("daily_pnl", "unrealized_pnl", "unrealized_pct", "realized_net", "net_unrealized", "realized_pct") and isinstance(value, (int, float)):
                return QColor(("#52c7a6" if dark else "#137333") if value > 0 else
                              ("#ff8b91" if dark else "#b3261e") if value < 0 else
                              ("#f0f0f2" if dark else "#30343b"))
            if key in ("reconciliation", "data_status", "valuation_status") and value in (
                "DIFFERENCE", "CONTRACT ERROR", "CONTRACT DETAILS REQUIRED",
                "MULTIPLIER REQUIRED", "FX REQUIRED", "VERIFY SECURITY TYPE",
                "NO MARKET DATA PERMISSION", "MOCK PRICE UNAVAILABLE", "UNMATCHED SALE", "NO TRADES",
                "POSITION DIFFERENCE", "SAVED SESSION BAR", "CACHED CLOSE"):
                return QColor("#ffb871" if dark else "#9b4917")
            return QColor("#f0f0f2" if dark else "#1d2b3a")
        return None

    def flags(self, index):
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable if index.isValid() else Qt.NoItemFlags

    def setData(self, index, value, role=Qt.EditRole):
        return False

    def sort(self, column, order=Qt.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old_indexes = self.persistentIndexList()
        identities = [id(self.rows[index.row()]) for index in old_indexes]
        self._apply_sort()
        new_positions = {id(row): number for number, row in enumerate(self.rows)}
        self.changePersistentIndexList(old_indexes, [self.index(new_positions[key], old.column())
                                                     for old, key in zip(old_indexes, identities)])
        self.layoutChanged.emit()
        self.headerDataChanged.emit(Qt.Vertical, 0, max(0, len(self.rows) - 1))

    def _apply_sort(self):
        if self.sort_column >= len(self.columns):
            self.sort_column = 0
        key = self.columns[self.sort_column][0]
        _, _, kind = self.columns[self.sort_column]
        populated = [row for row in self.rows if row.get(key) is not None and row.get(key) != ""]
        missing = [row for row in self.rows if row.get(key) is None or row.get(key) == ""]
        populated.sort(key=lambda row: float(row[key]) if kind in NUMERIC_KINDS
                       else str(row[key]).casefold(), reverse=self.sort_order == Qt.DescendingOrder)
        self.rows = populated + missing


class SignedBarDelegate(QStyledItemDelegate):
    """Show a signed value with a compact zero-centered bar."""

    def paint(self, painter, option, index):
        model = index.model()
        key = model.columns[index.column()][0]
        value = model.rows[index.row()].get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            super().paint(painter, option, index)
            return

        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        label = background.text
        background.text = ""
        # QStyledItemDelegate.paint would repopulate the text and draw it twice.
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, background, painter, option.widget)

        painter.save()
        painter.setClipRect(option.rect)
        bar = option.rect.adjusted(5, option.rect.height() - 7, -5, -3)
        axis = bar.center().x()
        scale = model.bar_scales.get(key, 0.0)
        width = max(1, round(max(0, bar.width() // 2 - 2) * abs(value) / scale)) if scale and value else 0
        dark = model.theme == "dark"
        painter.setPen(QColor("#77777e" if dark else "#aab6bd"))
        painter.drawLine(axis, bar.top() - 1, axis, bar.bottom())
        if width:
            color = QColor("#55c995" if dark else "#318a64") if value > 0 else QColor("#f18383" if dark else "#c55d61")
            painter.fillRect(axis + 1 if value > 0 else axis - width,
                             bar.top(), width, max(2, bar.height()), color)
        painter.setFont(option.font)
        painter.setPen(option.palette.highlightedText().color() if option.state & QStyle.State_Selected else
                       model.data(index, Qt.ForegroundRole))
        painter.drawText(option.rect.adjusted(4, 0, -5, -6), Qt.AlignRight | Qt.AlignVCenter, label)
        painter.restore()


class GridView(QTableView):
    def __init__(self, model):
        super().__init__()
        self.setHorizontalHeader(GridHeader(self))
        self.setModel(model)
        self.setAlternatingRowColors(False)
        self.horizontalHeader().setSectionsClickable(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setSortingEnabled(False)
        self.horizontalHeader().setSectionsMovable(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.verticalHeader().setDefaultSectionSize(25)
        self.setWordWrap(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.selection_axis = "row"
        self.setShowGrid(False)

    def keyPressEvent(self, event):
        if handle_table_shortcut(self, event):
            event.accept()
            return
        super().keyPressEvent(event)


class DisplayAmountSpinBox(QDoubleSpinBox):
    """Show two decimals at rest without rounding the value being edited."""

    def textFromValue(self, value):
        return f"{value:,.{self.decimals()}f}" if self.hasFocus() else display_number(value)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.lineEdit().setText(f"{self.value():,.{self.decimals()}f}")
        self.lineEdit().selectAll()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.lineEdit().setText(display_number(self.value()))


class MatchDialog(QDialog):
    def __init__(self, service, sell, parent=None):
        super().__init__(parent)
        name = (InstrumentNames(service.db, service.portfolio_id).trade(sell)
                if sell.get("security_type") in {"OPT", "FOP"} else sell["display_ticker"])
        self.setWindowTitle(f"Match SELL {sell['trade_id']} — {name}")
        self.resize(700, 350)
        self.inputs = []
        old_matches = {}
        for match in service.db.rows("trade_matches", service.portfolio_id):
            if match["sell_trade_id"] == sell["id"]:
                old_matches[match["buy_trade_id"]] = (
                    old_matches.get(match["buy_trade_id"], 0) + match["matched_qty"])
        remaining = service.remaining()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Sale quantity: {display_number(sell['quantity'])}. Allocate it across earlier buys."))
        from PySide6.QtWidgets import QTableWidgetItem
        table = InputReportTable()
        self.table = table
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(["Buy date", "Reference", "Original qty", "Available qty", "Buy price", "Gain/unit USD", "Allocate"])
        buys = [(buy, remaining[buy["id"]] + old_matches.get(buy["id"], 0))
                for buy in service.db.rows("trades", service.portfolio_id)
                if buy["side"] == "BUY" and buy["position_id"] == sell["position_id"]
                and service._precedes(buy, sell)
                and remaining[buy["id"]] + old_matches.get(buy["id"], 0) > 1e-9]
        buys.sort(key=lambda item: (item[0]["trade_date"], item[0]["id"]))
        table.setRowCount(len(buys))
        for row_number, (buy, free) in enumerate(buys):
            gain = service._gain_per_unit(buy, sell)
            for col_number, value in enumerate((buy["trade_date"], buy["trade_id"], buy["quantity"], free,
                                                buy["price"], gain if gain is not None else "FX needed")):
                item = QTableWidgetItem(display_number(value) if col_number in (2, 3, 4, 5) and
                                        isinstance(value, (int, float)) else str(value))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if col_number == 0:
                    item.setData(Qt.UserRole, buy["id"])
                table.setItem(row_number, col_number, item)
            spin = DisplayAmountSpinBox()
            spin.setMaximum(min(free, sell["quantity"]))
            spin.setDecimals(6)
            spin.setGroupSeparatorShown(True)
            spin.setValue(old_matches.get(buy["id"], 0))
            table.setCellWidget(row_number, 6, spin)
            self.inputs.append((buy["id"], spin))
        if not old_matches:
            for buy_id, qty in service.suggested_max_gain(sell["id"], replace_existing=True):
                next(spin for ident, spin in self.inputs if ident == buy_id).setValue(qty)
        for buy_id, spin in self.inputs:
            table.register_input(spin, buy_id, 6)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        allocation_width = max((spin.sizeHint().width() for _, spin in self.inputs), default=0) + 8
        allocation_width = max(150, allocation_width)
        header.setSectionResizeMode(6, QHeaderView.Interactive)
        table.setColumnWidth(6, allocation_width)
        header.sectionResized.connect(
            lambda column, _old, width: table.setColumnWidth(6, allocation_width)
            if column == 6 and width < allocation_width else None)
        layout.addWidget(table)
        best = QPushButton("Suggest maximum gain")
        def use_best():
            for _, spin in self.inputs:
                spin.setValue(0)
            for buy_id, qty in service.suggested_max_gain(sell["id"], replace_existing=True):
                next(spin for ident, spin in self.inputs if ident == buy_id).setValue(qty)
        best.clicked.connect(use_best)
        layout.addWidget(best)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def allocations(self):
        return [(buy_id, spin.value()) for buy_id, spin in self.inputs if spin.value() > 0]


class MainWindow(QMainWindow):
    DEFAULT_TAB_ORDER = ("Portfolio", "Trades", "Lots", "Cash", "Analytics", "Rebalance")
    TAB_LAYOUT_KEY = "tab_layout_v1"

    def __init__(self, database_path=None):
        super().__init__()
        install_clipboard_support()
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1450, max(640, available.width() - 40)),
                    min(850, max(480, available.height() - 40)))
        self._database_path_override = database_path is not None
        self.db = Database(database_path) if self._database_path_override else Database()
        self._pending_data_directory = None
        self._update_handoff = False
        self._update_close_wait = False
        self._update_dialog = None
        self._update_gate = None
        self._update_process = None
        self.log = configure_diagnostics(self.db.path, "monitor")
        self.log.info("Portfolio Monitor opened; database=%s", self.db.path)
        self.portfolio_id = 1
        self.setWindowTitle(window_title(self.db.portfolio_name(self.portfolio_id)))
        self._show_account_summary = self.db.setting("show_account_summary") != "0"
        self._history_attempt_day = None
        self._last_flex_sync_stamp = self.db.metadata("ibkr_flex_sync_completed")
        self._last_analytics_sync_stamp = self.db.metadata("ibkr_analytics_sync_completed")
        self._sync_process = None
        self._analytics_process = None
        self._cash_process = None
        self._history_process = None
        self._history_status_path = status_path(self.db.path)
        self._history_timer = QTimer(self)
        self._history_timer.timeout.connect(self._check_history_import)
        self._last_analytics_sync_attempt = 0.0
        self._last_cash_sync_day = None
        self._sync_all_queue = []
        self._sync_all_active = False
        self._sync_requested = False
        self._sync_progress_active = False
        self._live_sync_pending = False
        self._live_sync_events = set()
        self._live_sync_started_at = 0.0
        try:
            saved_timing = json.loads(self.db.metadata("sync_timing_v1") or "{}")
        except (ValueError, TypeError):
            saved_timing = {}
        self._sync_timing = SyncTiming(saved_timing)
        self._timed_sync_processes = {}
        self._progress_started_at = None
        self._progress_kind = None
        self._progress_failed = False
        self._progress_finished_at = None
        self._last_gateway_sync_request = None
        self._last_quote_cache_at = 0.0
        self._last_connection_attempt = 0.0
        self._disconnected_since = None
        self._connection_error = ""
        self.market = MockMarketDataProvider()
        self.service = PortfolioService(self.db, self.market, self.portfolio_id)
        self.models = {name: GridModel(name, self.db, self.service) for name in COLUMNS}
        self.views = {}
        self.selection_info = {}
        self.search_fields = {}
        root = QWidget()
        root.setObjectName("central")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 12, 14, 8)
        root_layout.setSpacing(9)
        self.heading = QLabel()
        self.heading.setObjectName("heading")
        heading_row = QHBoxLayout()
        heading_row.addWidget(self.heading)
        heading_row.addStretch()
        self.sync_button = QPushButton()
        self.sync_button.setObjectName("secondary")
        self.sync_button.clicked.connect(self._request_sync)
        heading_row.addWidget(self.sync_button)
        self.history_control_button = QPushButton('Pause import')
        self.history_control_button.setObjectName('secondary')
        self.history_control_button.clicked.connect(self.control_history_import)
        self.history_control_button.hide()
        heading_row.addWidget(self.history_control_button)
        self.help_button = QPushButton()
        self.help_button.setObjectName("secondary")
        self.help_button.clicked.connect(self.open_help)
        heading_row.addWidget(self.help_button)
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("secondary")
        self.settings_button.clicked.connect(self.open_settings)
        heading_row.addWidget(self.settings_button)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("themeToggle")
        self.theme_button.clicked.connect(self.toggle_theme)
        heading_row.addWidget(self.theme_button)
        for button, label in ((self.sync_button, "Sync IBKR"), (self.help_button, "Help"),
                              (self.settings_button, "Settings"), (self.theme_button, "Dark mode")):
            button.setProperty("headerIcon", True)
            button.setFixedSize(32, 32)
            button.setIconSize(QSize(18, 18))
            button.setAccessibleName(label)
            button.setToolTip(label)
        self.sync_request_timer = QTimer(self)
        self.sync_request_timer.setSingleShot(True)
        self.sync_request_timer.timeout.connect(self._run_requested_sync)
        root_layout.addLayout(heading_row)
        summary = QWidget()
        summary.setObjectName("summary")
        self.summary = summary
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(14, 5, 14, 4)
        summary_layout.setSpacing(5)
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(20)
        metrics.setVerticalSpacing(6)
        self.metric_values = {}
        for index, (key, title) in enumerate((("nav", "NET ASSET VALUE"), ("cash", "CASH"),
                                             ("holdings", "HOLDINGS VALUE"), ("daily", "DAILY P&L"),
                                             ("open", "OPEN P&L"), ("realized", "REALIZED P&L"))):
            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(1)
            label = QLabel(title)
            label.setObjectName("metricLabel")
            value = QLabel("—")
            value.setObjectName("metricValue")
            value.setMinimumWidth(0)
            cell_layout.addWidget(label)
            cell_layout.addWidget(value)
            metrics.addWidget(cell, index // 3, index % 3)
            self.metric_values[key] = value
        for column in range(3):
            metrics.setColumnStretch(column, 1)
        summary_layout.addLayout(metrics)
        self.data_notice = QLabel()
        self.data_notice.setObjectName("notice")
        self.data_notice.setWordWrap(True)
        summary_layout.addWidget(self.data_notice)
        root_layout.addWidget(summary)
        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tabs.tabBar().setToolTip("Drag tabs to reorder; use View to save the tab layout")
        root_layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self.sync_progress_label = QLabel("Syncing IBKR…")
        self.sync_progress_label.setObjectName("syncProgressLabel")
        self.sync_progress_bar = QProgressBar()
        self.sync_progress_bar.setObjectName("syncProgressBar")
        self.sync_progress_bar.setRange(0, 0)
        self.sync_progress_bar.setTextVisible(False)
        self.sync_progress_bar.setFixedWidth(130)
        self.sync_progress_bar.setFixedHeight(12)
        self.statusBar().addPermanentWidget(self.sync_progress_label)
        self.statusBar().addPermanentWidget(self.sync_progress_bar)
        self.sync_time_label = QLabel()
        self.sync_time_label.setObjectName("syncTimeLabel")
        self.sync_time_label.setMinimumWidth(130)
        self.sync_time_label.setToolTip("Estimated time for the remaining sync work; broker response times may vary.")
        self.statusBar().addPermanentWidget(self.sync_time_label)
        self.trade_sync_status = QLabel()
        self.trade_sync_status.setObjectName("tradeSyncStatus")
        self.trade_sync_status.setTextFormat(Qt.RichText)
        self.trade_sync_status.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.trade_sync_status.linkActivated.connect(self.show_trade_sync_details)
        self.statusBar().addPermanentWidget(self.trade_sync_status)
        self.history_status_label = QLabel()
        self.history_status_label.setObjectName("historyStatus")
        self.history_status_label.setTextFormat(Qt.RichText)
        self.history_status_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.history_status_label.linkActivated.connect(self.show_history_import_details)
        self.statusBar().addPermanentWidget(self.history_status_label)
        self.sync_progress_label.hide()
        self.sync_progress_bar.hide()
        self.sync_time_label.hide()
        for name in COLUMNS:
            self._make_grid_tab(name)
        self.cash_tab = CashMovementsTab(self.db, self.portfolio_id)
        self.tabs.addTab(self.cash_tab, "Cash")
        self.tabs.tabBar().setTabData(self.tabs.count() - 1, "Cash")
        self.analytics_tab = AnalyticsTab(self.db, self.service, self.portfolio_id)
        self.tabs.addTab(self.analytics_tab, "Analytics")
        self.tabs.tabBar().setTabData(self.tabs.count() - 1, "Analytics")
        self.calcs_tab = CalcsTab(self.db, self.service, self.portfolio_id)
        self.tabs.addTab(self.calcs_tab, "Rebalance")
        self.tabs.tabBar().setTabData(self.tabs.count() - 1, "Rebalance")
        self.tabs.currentChanged.connect(self._refresh_special_tab)
        self._make_settings_tab()
        self._make_help_dialog()
        self._make_menu()
        self.next_tab_shortcut = QShortcut(QKeySequence("Ctrl+Tab"), self)
        self.next_tab_shortcut.setContext(Qt.WindowShortcut)
        self.next_tab_shortcut.activated.connect(lambda: self._step_tab(1))
        self.previous_tab_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        self.previous_tab_shortcut.setContext(Qt.WindowShortcut)
        self.previous_tab_shortcut.activated.connect(lambda: self._step_tab(-1))
        self._restore_columns()
        # A saved default view must not replace the user's latest dragged subtab order.
        analytics_order = self.analytics_tab.section_order()
        self._restore_default_views()
        self.analytics_tab._apply_section_order(analytics_order)
        self.analytics_tab._save_section_order()
        self.restore_tab_layout()
        self._update_summary_visibility()
        self._apply_theme(self.db.setting("ui_theme") or "light")
        self._sync_progress_active = True
        self._start_provider()
        if isinstance(self.market, IBKRMarketDataProvider):
            self._watch_live_sync()
        self.lots_refresh_timer = QTimer(self)
        self.lots_refresh_timer.setSingleShot(True)
        self.lots_refresh_timer.timeout.connect(self._refresh_trade_lots)
        self.calcs_refresh_timer = QTimer(self)
        self.calcs_refresh_timer.setSingleShot(True)
        self.calcs_refresh_timer.timeout.connect(self._refresh_calcs_live)
        self.summary_refresh_timer = QTimer(self)
        self.summary_refresh_timer.setSingleShot(True)
        self.summary_refresh_timer.timeout.connect(self._refresh_summary)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(250)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._periodic_refresh)
        self._set_refresh_interval()
        self._update_status()
        self._queue_configured_syncs()
        self._update_sync_button()
        self._update_history_status()
        if self._history_running():
            self._history_timer.start(1000)

    def _refresh_special_tab(self, index):
        widget = self.tabs.widget(index)
        self._update_summary_visibility()
        if widget is self.calcs_tab or widget is self.cash_tab or widget is self.analytics_tab:
            widget.refresh()

    def _step_tab(self, direction):
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + direction) % count)

    def _update_summary_visibility(self):
        self.summary.setVisible(self._show_account_summary and
                                self.tabs.currentWidget() is not self.calcs_tab)

    def _set_account_summary_visible(self, visible):
        self._show_account_summary = bool(visible)
        self.db.set_setting("show_account_summary", "1" if visible else "0")
        self._update_summary_visibility()

    def _refresh_calcs_live(self):
        if self.tabs.currentWidget() is self.calcs_tab and not self.calcs_tab.updates_paused:
            focus = QApplication.focusWidget()
            editors = (self.calcs_tab.findChildren(QDoubleSpinBox) +
                       self.calcs_tab.findChildren(QComboBox))
            editing = any(editor is not None and
                          focus is not None and (focus is editor or editor.isAncestorOf(focus))
                          for editor in editors)
            if editing:
                # Keep the pending quote refresh until the user leaves the input.
                self.calcs_refresh_timer.start(1000)
            else:
                self.calcs_tab.refresh()

    def _apply_theme(self, theme):
        self.theme = theme if theme in ("light", "dark") else "light"
        self.setStyleSheet(stylesheet(self.theme))
        for visual in self.help_visuals:
            visual.set_theme(self.theme)
        self.analytics_tab.set_theme(self.theme)
        for button, name in ((self.sync_button, "sync"), (self.help_button, "help"),
                             (self.settings_button, "settings"),
                             (self.theme_button, "sun" if self.theme == "dark" else "moon")):
            button.setIcon(header_icon(name, self.theme))
        theme_action = "Switch to light mode" if self.theme == "dark" else "Switch to dark mode"
        self.theme_button.setAccessibleName(theme_action)
        self.theme_button.setToolTip(theme_action)
        for model in self.models.values():
            model.theme = self.theme
            if model.rowCount() and model.columnCount():
                model.dataChanged.emit(model.index(0, 0),
                                       model.index(model.rowCount() - 1, model.columnCount() - 1))
        self._update_status()
        self._update_history_status()

    def toggle_theme(self):
        theme = "light" if self.theme == "dark" else "dark"
        self.db.set_setting("ui_theme", theme)
        self._apply_theme(theme)

    def sync_all(self):
        """Refresh live IBKR state and start every configured Flex catch-up."""
        if self._update_blocked():
            return
        if self._history_running():
            QMessageBox.information(self, "Sync IBKR", "Historical import is running. Wait for it to finish before syncing.")
            return
        self.db.set_metadata('ibkr_flex_paused', '')
        self._progress_failed = False
        self._sync_progress_active = True
        self._live_sync_pending = False
        self._live_sync_events.clear()
        if self.db.setting("provider") == "IBKR":
            previous_market = self.market
            if isinstance(self.market, IBKRMarketDataProvider) and self.market.connected:
                self.market.refresh_live()
                self._watch_live_sync()
            else:
                self.refresh_market(force=True)
            if isinstance(self.market, IBKRMarketDataProvider) and self.market is not previous_market:
                self._watch_live_sync()
        account = self.db.setting("ib_account_id")
        missing = []
        self._sync_all_queue = []
        if not account:
            missing.append("IBKR account")
        else:
            for key, label, launch in (
                    ("flex_query_id", "Trades Flex query", self._launch_flex_sync),
                    ("analytics_flex_query_id", "Analytics Flex query", self._launch_analytics_sync),
                    ("cash_flex_query_id", "Cash Flex query", self._launch_cash_sync)):
                if self.db.setting(key):
                    self._sync_all_queue.append(partial(launch, retry=True)
                                                if key == "flex_query_id" else launch)
                else:
                    missing.append(label)
        self._sync_all_active = bool(self._sync_all_queue)
        self._advance_sync_queue()
        self._update_sync_button()
        if missing:
            self.statusBar().showMessage("Sync: configure " + ", ".join(missing) + " in Settings", 10000)

    def _request_sync(self):
        if self._update_blocked():
            return
        if self._sync_requested:
            return
        self._sync_requested = True
        self._sync_progress_active = True
        self._update_sync_button()
        self.sync_button.repaint()
        self.statusBar().repaint()
        # Give Qt time to paint the disabled button and progress indicator
        # before credential checks and worker startup.
        self.sync_request_timer.start(0)

    def _run_requested_sync(self):
        if self._update_blocked():
            self._sync_requested = False
            return
        try:
            self.sync_all()
        finally:
            self._sync_requested = False
            self._update_sync_button()

    def _queue_configured_syncs(self):
        if self._update_blocked():
            return
        if not self.db.setting("ib_account_id") or self._history_running():
            return
        self._sync_all_queue = [launch for key, launch in (
            ("flex_query_id", self._launch_flex_sync),
            ("analytics_flex_query_id", self._launch_analytics_sync),
            ("cash_flex_query_id", self._launch_cash_sync)) if self.db.setting(key)]
        self._sync_all_active = bool(self._sync_all_queue)
        self._advance_sync_queue()

    def _advance_sync_queue(self):
        if self._update_blocked():
            return
        self._finish_timed_syncs()
        self._record_trade_sync_exit()
        if self._history_running():
            return
        if any(process is not None and process.poll() is None for process in
               (self._sync_process, self._analytics_process, self._cash_process)):
            return
        if not self._sync_all_queue:
            self._sync_all_active = False
            return
        while self._sync_all_queue:
            launch = self._sync_all_queue.pop(0)
            launch(manual=True)
            if any(process is not None and process.poll() is None for process in
                   (self._sync_process, self._analytics_process, self._cash_process)):
                break
        if not self._sync_all_queue and not any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process)):
            self._sync_all_active = False

    def _update_sync_button(self):
        importing = self._history_running()
        running = importing or self._sync_requested or self._live_sync_pending or self._sync_all_active or any(process is not None and process.poll() is None for process in
                      (self._sync_process, self._analytics_process, self._cash_process))
        sync_label = "Importing history…" if importing else "Syncing…" if running else "Sync IBKR"
        self.sync_button.setAccessibleName(sync_label)
        self.sync_button.setEnabled(not running)
        statuses = []
        for label, setting, prefix in (
                ("Trades", "flex_query_id", "ibkr_flex"),
                ("Analytics", "analytics_flex_query_id", "ibkr_analytics"),
                ("Cash", "cash_flex_query_id", "ibkr_cash")):
            if not self.db.setting(setting):
                statuses.append(f"{label}: set its Flex query ID in Settings")
            else:
                status = (self._trade_flex_display_status() if label == "Trades" else
                          self.db.metadata(prefix + "_sync_status") or "not synced")
                completed = self.db.metadata(prefix + "_sync_completed")
                error = self.db.metadata(prefix + "_sync_error")
                cash_check = self.db.cash_reconciliation(self.portfolio_id) if label == "Cash" else None
                coverage = cash_check["period_to"] if cash_check else None
                statuses.append(f"{label}: {status}" + (f" · {completed}" if completed else "") +
                                (f" · reconciled through {coverage}" if coverage else "") +
                                (f" · {error[:120]}" if error else ""))
        self.sync_button.setToolTip(sync_label + "\nRefresh live IBKR data and fetch completed Flex history.\n" +
                                    "\n".join(statuses))
        self._update_sync_progress()

    def _update_sync_progress(self, history_status=None):
        if self._live_sync_pending and time.monotonic() - self._live_sync_started_at > 90:
            self._live_sync_pending = False
            self._finish_live_sync(success=False)
        self._finish_timed_syncs()
        running = [label for label, process in (
            ("Trades", self._sync_process), ("Analytics", self._analytics_process),
            ("Cash", self._cash_process)) if process is not None and process.poll() is None]
        syncing = self._sync_progress_active and (self._sync_requested or self._live_sync_pending or self._sync_all_active or bool(running))
        if history_status is None:
            history_status = self._read_history_status()
        importing = (history_status.get("state") in ("starting", "running") or
                     (self._history_process is not None and self._history_process.poll() is None))
        if importing:
            source = history_status.get("active_source") or history_status.get("source")
            stage = f"{source.title()} history" if source in ("trades", "analytics", "cash") else "history"
            self.sync_progress_label.setText('Pausing after current report…' if history_status.get('pause_requested')
                                             else f"Importing {stage}…")
        elif syncing:
            stage = ("live IBKR data" if self._live_sync_pending else
                     f"{running[0]} history" if running else "IBKR history")
            self.sync_progress_label.setText(f"Syncing {stage}…")
        else:
            self._sync_progress_active = False
        self.sync_progress_label.setVisible(syncing or importing)
        self.sync_progress_bar.setVisible(syncing or importing)
        self._update_sync_time(syncing, importing, running, history_status)

    def _sync_timing_key(self, stage):
        query_key = {"Trades": "flex_query_id", "Analytics": "analytics_flex_query_id",
                     "Cash": "cash_flex_query_id"}.get(stage)
        query = self.db.setting(query_key) if query_key else self.db.setting("ib_host")
        return json.dumps([stage, self.db.setting("ib_account_id"), query])

    def _start_timed_sync(self, stage, process):
        key = self._sync_timing_key(stage)
        self._timed_sync_processes[stage] = (process, key)
        self._sync_timing.start(key, time.monotonic())

    def _save_sync_timing(self):
        self.db.set_metadata("sync_timing_v1", json.dumps(self._sync_timing.durations))

    def _finish_timed_syncs(self):
        for stage, (process, key) in list(self._timed_sync_processes.items()):
            code = process.poll()
            if code is None:
                continue
            del self._timed_sync_processes[stage]
            self._sync_timing.finish(key, time.monotonic(), success=code == 0)
            self._progress_failed |= code != 0
            if code == 0:
                self._save_sync_timing()

    def _finish_live_sync(self, *, success):
        key = getattr(self, "_live_timing_key", None)
        if key is not None:
            self._sync_timing.finish(key, time.monotonic(), success=success)
            self._live_timing_key = None
            self._progress_failed |= not success
            if success:
                self._save_sync_timing()

    def _update_sync_time(self, syncing, importing, running, status):
        now = time.monotonic()
        if syncing or importing:
            kind = "history" if importing else "sync"
            if self._progress_started_at is None or self._progress_kind != kind:
                elapsed = 0
                if importing:
                    try:
                        elapsed = max(0, (datetime.now() - datetime.fromisoformat(status["started_at"])).total_seconds())
                    except (KeyError, TypeError, ValueError):
                        pass
                self._progress_started_at = now - elapsed
                self._progress_kind = kind
                self._progress_finished_at = None
            if importing:
                text = ('Finishing current report…' if status.get('pause_requested')
                        else history_remaining(status, datetime.now()))
            else:
                queued = []
                stages = {"_launch_flex_sync": "Trades", "_launch_analytics_sync": "Analytics",
                          "_launch_cash_sync": "Cash"}
                for launch in self._sync_all_queue:
                    function = launch.func if isinstance(launch, partial) else launch
                    stage = stages.get(getattr(function, "__name__", ""))
                    if stage:
                        queued.append(stage)
                text = self._sync_timing.estimate(
                    getattr(self, "_live_timing_key", None) if self._live_sync_pending else None,
                    [self._sync_timing_key(stage) for stage in running + queued], now)
            self.sync_time_label.setText(text)
            self.sync_time_label.setMinimumWidth(max(130, self.sync_time_label.sizeHint().width()))
            self.sync_time_label.show()
        elif self._progress_started_at is not None:
            elapsed = now - self._progress_started_at
            if self._progress_kind == "history":
                state = status.get("state", "failed")
                source = status.get("source")
                operation = (f"{source.title()} history import" if source in ("trades", "analytics", "cash")
                             else "History import")
                try:
                    elapsed = max(0, (datetime.fromisoformat(status["finished_at"]) -
                                      datetime.fromisoformat(status["started_at"])).total_seconds())
                except (KeyError, TypeError, ValueError):
                    pass
                outcome = ("completed in" if state.startswith("completed") else
                           "paused after" if state == "paused" else "failed after")
            else:
                operation = "IBKR sync"
                outcome = "stopped after" if self._progress_failed else "completed in"
            self.sync_time_label.setText(f"{operation} {outcome} {duration_text(elapsed)}")
            self.sync_time_label.setMinimumWidth(max(130, self.sync_time_label.sizeHint().width()))
            self._progress_started_at = None
            self._progress_finished_at = now
            self._progress_failed = False
        elif self._progress_finished_at is None or now - self._progress_finished_at >= 10:
            self.sync_time_label.hide()

    def _watch_live_sync(self):
        if self._update_blocked():
            return
        self._live_sync_pending = True
        self._live_sync_events.clear()
        self._live_sync_started_at = time.monotonic()
        self._live_timing_key = self._sync_timing_key("Live")
        self._sync_timing.start(self._live_timing_key, self._live_sync_started_at)

    def _history_running(self):
        if self._history_process is not None and self._history_process.poll() is None:
            return True
        return self._read_history_status().get("state") in ("starting", "running")

    def _read_history_status(self):
        status = read_status(self._history_status_path)
        if status.get('state') in ('starting', 'running') and pause_request_path(self._history_status_path).exists():
            status['pause_requested'] = True
        if status.get("state") not in ("starting", "running"):
            return status
        if self._history_process is not None and self._history_process.poll() is None:
            return status
        pid = status.get("pid")
        if pid and process_alive(pid):
            return status
        if status.get("state") == "starting" and not pid:
            try:
                started = datetime.fromisoformat(status["started_at"])
                if datetime.now() - started < timedelta(seconds=30):
                    return status
            except (KeyError, ValueError, TypeError):
                pass
        # The worker may have published its result between our initial read
        # and the process check. Read again after confirming it has exited.
        status = read_status(self._history_status_path)
        if status.get("state") not in ("starting", "running"):
            return status
        status.update(state="failed", pid=None,
                      message=("Import stopped before reporting a result. Diagnostic log: "
                               f"{diagnostic_log_path(self.db.path, 'history-import')}"),
                      finished_at=datetime.now().isoformat(timespec="seconds"))
        try:
            write_status(self._history_status_path, status)
        except OSError as exc:
            self.log.error("Could not record interrupted history import: %s", exc)
        return status

    def _update_history_status(self):
        status = self._read_history_status()
        self._update_history_controls(status)
        self._update_older_trades_button(status)
        state = status.get("state")
        if state:
            label = 'pausing' if status.get('pause_requested') else state.replace('_', ' ')
            self.history_status_label.setText(status_link(
                self.theme, "history", f'History: {label}'))
            self.history_status_label.setToolTip("Click for the latest historical import result")
            self.history_status_label.setCursor(Qt.PointingHandCursor)
        else:
            self.history_status_label.clear()
            self.history_status_label.setToolTip("")
            self.history_status_label.unsetCursor()
        self._update_sync_progress(status)

    def _update_older_trades_button(self, status):
        button = getattr(self, "older_trades_button", None)
        if button is None:
            return
        outside_trades = (status.get("outside_history_windows") or {}).get("trades") or []
        diagnosed = (status.get("state") not in (None, "starting", "running") and
                     bool(outside_trades) and
                     status.get("account_id") == self.db.setting("ib_account_id").strip())
        button.setVisible(diagnosed)

    def _update_history_controls(self, status):
        importing = status.get('state') in ('starting', 'running') or (
            self._history_process is not None and self._history_process.poll() is None)
        resumable = status.get('state') == 'paused' and status.get('paused_by_user')
        pausing = importing and status.get('pause_requested')
        for name in ('history_control_button', 'settings_history_control_button'):
            button = getattr(self, name, None)
            if button is None:
                continue
            button.setVisible(bool(importing or resumable))
            button.setText('Pausing…' if pausing else 'Pause import' if importing else 'Resume import')
            button.setEnabled(bool(status.get('pause_supported') and not pausing) if importing
                              else bool(resumable and not self._flex_sync_running()))
            button.setToolTip('Finish and save the current report, then pause.' if importing and status.get('pause_supported')
                              else 'This running worker predates pause support. Restart the app after it finishes.' if importing
                              else 'Continue the same source and date range, skipping saved history.')

    def control_history_import(self):
        if self._history_running():
            self.pause_history_import()
        else:
            self.resume_history_import()

    def pause_history_import(self):
        status = self._read_history_status()
        if not self._history_running() or not status.get('pause_supported'):
            return
        try:
            pause_request_path(self._history_status_path).touch()
        except OSError as exc:
            QMessageBox.warning(self, 'Pause import', f'Could not request pause: {exc}')
            return
        self._update_history_status()
        self.statusBar().showMessage('Pausing after the current report is saved.', 10000)

    def resume_history_import(self):
        status = self._read_history_status()
        if status.get('state') != 'paused' or not status.get('paused_by_user'):
            return
        if status.get('account_id') != self.db.setting('ib_account_id'):
            QMessageBox.warning(self, 'Resume import', 'The IBKR account changed. Start a new import for this account.')
            return
        try:
            start, end = date.fromisoformat(status['from_date']), date.fromisoformat(status['to_date'])
            source = status['source']
            if source not in ('all', 'trades', 'analytics', 'cash') or start > end or end >= date.today():
                raise ValueError
        except (KeyError, TypeError, ValueError):
            QMessageBox.warning(self, 'Resume import', 'The saved date range is invalid. Start a new import.')
            return
        self._start_history_import(source, start, end)

    def show_history_import_details(self, _link=None):
        status = self._read_history_status()
        if not status:
            return
        status = current_history_result(self.db, status, self.portfolio_id)
        box = QMessageBox(self)
        title, summary = format_history_summary(status)
        box.setWindowTitle("History import result")
        box.setIcon(QMessageBox.Warning if status.get("state") == "failed" else QMessageBox.Information)
        box.setTextFormat(Qt.PlainText)
        box.setText(title)
        box.setInformativeText(summary)
        details = format_history_details(status)
        box.setDetailedText(details)
        box.setStandardButtons(QMessageBox.Ok)
        if status.get('reconciliation_guidance'):
            copy = box.addButton('Copy diagnostics', QMessageBox.ActionRole)
            copy.clicked.connect(lambda: QApplication.clipboard().setText(details))
        box.exec()

    def _flex_sync_running(self):
        return self._sync_all_active or any(
            process is not None and process.poll() is None for process in
            (self._sync_process, self._analytics_process, self._cash_process))

    def _update_account_heading(self):
        account = self.db.setting("ib_account_id")
        self.heading.setText(account or "Account not set")

    def _sync_launch(self, module):
        return worker_launch(module, [self.db.path])

    def _launch_flex_sync(self, *, manual=False, retry=False):
        if self._update_blocked():
            return
        if self._history_running():
            return
        if trade_sync_paused(self.db) and not retry:
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("flex_query_id"):
            if manual:
                QMessageBox.warning(self, "IBKR trade sync", "Set the IBKR account and Flex query ID in Settings first.")
            return
        now = datetime.now()
        if (not manual and self._last_gateway_sync_request is not None and
            now - self._last_gateway_sync_request < timedelta(minutes=15)):
            return
        if self._sync_process is not None and self._sync_process.poll() is None:
            return
        try:
            self.db.set_metadata("ibkr_flex_sync_status", "starting")
            self.db.set_metadata("ibkr_flex_sync_error", "")
            self.db.set_metadata("ibkr_flex_sync_error_code", "")
            self.db.set_metadata("ibkr_flex_sync_stage", "")
            self.db.set_metadata("ibkr_flex_sync_run_id", "")
            self.db.set_metadata("ibkr_flex_sync_started", now.isoformat(timespec="seconds"))
            command, workdir = self._sync_launch("scripts.sync_trades")
            self.log.info("Launching trade sync; manual=%s", manual)
            self._sync_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._start_timed_sync("Trades", self._sync_process)
            if not manual:
                self._last_gateway_sync_request = now
        except OSError as exc:
            self.db.set_metadata("ibkr_flex_sync_status", "failed")
            self.db.set_metadata("ibkr_flex_sync_error", str(exc))
            self._progress_failed = True
            self.db.set_metadata("ibkr_flex_sync_stage", "launching sync process")
            self.db.set_metadata("ibkr_flex_sync_failed_at", datetime.now().isoformat(timespec="seconds"))
            self.log.error("Trade sync launch failed: %s: %s", type(exc).__name__, exc)

    def _record_trade_sync_exit(self):
        process = self._sync_process
        if process is None:
            return
        code = process.poll()
        if code is None:
            return
        self._sync_process = None
        self.log.info("Trade sync process exited with code %s", code)
        if code and self.db.metadata("ibkr_flex_sync_status") in ("starting", "running"):
            self.db.set_metadata("ibkr_flex_sync_status", "failed")
            self.db.set_metadata("ibkr_flex_sync_error", f"Sync process exited with code {code}; check the diagnostic log.")
            self.db.set_metadata("ibkr_flex_sync_stage", "running sync process")
            self.db.set_metadata("ibkr_flex_sync_failed_at", datetime.now().isoformat(timespec="seconds"))
            self.log.error("Trade sync process failed without a recorded sync error; exit code %s", code)

    def _trade_flex_display_status(self):
        if trade_sync_paused(self.db):
            return "paused"
        return self.db.metadata("ibkr_flex_sync_status") or "never"

    def show_trade_sync_details(self, _link=""):
        if self.db.metadata("ibkr_flex_sync_status") != "failed":
            return
        run_id = self.db.metadata("ibkr_flex_sync_run_id")
        if run_id and run_id.isdigit():
            row = self.db.conn.execute("""SELECT id, started_at, finished_at, from_day, to_day, error
                FROM broker_sync_runs WHERE source='flex' AND id=?""", (int(run_id),)).fetchone()
        elif not self.db.metadata("ibkr_flex_sync_started"):
            row = self.db.conn.execute("""SELECT id, started_at, finished_at, from_day, to_day, error
                FROM broker_sync_runs WHERE source='flex' AND status='failed'
                ORDER BY id DESC LIMIT 1""").fetchone()
        else:
            row = None
        error = self.db.metadata("ibkr_flex_sync_error") or (row["error"] if row else "")
        stage = self.db.metadata("ibkr_flex_sync_stage") or "Unknown"
        failed_at = self.db.metadata("ibkr_flex_sync_failed_at") or (row["finished_at"] if row else "Unknown")
        window = (f"{row['from_day']} to {row['to_day']}" if row and row["from_day"] else "Not recorded")
        details = (f"Error: {error or 'No error was recorded.'}\n"
                   f"Failed at: {failed_at}\nStage: {stage}\nDate window: {window}\n"
                   f"Run ID: {row['id'] if row else 'Not recorded'}\n"
                   f"Last successful sync: {self.db.metadata('ibkr_flex_sync_completed') or 'Never'}\n\n"
                   f"Diagnostic log: {diagnostic_log_path(self.db.path, 'sync-trades')}")
        if trade_sync_paused(self.db):
            code = self.db.metadata('ibkr_flex_paused') or '1025'
            details += (f"\n\nAutomatic Flex requests are paused after IBKR error {code}. "
                        "Check the configured Flex queries and token in Client Portal. "
                        "Then click Sync IBKR or run Import history to retry.")
        QMessageBox.warning(self, "IBKR trade sync failed", details)

    def _launch_analytics_sync(self, *, manual=False):
        if self._update_blocked():
            return
        if self._history_running():
            return
        if self.db.metadata('ibkr_flex_paused') not in ('', '1003'):
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("analytics_flex_query_id"):
            if manual:
                QMessageBox.warning(self, "Analytics history", "Set the IBKR account and Analytics Flex query ID in Settings first.")
            return
        if self._analytics_process is not None and self._analytics_process.poll() is None:
            return
        if not manual and time.monotonic() - self._last_analytics_sync_attempt < 300:
            return
        self._last_analytics_sync_attempt = time.monotonic()
        try:
            self.db.set_metadata("ibkr_analytics_sync_status", "starting")
            self.db.set_metadata("ibkr_analytics_sync_error", "")
            command, workdir = self._sync_launch("scripts.sync_analytics")
            self.log.info("Launching analytics history sync; manual=%s", manual)
            self._analytics_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._start_timed_sync("Analytics", self._analytics_process)
            self.analytics_tab.refresh()
        except OSError as exc:
            self.db.set_metadata("ibkr_analytics_sync_status", "failed")
            self.db.set_metadata("ibkr_analytics_sync_error", str(exc))
            self._progress_failed = True
            self.log.error("Analytics history sync launch failed: %s: %s", type(exc).__name__, exc)

    def _launch_cash_sync(self, *, manual=False):
        if self._update_blocked():
            return
        if self._history_running():
            return
        if self.db.metadata('ibkr_flex_paused') not in ('', '1003'):
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("cash_flex_query_id"):
            if manual:
                QMessageBox.warning(self, "Cash history", "Set the IBKR account and Cash Flex query ID in Settings first.")
            return
        if self._cash_process is not None and self._cash_process.poll() is None:
            return
        if not manual and self._last_cash_sync_day == date.today():
            return
        if not manual:
            self._last_cash_sync_day = date.today()
        try:
            self.db.set_metadata("ibkr_cash_sync_status", "starting")
            self.db.set_metadata("ibkr_cash_sync_error", "")
            command, workdir = self._sync_launch("scripts.sync_cash")
            self.log.info("Launching cash history sync; manual=%s", manual)
            self._cash_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._start_timed_sync("Cash", self._cash_process)
        except OSError as exc:
            self.db.set_metadata("ibkr_cash_sync_status", "failed")
            self.db.set_metadata("ibkr_cash_sync_error", str(exc))
            self._progress_failed = True
            self.log.error("Cash history sync launch failed: %s: %s", type(exc).__name__, exc)

    def _make_grid_tab(self, name):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar_widget = QWidget()
        toolbar_widget.setObjectName(f"{name}Toolbar")
        toolbar = QHBoxLayout(toolbar_widget)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(TOOLBAR_GROUP_SPACING)
        if name == "Trades":
            filter_controls = QHBoxLayout()
            filter_controls.setSpacing(TOOLBAR_ITEM_SPACING)
            self.trade_side_combo = QComboBox()
            style_toolbar_dropdown(self.trade_side_combo)
            self.trade_side_combo.addItems(["All", "BUY", "SELL"])
            self.trade_side_combo.currentTextChanged.connect(self.models[name].set_side_filter)
            filter_controls.addWidget(QLabel("Side"))
            filter_controls.addWidget(self.trade_side_combo)
            toolbar.addLayout(filter_controls)
        if name == "Lots":
            filter_controls = QHBoxLayout()
            filter_controls.setSpacing(TOOLBAR_ITEM_SPACING)
            lot_filter = QComboBox()
            style_toolbar_dropdown(lot_filter)
            lot_filter.addItems(["All", "Open", "Closed"])
            lot_filter.currentTextChanged.connect(self.models[name].set_lot_filter)
            self.lot_filter_combo = lot_filter
            filter_controls.addWidget(QLabel("Status"))
            filter_controls.addWidget(lot_filter)
            toolbar.addLayout(filter_controls)
        column_controls = QHBoxLayout()
        column_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        if name == "Portfolio":
            column_controls.addWidget(QLabel("Show"))
        columns_button = self._button(column_controls, "Columns", lambda: None)
        style_toolbar_dropdown(columns_button)
        column_menu = QMenu(columns_button)
        column_menu.aboutToShow.connect(lambda table=name, menu=column_menu: self._populate_columns_menu(table, menu))
        columns_button.setMenu(column_menu)
        toolbar.addLayout(column_controls)
        if name == "Portfolio":
            self.hide_closed_checkbox = ThemedCheckBox("Hide closed")
            self.hide_closed_checkbox.setToolTip("Hide positions with zero trade-derived quantity")
            self.hide_closed_checkbox.setChecked(self.models[name].hide_closed)
            self.hide_closed_checkbox.toggled.connect(self.models[name].set_hide_closed)
            toolbar.addWidget(self.hide_closed_checkbox)
        toolbar.addStretch()
        search = QLineEdit()
        search.setPlaceholderText("Type instrument")
        search.setClearButtonEnabled(True)
        search.setMaximumWidth(220)
        search.textChanged.connect(self.models[name].set_filter)
        self.search_fields[name] = search
        toolbar.addWidget(search)
        toolbar_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        toolbar_widget.customContextMenuRequested.connect(
            lambda point, table=name, widget=toolbar_widget:
            self._toolbar_context_menu(table, widget.mapToGlobal(point)))
        for control in (*toolbar_widget.findChildren(QPushButton), *toolbar_widget.findChildren(QCheckBox)):
            control.setContextMenuPolicy(Qt.CustomContextMenu)
            control.customContextMenuRequested.connect(
                lambda point, table=name, widget=control:
                self._toolbar_context_menu(table, widget.mapToGlobal(point)))
        layout.addWidget(toolbar_widget)
        view = GridView(self.models[name])
        view.setAlternatingRowColors(False)
        view.setSelectionBehavior(QAbstractItemView.SelectItems)
        view.clicked.connect(lambda _, table=name: self._set_selection_axis(table, "cell"))
        view.verticalHeader().sectionClicked.connect(lambda _, table=name: self._set_selection_axis(table, "row"))
        view.horizontalHeader().sectionClicked.connect(lambda column, table=name: self._header_clicked(table, column))
        view.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
        view.horizontalHeader().customContextMenuRequested.connect(
            lambda point, table=name: self._header_menu(table, point))
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(lambda point, table=name: self._table_context_menu(table, point))
        for column, (key, _, _) in enumerate(self.models[name].columns):
            if key in BAR_COLUMNS[name]:
                view.setItemDelegateForColumn(column, SignedBarDelegate(view))
        self.views[name] = view
        layout.addWidget(view)
        info = QLabel("No selection")
        info.setObjectName("hint")
        self.selection_info[name] = info
        layout.addWidget(info)
        view.selectionModel().selectionChanged.connect(lambda *_args, table=name: self._update_selection_info(table))
        if name == "Portfolio":
            view.doubleClicked.connect(lambda _: self.show_position_details())
        self.tabs.addTab(page, "Overview" if name == "Portfolio" else name)
        self.tabs.tabBar().setTabData(self.tabs.count() - 1, name)

    @staticmethod
    def _button(layout, title, callback):
        button = QPushButton(title)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _make_settings_tab(self):
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        self.settings_status = SettingsStatus()
        connection = QFrame()
        connection.setObjectName("settingsSection")
        connection_layout = QVBoxLayout(connection)
        connection_layout.setContentsMargins(16, 14, 16, 14)
        connection_layout.setSpacing(14)
        heading_row = QHBoxLayout()
        heading = QLabel("Connection")
        heading.setObjectName("settingsHeading")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        self.settings_status.summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        heading_row.addWidget(self.settings_status.summary, 1)
        connection_layout.addLayout(heading_row)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        advanced_form = QFormLayout()
        advanced_form.setVerticalSpacing(10)
        history_form = QFormLayout()
        history_form.setVerticalSpacing(10)
        storage_form = QFormLayout()
        self.settings_fields = {}
        definitions = [
            ("portfolio_name", "Portfolio name", None),
            ("provider", "Data source", ["IBKR", "Demo"]),
            ("ib_account_id", "Account", None),
            ("ib_host", "Host", None), ("ib_port", "Port", None),
            ("ib_client_id", "Client ID", None),
            ("flex_query_id", "Trades Flex query ID", None),
            ("analytics_flex_query_id", "Analytics Flex query ID", None),
            ("cash_flex_query_id", "Cash Flex query ID", None),
            ("data_mode", "Preferred data mode", ["Live", "Frozen", "Delayed"]),
            ("refresh_seconds", "Refresh interval (seconds)", None),
            ("data_directory", "Data directory (next launch)", None),
            ("backup_directory", "Backup directory", None),
        ]
        for key, label, options in definitions:
            stored = (self.db.portfolio_name(self.portfolio_id) if key == "portfolio_name"
                      else self.db.setting(key))
            if options:
                widget = QComboBox()
                widget.addItems(options)
                widget.setCurrentText("Demo" if key == "provider" and stored == "Mock" else stored)
            else:
                widget = QLineEdit("" if stored is None else str(stored))
            self.settings_fields[key] = widget
            widget.setAccessibleName(label)
            if key in ("portfolio_name", "provider", "ib_account_id"):
                target = form
            elif key in ("flex_query_id", "analytics_flex_query_id", "cash_flex_query_id"):
                target = history_form
            elif key in ("data_directory", "backup_directory"):
                target = storage_form
            else:
                target = advanced_form
            target.addRow(label, widget)
        connection_layout.addLayout(form)
        connection_layout.addWidget(self.settings_status.warning)
        layout.addWidget(connection)
        self.settings_fields["data_directory"].setToolTip(f"Choose a portfolio subfolder within {data_root()}")
        self.settings_fields["backup_directory"].setToolTip(f"Backups must also stay in a subfolder within {data_root()}")
        if self._database_path_override:
            self.settings_fields["data_directory"].setText(str(self.db.path.parent))
            self.settings_fields["data_directory"].setReadOnly(True)
            self.settings_fields["data_directory"].setToolTip("Selected by --database for this launch")
        self.connection_options = SettingsSection("Connection options")
        self.connection_options.content_layout.addLayout(advanced_form)
        self.advanced_button = self.connection_options.toggle
        self.advanced_settings = self.connection_options.content
        layout.addWidget(self.connection_options)
        history = self.settings_status.history_section.content_layout
        history.insertLayout(0, history_form)
        row = QHBoxLayout()
        self._button(row, "Save Flex token", self.save_flex_token).setObjectName("secondary")
        self._button(row, "Import history", self.import_history).setObjectName("secondary")
        self.older_trades_button = self._button(row, "Import older trades…", self.import_older_statement_trades)
        self.older_trades_button.setObjectName("secondary")
        self.older_trades_button.setToolTip(
            "Import English IBKR Activity Statement CSV trades older than Flex history")
        self.older_trades_button.hide()
        self.settings_history_control_button = self._button(row, 'Pause import', self.control_history_import)
        self.settings_history_control_button.setObjectName("secondary")
        self.settings_history_control_button.hide()
        row.addStretch()
        history.insertLayout(1, row)
        hint = QLabel("Save settings before saving a token or importing history.")
        hint.setObjectName("settingsKey")
        hint.setWordWrap(True)
        history.insertWidget(2, hint)
        diagnostics = self.settings_status.diagnostics_section.content_layout
        storage_heading = QLabel("Storage")
        storage_heading.setObjectName("settingsHeading")
        diagnostics.addWidget(storage_heading)
        diagnostics.addLayout(storage_form)
        self.settings_history = self.settings_status.history_details
        self.settings_info = self.settings_status.technical_details
        layout.addWidget(self.settings_status)
        layout.addStretch()
        scroller = QScrollArea()
        scroller.setObjectName("settingsScroll")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.NoFrame)
        scroller.setWidget(page)
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("Settings")
        self.settings_dialog.resize(620, 440)
        dialog_layout = QVBoxLayout(self.settings_dialog)
        dialog_layout.addWidget(scroller)
        footer = QHBoxLayout()
        footer.setContentsMargins(8, 4, 8, 4)
        footer.addStretch()
        close_button = self._button(footer, "Close", self.settings_dialog.reject)
        close_button.setObjectName("secondary")
        close_button.setAutoDefault(False)
        self.save_settings_button = self._button(footer, "Save settings", self.apply_settings)
        self.save_settings_button.setDefault(True)
        dialog_layout.addLayout(footer)
        for button in page.findChildren(QPushButton):
            button.setAutoDefault(False)

    def open_settings(self):
        self.settings_dialog.show()
        self._update_status()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def save_flex_token(self):
        account = self.settings_fields["ib_account_id"].text().strip()
        if not account or account != self.db.setting("ib_account_id"):
            QMessageBox.warning(self, "Flex token", "Save the IBKR account setting first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Save Flex token")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Paste the Flex Web Service token for this account."))
        entry = QLineEdit()
        entry.setEchoMode(QLineEdit.Password)
        layout.addWidget(entry)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        token = entry.text().strip()
        entry.clear()
        try:
            save_flex_token(account, token)
        except Exception as exc:
            QMessageBox.warning(self, "Flex token", f"Could not save token: {type(exc).__name__}")
            return
        QMessageBox.information(self, "Flex token", "Token saved to this computer's credential store.")

    def import_history(self):
        if self._history_running():
            QMessageBox.information(self, "Import history", "A historical import is already running.")
            return
        if self._flex_sync_running():
            QMessageBox.information(self, "Import history", "An IBKR Flex sync is running. Wait for it to finish before importing history.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Import historical Flex data")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        source = QComboBox()
        source.addItems(["all", "trades", "analytics", "cash"])
        first = QLineEdit()
        last = QLineEdit()
        first.setPlaceholderText("YYYY-MM-DD")
        last.setPlaceholderText("YYYY-MM-DD")
        form.addRow("Source", source)
        form.addRow("From date", first)
        form.addRow("To date", last)
        layout.addLayout(form)
        layout.addWidget(QLabel("Imports dated Activity Flex reports into your local database."))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            start = date.fromisoformat(first.text().strip())
            end = date.fromisoformat(last.text().strip())
            if start > end or end >= date.today():
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Import history", "Choose an ordered date range before today (YYYY-MM-DD).")
            return
        if self._flex_sync_running():
            QMessageBox.information(self, "Import history", "An IBKR Flex sync started while the dialog was open. Wait for it to finish, then retry.")
            return
        self._start_history_import(source.currentText(), start, end)

    def import_older_statement_trades(self):
        if not self.db.setting("ib_account_id").strip():
            QMessageBox.information(self, "Import older trades", "Set and save your IBKR account in Settings first.")
            return
        if self._history_running() or self._flex_sync_running():
            QMessageBox.information(self, "Import older trades", "Wait for the current IBKR import or sync to finish first.")
            return
        guide = QMessageBox(self)
        guide.setWindowTitle("Import older trades")
        guide.setIcon(QMessageBox.Information)
        guide.setText("Older trades are individual buy/sell executions from dates IBKR Flex can’t provide.")
        guide.setInformativeText(
            "1. Client Portal → Performance & Reports → Statements.\n"
            "2. Choose the older period, starting at account opening if stocks were transferred in; download an English Activity Statement CSV.\n"
            "3. Choose that CSV here.\n\n"
            "Include Trades, Transfers, Open Positions and Financial Instrument Information. "
            "This imports older executions and verified transferred holdings; it does not add Analytics or Cash history.")
        guide.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        guide.setDefaultButton(QMessageBox.Yes)
        guide.button(QMessageBox.Yes).setText("Choose CSV…")
        if guide.exec() != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an IBKR Activity Statement", "",
            "IBKR Activity Statement (*.csv);;CSV files (*.csv)")
        if not path:
            return
        try:
            preview = preview_activity_statement_trades(self.db, path, self.portfolio_id)
        except Exception as exc:
            QMessageBox.warning(self, "Import older trades",
                                f"This statement could not be safely imported:\n\n{exc}\n\nNo trades were changed.")
            return
        first, last = preview["period"]
        question = QMessageBox(self)
        question.setWindowTitle("Review older trades")
        question.setIcon(QMessageBox.Information)
        question.setText(
            f"IBKR account: {preview['account']}\n"
            f"Statement dates: {first} to {last}\n"
            f"Executions found: {len(preview['rows']):,}" +
            (f"\nTransferred holdings found: {len(preview['transfers']):,} stocks "
             f"({sum(float(g['quantity']) for g in preview['transfers']):g} shares)"
             if preview.get('transfers') else '') +
            (f"\nForex conversion rows not included: {preview['ignored_forex']:,}"
             if preview.get("ignored_forex") else ""))
        question.setInformativeText(
            "Transferred holdings are added only when the statement and saved broker lots verify quantity and cost basis. "
            "They are recorded as holdings at the statement end date, with original acquisition dates retained.\n\n"
            "Existing executions and transferred holdings are skipped. This does not add cash or performance history, "
            "or mark Flex dates as covered.")
        question.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        question.setDefaultButton(QMessageBox.Cancel)
        question.button(QMessageBox.Yes).setText("Import trades")
        if question.exec() != QMessageBox.Yes:
            return
        try:
            outcome = import_activity_statement_trades(self.db, preview, self.portfolio_id)
        except Exception as exc:
            QMessageBox.warning(self, "Import older trades",
                                f"No trades were imported because the statement could not be reconciled safely:\n\n{exc}")
            return
        result = outcome["ingest"]
        imported = result.inserted if result else 0
        updated = result.updated if result else 0
        self._refresh_models()
        success_message = (
            f"Executions imported: {imported + updated:,}\n"
            f"Executions already saved: {outcome['duplicates']:,}\n"
            + (f"Transferred holdings added: {outcome['transfers_added']:,} stocks "
               f"({outcome['transfer_quantity']:g} shares)\n"
               f"Transferred holdings already saved: {outcome['transfers_skipped']:,} stocks\n"
               if preview.get('transfers') else '') + "\n"
            + (f"Forex conversion rows not imported: {preview['ignored_forex']:,}\n\n"
               if preview.get("ignored_forex") else "")
            + "These records are saved locally. Activity Statement imports remain separate from Flex coverage; "
            "continue using Sync IBKR for regular updates.")
        try:
            reconciliation = reconciliation_status(self.db, self.portfolio_id)
        except Exception:
            self.log.exception('Could not read lot diagnostics after a saved statement import')
            QMessageBox.information(self, 'Older trades imported', success_message +
                '\n\nThe import was saved, but remaining lot diagnostics could not be read. Check the diagnostic logs.')
            return
        if reconciliation['reconciliation_guidance']:
            box = QMessageBox(self)
            box.setWindowTitle('Older trades imported; some lots need review')
            box.setIcon(QMessageBox.Information)
            box.setTextFormat(Qt.PlainText)
            box.setText(success_message)
            box.setInformativeText('Flagged quantities, cost basis or P&L remain unverified.\n\n' +
                                   '\n'.join(reconciliation['reconciliation_actions']))
            details = '\n\n'.join(reconciliation['reconciliation_guidance'] +
                                    reconciliation['reconciliation_details'])
            box.setDetailedText(details)
            box.setStandardButtons(QMessageBox.Ok)
            copy = box.addButton('Copy diagnostics', QMessageBox.ActionRole)
            copy.clicked.connect(lambda: QApplication.clipboard().setText(details))
            box.exec()
        else:
            QMessageBox.information(self, 'Older trades imported', success_message)

    def _start_history_import(self, source, start, end):
        if self._update_blocked():
            return
        if self._history_running() or self._flex_sync_running():
            QMessageBox.information(self, 'Import history', 'An IBKR import or sync is running. Wait for it to finish, then retry.')
            return
        args = ["--source", source, "--from-date", start.isoformat(),
                "--to-date", end.isoformat(), "--database", str(self.db.path)]
        status = dict(state="starting", source=source, from_date=start.isoformat(),
                      to_date=end.isoformat(), started_at=datetime.now().isoformat(timespec="seconds"),
                      pid=None, message="", pause_supported=True, paused_by_user=False,
                      account_id=self.db.setting('ib_account_id'))
        try:
            pause_request_path(self._history_status_path).unlink(missing_ok=True)
            write_status(self._history_status_path, status)
        except OSError as exc:
            QMessageBox.warning(self, "Import history", f"Could not save import status: {exc}")
            return
        command, workdir = worker_launch("scripts.history_worker", ["--status-file",
                                        str(self._history_status_path), *args])
        try:
            self._history_process = subprocess.Popen(
                command, cwd=workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            status.update(state="failed", message=f"Could not start import: {exc}")
            try:
                write_status(self._history_status_path, status)
            except OSError:
                self.log.exception("Could not record history launch failure")
            QMessageBox.warning(self, "Import history", f"Could not start import: {exc}")
            return
        self._update_sync_button()
        self._update_history_status()
        self.statusBar().showMessage("Historical import started; its result will appear in the status bar.", 10000)
        self._history_timer.start(1000)

    def _check_history_import(self):
        process = getattr(self, "_history_process", None)
        if process is None:
            if not self._history_running():
                self._history_timer.stop()
            self._update_history_status()
            return
        if process.poll() is None:
            self._update_history_status()
            return
        self._history_process = None
        status = self._read_history_status()
        self._history_timer.stop()
        try:
            self._update_sync_button()
            self._update_history_status()
            self.show_history_import_details()
            if status.get("state") in ("completed", "completed_with_gaps", "completed_with_review",
                                       "completed_with_pending_history", "paused"):
                self._refresh_models()
        except Exception as exc:
            self.log.exception("Could not refresh after history import: %s", exc)

    def _make_help_dialog(self):
        self.help_dialog = QDialog(self)
        self.help_dialog.setWindowTitle("Help · Portfolio Monitor")
        self.help_dialog.resize(760, 600)
        dialog_layout = QVBoxLayout(self.help_dialog)
        guide_layout = QHBoxLayout()
        self.help_navigation = QListWidget()
        self.help_navigation.setObjectName("helpNavigation")
        self.help_navigation.setFixedWidth(165)
        self.help_navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.help_sections = QStackedWidget()
        self.help_navigation.currentRowChanged.connect(self.help_sections.setCurrentIndex)
        self.help_section_indices = {}
        self.help_visuals = []
        guide_layout.addWidget(self.help_navigation)
        guide_layout.addWidget(self.help_sections, 1)
        dialog_layout.addLayout(guide_layout)

        def add_section(title):
            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QScrollArea.NoFrame)
            page = QWidget()
            page.setObjectName("helpPage")
            content = QVBoxLayout(page)
            content.setContentsMargins(16, 12, 16, 12)
            content.setSpacing(8)
            heading = QLabel(title)
            heading.setObjectName("helpHeading")
            content.addWidget(heading)
            scroller.setWidget(page)
            self.help_section_indices[title] = self.help_sections.addWidget(scroller)
            self.help_navigation.addItem(title)
            return content

        def add_text(content, title, body):
            heading = QLabel(title)
            heading.setObjectName("helpSection")
            content.addWidget(heading)
            label = QLabel(body)
            label.setObjectName("helpBody")
            label.setWordWrap(True)
            content.addWidget(label)

        def add_visual(content, kind):
            visual = HelpVisual(kind)
            self.help_visuals.append(visual)
            content.addWidget(visual)

        start_content = add_section("Initial setup")
        introduction = QLabel(
            "Track your portfolio. The app cannot place orders.")
        introduction.setObjectName("helpBody")
        introduction.setWordWrap(True)
        start_content.insertWidget(1, introduction)
        add_visual(start_content, "start")
        add_text(start_content, "Connect IBKR",
                 "1. Open TWS / IB Gateway. Enable the read-only API.\n"
                 "2. In Settings, choose IBKR. Enter Account; check Connection options.\n"
                 "3. Click Save settings. Open HISTORICAL_REPORTS.md below.")
        self.help_setup_button = QPushButton("Open HISTORICAL_REPORTS.md")
        self.help_setup_button.setObjectName("secondary")
        self.help_setup_button.setAutoDefault(False)
        self.help_setup_button.clicked.connect(self.open_historical_reports)
        start_content.addWidget(self.help_setup_button)
        add_text(start_content, "Load history",
                 "1. In Settings > History & Flex, enter all three query IDs.\n"
                 "2. Save settings. Click Save Flex token and paste it.\n"
                 "3. Click Sync IBKR, then Import history for older dates.\n"
                 "Import older trades: CSV trades and verified transferred holdings only.")
        add_text(start_content, "Pause or check an import",
                 "Pause import saves progress. Resume import continues.\n"
                 "Click History status > Show Details to check issues.")

        portfolio_content = add_section("Portfolio")
        add_text(portfolio_content, "Overview",
                 "Search or click Hide closed.\n"
                 "Double-click a holding for trades and lots.")
        add_visual(portfolio_content, "portfolio")
        add_text(portfolio_content, "Trades and Lots",
                 "Trades: buys/sells. Lots: held/sold quantities. Both read-only.\n"
                 "Profit or loss (P&L) includes fees.\n"
                 "Realized: matched sales. Open: remaining holdings.\n"
                 "Matched P&L appears twice. Count it once.")
        add_text(portfolio_content, "Cash",
                 "Choose dates. Select Hide zero cash currencies if needed.\n"
                 "Hover over Cash check for verified dates.")

        analytics_content = add_section("Analytics")
        add_text(analytics_content, "P&L and Capital deployment",
                 "Choose Range / Custom dates. Drag to change dates; hover for values.\n"
                 "Chart and table: Top and bottom / Side by side.\n"
                 "P&L: account value (NAV) and returns. S&P 500: index comparison.\n"
                 "Adjusted: removes deposits, withdrawals and transfers.\n"
                 "Capital deployment: invested money, buys/sells and cash.")
        add_visual(analytics_content, "analytics")
        add_text(analytics_content, "Daily moves and Sharpe",
                 "Choose Max USD gain / Max USD loss, or Max % gain / Max % loss.\n"
                 "Dollar and percentage rankings can pick different days.\n"
                 "Sharpe ratio: return compared with daily fluctuations.\n"
                 "Needs 63 completed daily returns. Read the note for gaps.")
        add_text(analytics_content, "Returns",
                 "Select a name for lots. Options share their underlying's row.\n"
                 "Tables: Top and bottom / Side by side.\n"
                 "Return on cost = total P&L / all purchase costs, including buy fees.\n"
                 "Shorts: dollars only. Excludes dividends, interest and withholding.")
        add_text(analytics_content, "Exposure",
                 "Choose a group and Market value / Adjusted (includes option sensitivity).\n"
                 "Click a slice to filter; Manage categories to edit groups.")

        rebalance_content = add_section("Rebalance")
        add_text(rebalance_content, "Plan targets and scenarios",
                 "Enter target weights / Cash target to estimate trades.\n"
                 "Click Add idea for a stock. Set upside/downside to compare outcomes.\n"
                 "Show: choose columns. Pause updates: freeze prices and weights.")
        add_visual(rebalance_content, "rebalance")

        views_content = add_section("Views & data")
        add_text(views_content, "Tables and saved views",
                 "Click headings to sort. Right-click for columns/decimals.\n"
                 "View > Save current tab view: keep filters and layout.\n"
                 "Drag tabs to reorder. View > Save tab layout: keep the order.\n"
                 "Analytics subtab order saves automatically.\n"
                 "File > Export Workbook: export tables.")
        add_text(views_content, "Data status",
                 "Today's values aren't final. Offline values may be old.\n"
                 "Hover over dashes for details.\n"
                 "Settings > History & Flex > Show all date ranges: coverage.\n"
                 "Import: High = stopped; Medium = review/action; Low = notice.\n"
                 "Click Show Details for next steps. Copy diagnostics copies evidence.")
        add_text(views_content, "Portfolio name and files",
                 "Rename: Settings > Portfolio name, then Save settings.\n"
                 "Settings > Diagnostics > Technical details: database path.\n"
                 "That portfolio's data folder holds reports and logs.\n"
                 "File > Create SQLite Backup: save a copy.")

        shortcuts_content = add_section("Shortcuts")
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(5)
        for number, (keys, action) in enumerate((
            ("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous tab"),
            ("Ctrl+Shift+H", "Show or hide the account summary"),
            ("Enter / F2", "Edit a cell; Enter saves and moves down"),
            ("Esc", "Cancel the edit"),
            ("Arrow keys", "Move between input cells and save the edit"),
            ("Ctrl+-", "Rebalance: remove an idea or set a holding's target to 0%"),
            ("Shift+Space", "Select the row"),
            ("Ctrl+Space", "Select the column"),
            ("Ctrl+C", "Copy the selection"),
            ("Ctrl+V", "Paste into an editable field or cell"),
            ("Ctrl+X", "Cut selected text in an input field"),
            ("Ctrl+A", "Select all in the input or table"),
            ("F1", "Open this guide"),
        )):
            key_label = QLabel(keys)
            key_label.setObjectName("helpKey")
            description = QLabel(action)
            description.setWordWrap(True)
            grid.addWidget(key_label, number, 0)
            grid.addWidget(description, number, 1)
        grid.setColumnStretch(1, 1)
        shortcuts_content.addLayout(grid)
        for content in (start_content, portfolio_content, analytics_content,
                        rebalance_content, views_content, shortcuts_content):
            content.addStretch()
        self.help_navigation.setCurrentRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.help_dialog.reject)
        dialog_layout.addWidget(buttons)

    def open_historical_reports(self):
        root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else project_directory()
        guide = root / "HISTORICAL_REPORTS.md"
        if not guide.is_file():
            QMessageBox.warning(self, "Flex setup guide", "HISTORICAL_REPORTS.md is missing from the app folder.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide))):
            QMessageBox.warning(self, "Flex setup guide", "Could not open the guide. Open HISTORICAL_REPORTS.md in the app folder.")

    def open_help(self):
        current = self.tabs.currentWidget()
        section = ("Analytics" if current is self.analytics_tab else
                   "Rebalance" if current is self.calcs_tab else "Portfolio")
        self.help_navigation.setCurrentRow(self.help_section_indices[section])
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    def _make_menu(self):
        file_menu = self.menuBar().addMenu("File")
        for title, handler in (("Export Workbook (unencrypted)", self.export),
                               ("Create encrypted local backup", self.backup),
                               ("Restore encrypted local backup", self.restore_local_backup),
                               ("Export portable recovery backup", self.export_recovery_backup),
                               ("Restore portable recovery backup", self.restore_recovery_backup)):
            action = QAction(title, self)
            action.triggered.connect(handler)
            file_menu.addAction(action)
        view_menu = self.menuBar().addMenu("View")
        self.show_summary_action = view_menu.addAction("Show account summary")
        self.show_summary_action.setCheckable(True)
        self.show_summary_action.setChecked(self._show_account_summary)
        self.show_summary_action.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.show_summary_action.toggled.connect(self._set_account_summary_visible)
        view_menu.addSeparator()
        self.save_view_action = view_menu.addAction(
            "Save current tab view",
            lambda: self.save_default_view(self._current_table_name()) if self._current_table_name() else None)
        self.restore_view_action = view_menu.addAction(
            "Restore saved tab view",
            lambda: self.restore_default_view(self._current_table_name()) if self._current_table_name() else None)
        view_menu.addSeparator()
        self.save_tabs_action = view_menu.addAction("Save tab layout", self.save_tab_layout)
        self.restore_tabs_action = view_menu.addAction("Restore saved tab layout", self.restore_tab_layout)
        view_menu.aboutToShow.connect(lambda: (self.save_view_action.setEnabled(self._current_table_name() is not None),
            self.restore_view_action.setEnabled(self._current_table_name() is not None and
                self._saved_default_view(self._current_table_name()) is not None),
            self.restore_tabs_action.setEnabled(self._saved_tab_layout() is not None)))
        help_menu = self.menuBar().addMenu("Help")
        guide_action = QAction("User guide", self)
        guide_action.setShortcut(QKeySequence("F1"))
        guide_action.setShortcutContext(Qt.ApplicationShortcut)
        guide_action.triggered.connect(self.open_help)
        help_menu.addAction(guide_action)
        help_menu.addAction("Check for updates…", self.check_for_updates)

    def _restore_columns(self):
        use_saved = self.db.setting("header_layout_version") == "3"
        portfolio_saved = self.db.setting("portfolio_columns_version") == "4"
        for name, view in self.views.items():
            state = self.db.setting(f"header_{name}")
            if use_saved and state and (name != "Portfolio" or portfolio_saved):
                view.horizontalHeader().restoreState(QByteArray.fromBase64(state.encode("ascii")))
            else:
                view.horizontalHeader().setDefaultSectionSize(115)
                view.setColumnWidth(0, 145)
                if name == "Trades":
                    view.setColumnWidth(1, 165)
                    for column in (8, 9, 10, 11):
                        view.hideColumn(column)
                elif name == "Portfolio":
                    for column in (13, 14, 15):
                        view.hideColumn(column)
                else:
                    view.horizontalHeader().setDefaultSectionSize(105)
                    for column, width in ((0, 75), (1, 130), (2, 90), (3, 80), (4, 85),
                                          (5, 90), (6, 85), (7, 110), (8, 110), (9, 120)):
                        view.setColumnWidth(column, width)
                    for column in (10, 11, 12):
                        view.hideColumn(column)
            if name == "Trades" and self.db.setting("trade_pnl_columns_intro_applied") != "1":
                self._introduce_trade_pnl_columns()
                self.db.set_setting("trade_pnl_columns_intro_applied", "1")
            if name == "Trades" and self.db.setting("trade_pnl_pct_columns_intro_applied") != "1":
                self._introduce_trade_pnl_percent_columns()
                self.db.set_setting("trade_pnl_pct_columns_intro_applied", "1")
            if name == "Portfolio" and self.db.setting("price_move_bars_intro_applied") != "1":
                for column in (9, 10, 11):
                    view.showColumn(column)
                self.db.set_setting("price_move_bars_intro_applied", "1")
            if name == "Portfolio" and self.db.setting("portfolio_open_cost_column_intro_applied") != "1":
                columns = {key: index for index, (key, _, _) in enumerate(PORTFOLIO)}
                column = columns["open_cost_usd"]
                view.showColumn(column)
                view.setColumnWidth(column, 155)
                header = view.horizontalHeader()
                source, target = header.visualIndex(column), header.visualIndex(columns["average_cost"]) + 1
                header.moveSection(source, target - 1 if source < target else target)
                self.db.set_setting("portfolio_open_cost_column_intro_applied", "1")
        if not use_saved:
            self.db.set_setting("header_layout_version", "3")
        if not portfolio_saved:
            self.db.set_setting("portfolio_columns_version", "4")

    def _introduce_trade_pnl_columns(self):
        view = self.views["Trades"]
        header = view.horizontalHeader()
        columns = {key: index for index, (key, _, _) in enumerate(TRADES)}
        for key, after, width in (("last_price", "price", 110),
                                  ("realized_net", "gross_usd", 140),
                                  ("net_unrealized", "realized_net", 140),
                                  ("price_source", "match_status", 150),
                                  ("pnl_note", "price_source", 190)):
            column = columns[key]
            view.showColumn(column)
            view.setColumnWidth(column, width)
            header.moveSection(header.visualIndex(column), header.visualIndex(columns[after]) + 1)
        self._introduce_trade_pnl_percent_columns()

    def _introduce_trade_pnl_percent_columns(self):
        view = self.views["Trades"]
        header = view.horizontalHeader()
        columns = {key: index for index, (key, _, _) in enumerate(TRADES)}
        for key, after in (("realized_pct", "realized_net"), ("unrealized_pct", "net_unrealized")):
            column = columns[key]
            view.showColumn(column)
            view.setColumnWidth(column, 130)
            source, target = header.visualIndex(column), header.visualIndex(columns[after]) + 1
            header.moveSection(source, target - 1 if source < target else target)

    def _current_table_name(self):
        name = self.tabs.tabBar().tabData(self.tabs.currentIndex())
        return name if name in self.DEFAULT_TAB_ORDER else None

    def _tab_index(self, name):
        return next((index for index in range(self.tabs.count())
                     if self.tabs.tabBar().tabData(index) == name), -1)

    def _saved_tab_layout(self):
        try:
            state = json.loads(self.db.setting(self.TAB_LAYOUT_KEY))
        except (TypeError, ValueError):
            return None
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("order"), list):
            return None
        order = state["order"]
        if (not all(isinstance(name, str) and name in self.DEFAULT_TAB_ORDER for name in order) or
            len(set(order)) != len(order)):
            return None
        active = state.get("active")
        if active is not None and active not in self.DEFAULT_TAB_ORDER:
            return None
        return {"order": order + [name for name in self.DEFAULT_TAB_ORDER if name not in order],
                "active": active}

    def save_tab_layout(self):
        order = [self.tabs.tabBar().tabData(index) for index in range(self.tabs.count())]
        state = {"version": 1, "order": order,
                 "active": self.tabs.tabBar().tabData(self.tabs.currentIndex())}
        self.db.set_setting(self.TAB_LAYOUT_KEY, json.dumps(state))
        self.statusBar().showMessage("Tab layout saved", 5000)

    def restore_tab_layout(self):
        state = self._saved_tab_layout()
        if state is None:
            return False
        for destination, name in enumerate(state["order"]):
            source = self._tab_index(name)
            if source >= 0 and source != destination:
                self.tabs.tabBar().moveTab(source, destination)
        active = self._tab_index(state["active"])
        if active >= 0:
            self.tabs.setCurrentIndex(active)
        self.statusBar().showMessage("Saved tab layout restored", 5000)
        return True

    def _saved_default_view(self, name):
        try:
            state = json.loads(self.db.setting(f"default_view_{name}"))
        except (TypeError, ValueError):
            return None
        if name in ("Analytics", "Rebalance"):
            valid_versions = (1, 2) if name == "Rebalance" else (1,)
            return state if isinstance(state, dict) and state.get("version") in valid_versions else None
        if name == "Cash":
            if not isinstance(state, dict):
                return None
            try:
                dates_valid = all(date.fromisoformat(state[key]).isoformat() == state[key]
                                  for key in ("start", "end"))
            except (KeyError, TypeError, ValueError):
                return None
            if (state.get("version") != 1 or
                self.cash_tab.range_combo.findText(state.get("range", "")) < 0 or
                not dates_valid or not isinstance(state.get("hide_zero"), bool) or
                not isinstance(state.get("header"), str) or
                not isinstance(state.get("sort_column"), int) or
                isinstance(state.get("sort_column"), bool) or
                not 0 <= state["sort_column"] < self.cash_tab.table.columnCount() or
                state.get("sort_order") not in ("asc", "desc") or
                not isinstance(state.get("amount_decimals"), int) or
                isinstance(state.get("amount_decimals"), bool) or
                not 0 <= state["amount_decimals"] <= 30):
                return None
            return state
        if name == "Portfolio" and isinstance(state, dict) and state.get("columns") != [
                key for key, _, _ in COLUMNS[name]]:
            # Keep filters and sort from an older default, but discard its obsolete header layout.
            state["header"] = None
            if state.get("sort_key") == "broker_qty":
                state["sort_key"] = "position_qty"
            elif state.get("sort_key") == "qty_difference":
                state["sort_key"] = "reconciliation"
        if (not isinstance(state, dict) or state.get("version") != 1 or
            state.get("header") is not None and not isinstance(state.get("header"), str) or
            state.get("sort_key") not in {key for key, _, _ in COLUMNS[name]} or
            state.get("sort_order") not in ("asc", "desc") or
            not isinstance(state.get("search"), str)):
            return None
        return state

    def save_default_view(self, name):
        if name in ("Analytics", "Rebalance"):
            tab = self.analytics_tab if name == "Analytics" else self.calcs_tab
            self.db.set_setting(f"default_view_{name}", json.dumps(tab.view_state()))
            self.statusBar().showMessage(f"{name} tab view saved", 5000)
            return
        if name == "Cash":
            self.db.set_setting("default_view_Cash", json.dumps(self.cash_tab.view_state()))
            self.statusBar().showMessage("Cash tab view saved", 5000)
            return
        view, model = self.views[name], self.models[name]
        state = {
            "version": 1,
            "header": bytes(view.horizontalHeader().saveState().toBase64()).decode("ascii"),
            "columns": [key for key, _, _ in COLUMNS[name]],
            "sort_key": model.columns[model.sort_column][0],
            "sort_order": "desc" if model.sort_order == Qt.DescendingOrder else "asc",
            "search": self.search_fields[name].text(),
            "column_decimals": model.column_decimals.copy(),
        }
        if name == "Portfolio":
            state["hide_closed"] = self.hide_closed_checkbox.isChecked()
        elif name == "Trades":
            state["side_filter"] = self.trade_side_combo.currentText()
        elif name == "Lots":
            state["lot_filter"] = self.lot_filter_combo.currentText()
        self.db.set_setting(f"default_view_{name}", json.dumps(state))
        self.statusBar().showMessage(f"{self.tabs.tabText(self._tab_index(name)).replace('&&', '&')} tab view saved", 5000)

    def restore_default_view(self, name):
        state = self._saved_default_view(name)
        if state is None:
            return False
        if name in ("Analytics", "Rebalance"):
            tab = self.analytics_tab if name == "Analytics" else self.calcs_tab
            if not tab.apply_view_state(state):
                return False
            self.statusBar().showMessage(f"{name} tab view restored", 5000)
            return True
        if name == "Cash":
            self.cash_tab.apply_view_state(state)
            self.statusBar().showMessage("Cash tab view restored", 5000)
            return True
        header = self.views[name].horizontalHeader()
        if state["header"] is not None:
            header.restoreState(QByteArray.fromBase64(state["header"].encode("ascii")))
        if name == "Trades" and "realized_net" not in state.get("columns", ()):
            self._introduce_trade_pnl_columns()
        elif name == "Trades" and "realized_pct" not in state.get("columns", ()):
            self._introduce_trade_pnl_percent_columns()
        self.search_fields[name].setText(state["search"])
        if name == "Portfolio":
            self.hide_closed_checkbox.setChecked(state.get("hide_closed") is True)
        elif name == "Trades":
            self.trade_side_combo.setCurrentText(
                state["side_filter"] if state.get("side_filter") in ("All", "BUY", "SELL") else "All")
        elif name == "Lots":
            self.lot_filter_combo.setCurrentText(
                state["lot_filter"] if state.get("lot_filter") in ("All", "Open", "Closed") else "All")
        column = next(index for index, (key, _, _) in enumerate(COLUMNS[name]) if key == state["sort_key"])
        self._sort_table(name, column, Qt.DescendingOrder if state["sort_order"] == "desc" else Qt.AscendingOrder)
        self.models[name].restore_column_decimals(state.get("column_decimals"))
        self.statusBar().showMessage(f"{self.tabs.tabText(self._tab_index(name)).replace('&&', '&')} tab view restored", 5000)
        return True

    def _restore_default_views(self):
        for name in self.DEFAULT_TAB_ORDER:
            self.restore_default_view(name)

    def _update_blocked(self):
        if self._update_handoff:
            return True
        try:
            return handoff_running() or setup_running()
        except (RuntimeError, OSError):
            return True

    def check_for_updates(self):
        from app.updates.update_ui import UpdateDialog
        if self._update_handoff:
            return
        if self._update_dialog is None:
            self._update_dialog = UpdateDialog(self)
        self._update_dialog.show()
        self._update_dialog.raise_()
        self._update_dialog.check()

    def update_readiness(self):
        if self._pending_data_directory is not None:
            return "A database move is pending. Close and restart normally before updating."
        if self._live_sync_pending:
            return "Live IBKR sync is pending. Wait for it to finish."
        if self._sync_requested or self.sync_request_timer.isActive() or self._sync_all_active or self._sync_all_queue:
            return "A sync request is scheduled. Wait for it to finish."
        try:
            for name, process in (("trade sync", self._sync_process), ("analytics sync", self._analytics_process),
                                  ("cash sync", self._cash_process), ("history import", self._history_process)):
                if process is not None and process.poll() is None:
                    return f"{name.title()} is running. Wait for it to finish."
            status = read_status(self._history_status_path)
            if status.get("state") in ("starting", "running") or status.get("pid") and process_alive(status["pid"]):
                return "History import may still be running or detached. Resolve it before updating."
        except (RuntimeError, OSError, ValueError, TypeError):
            return "Could not confirm that sync and history workers have stopped. Retry after closing them."
        try:
            with single_writer_lock(self.db.path):
                pass
        except (RuntimeError, OSError):
            return "Another database writer is active. Wait for it to finish."
        return None

    def _save_close_state(self):
        self.calcs_tab.save_view()
        for name, view in self.views.items():
            state = bytes(view.horizontalHeader().saveState().toBase64()).decode("ascii")
            self.db.set_setting(f"header_{name}", state)
            model = self.models[name]
            self.db.set_setting(f"sort_{name}", f"{model.sort_column}:{'desc' if model.sort_order == Qt.DescendingOrder else 'asc'}")

    def _update_timers(self):
        return (self.timer, self.sync_request_timer, self.refresh_timer, self._history_timer,
                self.lots_refresh_timer, self.calcs_refresh_timer, self.summary_refresh_timer)

    def _rollback_update(self, reason, dialog):
        # Setup may have launched even though teardown failed. Keep the app
        # mutex and handoff protection until the wizard has actually exited.
        if self._update_process is not None:
            try:
                setup_active = self._update_process.poll() is None or setup_running()
            except (RuntimeError, OSError):
                setup_active = True
            if setup_active:
                self._update_recovery_reason = reason
                dialog.status.setText(f"{reason}. Finish or cancel Setup to return to the app.")
                if not getattr(self, "_update_recovery_waiting", False):
                    self._update_recovery_waiting = True
                    QTimer.singleShot(100, lambda: self._resume_after_setup(dialog))
                return
        self._update_recovery_waiting = False
        if self._update_process is None and getattr(self, "_pending_verified_update", None) is not None:
            cleanup_attempt(self._pending_verified_update)
        self._pending_verified_update = None
        if self._update_gate:
            self._update_gate.close()
            self._update_gate = None
        self._update_handoff = False
        self._update_process = None
        self.centralWidget().setEnabled(True)
        for timer, interval in getattr(self, "_saved_update_timers", ()):
            if interval >= 0:
                timer.start(interval)
        if getattr(self, "_update_provider_stopped", False):
            self._start_provider()
            self._update_provider_stopped = False
        dialog.status.setText(reason)
        dialog.update_button.setEnabled(dialog.release is not None)

    def _resume_after_setup(self, dialog):
        if not self._update_handoff or not self._update_recovery_waiting:
            return
        try:
            setup_active = (self._update_process is not None and self._update_process.poll() is None) or setup_running()
        except (RuntimeError, OSError):
            setup_active = True
        if setup_active:
            QTimer.singleShot(100, lambda: self._resume_after_setup(dialog))
            return
        self._update_process = None
        self._rollback_update(self._update_recovery_reason, dialog)

    def begin_update_handoff(self, result, dialog):
        if result.release != dialog.release or result.generation != dialog.generation:
            cleanup_attempt(result)
            dialog.status.setText("Update request changed; check again")
            return
        reason = self.update_readiness()
        if reason:
            cleanup_attempt(result)
            dialog.status.setText(reason)
            dialog.update_button.setEnabled(True)
            return
        self._pending_verified_update = result
        self._saved_update_timers = ()
        self._update_provider_stopped = False
        try:
            self._update_process = None
            gate = Handoff()
            gate.__enter__()
            self._update_gate = gate
            self._update_handoff = True
            reason = self.update_readiness()
            if reason:
                raise RuntimeError(reason)
            self._save_close_state()
            self.centralWidget().setEnabled(False)
            self._saved_update_timers = [(timer, timer.interval() if timer.isActive() else -1)
                                         for timer in self._update_timers()]
            for timer in self._update_timers():
                timer.stop()
            self._update_provider_stopped = isinstance(self.market, IBKRMarketDataProvider)
            self._provider_done = self.market.stop() if self._update_provider_stopped else None
            self._provider_deadline = time.monotonic() + 10
            self._poll_provider_for_update(dialog)
        except Exception as exc:
            self._rollback_update(f"Update handoff stopped: {exc}", dialog)

    def _poll_provider_for_update(self, dialog):
        if not self._update_handoff:
            return
        if self._provider_done is None or self._provider_done.is_set():
            dialog.finalize(self._pending_verified_update)
        elif time.monotonic() >= self._provider_deadline:
            self._rollback_update("IBKR provider did not disconnect; update cancelled", dialog)
        else:
            QTimer.singleShot(100, lambda: self._poll_provider_for_update(dialog))

    def launch_verified_update(self, result, dialog):
        if (not self._update_handoff or result.generation != dialog.generation - 1 or
                result.release != dialog.release):
            self._rollback_update("Update request changed; retry", dialog)
            return
        reason = self.update_readiness()
        if reason:
            self._rollback_update(reason, dialog)
            return
        if not identity_matches(result):
            self._rollback_update("Verified installer changed before Setup could start", dialog)
            return
        try:
            with single_writer_lock(self.db.path):
                self._update_process = subprocess.Popen(
                    [str(result.path.resolve()), "/NOCLOSEAPPLICATIONS", "/NORESTART"],
                    cwd=str(result.path.parent.resolve()), shell=False)
        except (OSError, RuntimeError) as exc:
            self._rollback_update(f"Setup could not start: {exc}", dialog)
            return
        self._setup_deadline = time.monotonic() + 15
        self._wait_for_setup(dialog)

    def _wait_for_setup(self, dialog):
        if not self._update_handoff or getattr(self, "_update_recovery_waiting", False):
            return
        try:
            running = setup_running()
        except RuntimeError as exc:
            if self._update_process is not None and self._update_process.poll() is None:
                dialog.status.setText(f"Cannot inspect Setup yet: {exc}. Waiting while the app remains protected…")
                QTimer.singleShot(100, lambda: self._wait_for_setup(dialog))
            else:
                self._rollback_update(str(exc), dialog)
            return
        if running and dialog.workers:
            QTimer.singleShot(100, lambda: self._wait_for_setup(dialog))
        elif running:
            self._update_launch_exit = True
            self.close()
        elif self._update_process.poll() is not None:
            self._rollback_update("Setup did not start. The installed app remains open.", dialog)
        else:
            if time.monotonic() > self._setup_deadline:
                dialog.status.setText("Waiting for Setup to open. The app remains protected during handoff…")
            QTimer.singleShot(100, lambda: self._wait_for_setup(dialog))

    def closeEvent(self, event):
        if self._update_handoff and not getattr(self, "_update_launch_exit", False):
            event.ignore()
            if not getattr(self, "_update_process", None):
                self._rollback_update("Update cancelled before Setup opened", self._update_dialog)
            return
        if self._update_dialog and self._update_dialog.workers and not getattr(self, "_update_launch_exit", False):
            self._update_close_wait = True
            self._update_dialog.cancel()
            event.ignore()
            return
        if getattr(self, "_update_launch_exit", False):
            try:
                self.db.conn.close()
            except Exception as exc:
                self._update_launch_exit = False
                event.ignore()
                self._rollback_update(f"Could not close database: {exc}", self._update_dialog)
                return
            if self._update_gate:
                self._update_gate.close()
                self._update_gate = None
            super().closeEvent(event)
            return
        if self._pending_data_directory is not None and (self._history_running() or self._sync_all_active or self._sync_all_queue or any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process))):
            event.ignore()
            self.statusBar().showMessage("Wait for history sync to finish before moving the database", 10000)
            return
        self._save_close_state()
        for timer in self._update_timers():
            timer.stop()
        if isinstance(self.market, IBKRMarketDataProvider):
            self.market.stop()
        if self._pending_data_directory is not None:
            try:
                self.db.configure_data_directory(self._pending_data_directory)
            except Exception as exc:
                if getattr(self.db, "_relocation_retired", False):
                    self.db.conn.close()
                    message = ("The new portfolio location is selected. Restart the app to open it."
                               if getattr(self.db, "_relocation_selected", False) else
                               "The original portfolio was retired safely. Restart the app and recover the completed operation receipt.")
                    QMessageBox.warning(self, "Database move requires restart", message + "\n\n" + str(exc))
                    super().closeEvent(event)
                    return
                event.ignore()
                self.timer.start(250)
                self._set_refresh_interval()
                self._start_provider()
                QMessageBox.warning(self, "Database move failed", str(exc))
                return
        self.db.conn.close()
        super().closeEvent(event)

    def selected_row(self, name):
        index = self.views[name].currentIndex()
        return self.models[name].rows[index.row()] if index.isValid() else None

    def _populate_columns_menu(self, name, menu):
        menu.clear()
        menu.addAction("Show all columns", lambda checked=False, table=name: self._show_all_columns(table))
        menu.addSeparator()
        view = self.views[name]
        for column, (_, label, _) in enumerate(COLUMNS[name]):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not view.isColumnHidden(column))
            action.toggled.connect(lambda checked, table=name, number=column:
                                   self.views[table].setColumnHidden(number, not checked))

    def _select_column(self, name, column):
        view = self.views[name]
        view.selection_axis = "column"
        view.selectionModel().select(view.model().index(0, column),
                                     QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Columns)
        self._update_selection_info(name)

    def _header_clicked(self, name, column):
        header = self.views[name].horizontalHeader()
        if header.click_modifiers & Qt.ControlModifier:
            self._select_column(name, column)
            return
        model = self.models[name]
        reverse = (header.isSortIndicatorShown() and model.sort_column == column and
                   model.sort_order == Qt.AscendingOrder)
        self._sort_table(name, column, Qt.DescendingOrder if reverse else Qt.AscendingOrder)
        self.views[name].selectionModel().clear()
        self.views[name].selection_axis = "cell"
        self._update_selection_info(name)

    def _set_selection_axis(self, name, axis):
        self.views[name].selection_axis = axis
        self._update_selection_info(name)

    def _sort_table(self, name, column, order):
        self.models[name].sort(column, order)
        header = self.views[name].horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setSortIndicator(column, order)
        self._update_selection_info(name)

    def _build_header_menu(self, name, column):
        menu = QMenu(self)
        menu.addAction("Sort ascending", lambda checked=False, table=name, number=column:
                       self._sort_table(table, number, Qt.AscendingOrder))
        menu.addAction("Sort descending", lambda checked=False, table=name, number=column:
                       self._sort_table(table, number, Qt.DescendingOrder))
        if self.models[name].columns[column][2] in NUMERIC_KINDS:
            decimals_menu = QMenu("Decimal places", menu)
            menu.addMenu(decimals_menu)
            group = QActionGroup(decimals_menu)
            group.setExclusive(True)
            for places in range(QUICK_DISPLAY_DECIMALS + 1):
                action = decimals_menu.addAction(f"{places} decimal place{'s' if places != 1 else ''}")
                action.setCheckable(True)
                action.setChecked(self.models[name].decimals_for(column) == places)
                group.addAction(action)
                action.triggered.connect(lambda checked=False, table=name, number=column, count=places:
                                         self.models[table].set_column_decimals(number, count))
            custom = decimals_menu.addAction("Custom…")
            custom.setCheckable(True)
            custom.setChecked(self.models[name].decimals_for(column) > QUICK_DISPLAY_DECIMALS)
            group.addAction(custom)
            custom.triggered.connect(lambda checked=False, table=name, number=column:
                                     self._choose_column_decimals(table, number))
        menu.addSeparator()
        menu.addAction("Hide column", lambda checked=False, table=name, number=column:
                       self.views[table].hideColumn(number))
        menu.addAction("Show all columns", lambda checked=False, table=name: self._show_all_columns(table))
        return menu

    def _choose_column_decimals(self, name, column):
        model = self.models[name]
        places, accepted = QInputDialog.getInt(
            self, "Custom decimal places", "Decimal places (0–30):",
            model.decimals_for(column), 0, MAX_DISPLAY_DECIMALS)
        if accepted:
            model.set_column_decimals(column, places)

    def _header_menu(self, name, point):
        header = self.views[name].horizontalHeader()
        column = header.logicalIndexAt(point)
        if column < 0:
            return
        self._build_header_menu(name, column).exec(header.mapToGlobal(point))

    def _build_toolbar_menu(self, name):
        menu = QMenu(self)
        menu.addAction("Save current tab view", lambda checked=False, table=name: self.save_default_view(table))
        restore_action = menu.addAction("Restore saved tab view", lambda checked=False, table=name: self.restore_default_view(table))
        restore_action.setEnabled(self._saved_default_view(name) is not None)
        return menu

    def _toolbar_context_menu(self, name, global_point):
        self._build_toolbar_menu(name).exec(global_point)

    def _table_context_menu(self, name, point):
        view = self.views[name]
        index = view.indexAt(point)
        if index.isValid() and index not in view.selectedIndexes():
            view.setCurrentIndex(index)
        menu = add_table_copy_actions(QMenu(self), view)
        if name == "Portfolio":
            row = self.selected_row("Portfolio")
            if row:
                menu.addSeparator()
                menu.addAction("Position details", lambda checked=False: self.show_position_details())
        if not menu.isEmpty():
            menu.exec(view.viewport().mapToGlobal(point))

    def _update_selection_info(self, name):
        view = self.views[name]
        indexes = view.selectedIndexes()
        if not indexes:
            self.selection_info[name].setText("No selection")
            return
        if view.selection_axis == "column":
            count = len({index.column() for index in indexes})
            self.selection_info[name].setText(f"{count:,} column{'s' if count != 1 else ''} selected")
        elif view.selection_axis == "row":
            count = len({index.row() for index in indexes})
            self.selection_info[name].setText(f"{count:,} row{'s' if count != 1 else ''} selected")
        else:
            count = len(indexes)
            self.selection_info[name].setText(f"{count:,} cell{'s' if count != 1 else ''} selected")

    @staticmethod
    def _row_identity(name, row):
        if name == "Lots":
            return ("match", row["match_id"]) if row.get("match_id") else ("open", row.get("buy_trade_id"))
        return ("saved", row["id"])

    def _refresh_model(self, name, rows=None):
        model, view = self.models[name], self.views[name]
        vertical_position = view.verticalScrollBar().value()
        horizontal_position = view.horizontalScrollBar().value()
        chosen = [(self._row_identity(name, model.rows[index.row()]), index.column())
                  for index in view.selectedIndexes() if index.row() < len(model.rows)]
        current = view.currentIndex()
        current_key = (self._row_identity(name, model.rows[current.row()]), current.column()) if current.isValid() and current.row() < len(model.rows) else None
        axis = view.selection_axis
        model.refresh(rows)
        column = 0 if name == "Portfolio" else 1
        fit_option_name_column(view, column,
                               (row.get("display_ticker") or row.get("ticker") for row in model.rows),
                               self.db, f"option_name_width_{name}_{self.portfolio_id}")
        positions = {self._row_identity(name, row): number for number, row in enumerate(model.rows)}
        selection = view.selectionModel()
        for number, (key, column) in enumerate(chosen):
            if key in positions:
                selection.select(model.index(positions[key], column),
                                 QItemSelectionModel.ClearAndSelect if number == 0 else QItemSelectionModel.Select)
        if current_key and current_key[0] in positions:
            selection.setCurrentIndex(model.index(positions[current_key[0]], current_key[1]), QItemSelectionModel.NoUpdate)
        view.selection_axis = axis
        view.verticalScrollBar().setValue(vertical_position)
        view.horizontalScrollBar().setValue(horizontal_position)
        self._update_selection_info(name)

    def show_position_details(self):
        position = self.selected_row("Portfolio")
        if not position:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{position['display_ticker']} · position details")
        dialog.resize(950, 650)
        layout = QVBoxLayout(dialog)
        nav = position.get("value")
        layout.addWidget(QLabel(
            f"Quantity {display_number(position['position_qty'])}    ·    Value {display_number(nav)} USD" if nav is not None else
            f"Quantity {display_number(position['position_qty'])}    ·    Value unavailable"))
        layout.addWidget(QLabel(
            f"DTD {self._pct(position.get('one_day'))}    ·    5D {self._pct(position.get('5d'))}"
            f"    ·    1M {self._pct(position.get('1m'))}    ·    3M {self._pct(position.get('3m'))}"
            f"    ·    ADV20 {display_number(position['adv20']) if position.get('adv20') is not None else '—'}"
            f"    ·    Status {position['data_status']}"))
        trades = [t for t in self.service.trade_rows() if t["position_id"] == position["id"]]
        lots = [lot for lot in self.service.lots_rows() if lot.get("position_id") == position["id"]]
        for title, records, specs in (
            ("Trades", trades, [("trade_date", "Date"), ("side", "Side"), ("quantity", "Qty"),
                                ("price", "Price"), ("commission_usd", "Fee USD"), ("match_status", "Match"),
                                ("last_price", "Last price"), ("realized_net", "Realized P&L USD"),
                                ("realized_pct", "Realized P&L %"), ("net_unrealized", "Open P&L USD"),
                                ("unrealized_pct", "Open P&L %")]),
            ("Lots", lots, [("buy_date", "Buy"), ("buy_qty", "Qty"), ("buy_price", "Cost"),
                             ("sell_date", "Sale"), ("realized_net", "Realized USD"),
                             ("net_unrealized", "Open P&L USD")])):
            layout.addWidget(QLabel(title))
            table = ReportTable(len(records), len(specs))
            table.setHorizontalHeaderLabels([label for _, label in specs])
            numeric_keys = {"quantity", "price", "commission_usd", "buy_qty", "buy_price",
                            "sell_price", "realized_net", "net_unrealized", "last_price", "realized_pct", "unrealized_pct"}
            numeric_formats = {column: (2, "percent" if key in ("realized_pct", "unrealized_pct") else "plain") for column, (key, _) in enumerate(specs)
                               if key in numeric_keys}
            table.configure_numbers(self.db, f"position_details_{title.lower()}_decimals_{self.portfolio_id}",
                                    numeric_formats)
            for row_num, record in enumerate(records):
                for col_num, (key, _) in enumerate(specs):
                    value = record.get(key)
                    if key in ("realized_pct", "unrealized_pct") and value is not None:
                        value *= 100
                    table.setItem(row_num, col_num, table.numeric_item(col_num, value)
                                  if key in numeric_keys else SortItem("" if value is None else str(value)))
            table.setSortingEnabled(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    @staticmethod
    def _pct(value):
        return f"{value:,.2%}" if value is not None else "—"

    def _show_all_columns(self, name):
        for column in range(self.models[name].columnCount()):
            self.views[name].showColumn(column)

    def _refresh_models(self):
        portfolio = self.service.portfolio_rows()
        lots = self.service.lots_rows(portfolio)
        trades = self.service.trade_rows(portfolio, lots)
        for name in self.models:
            self._refresh_model(name, portfolio if name == "Portfolio" else lots if name == "Lots" else trades)
        if self.tabs.currentWidget() is self.calcs_tab:
            self._refresh_calcs_live()
        if self.tabs.currentWidget() is self.cash_tab:
            self.cash_tab.refresh()
        if self.tabs.currentWidget() is self.analytics_tab:
            self.analytics_tab.refresh()
        self._refresh_summary(portfolio, lots)

    def _refresh_trade_lots(self):
        portfolio = self.service.portfolio_rows()
        lots = self.service.lots_rows(portfolio)
        self._refresh_model("Trades", self.service.trade_rows(portfolio, lots))
        self._refresh_model("Lots", lots)

    def _refresh_summary(self, rows=None, lots=None):
        self._update_account_heading()
        rows = rows if rows is not None else self.service.portfolio_rows()
        lots = lots if lots is not None else self.service.lots_rows(rows)
        active = [r for r in rows if abs(float(r["position_qty"] or 0)) > 1e-9]
        eligible = [r for r in active if r["reconciliation"] != "DIFFERENCE"]
        daily = sum(r["daily_pnl"] for r in eligible if r["daily_pnl"] is not None)
        realized_rows = [r for r in lots if r.get("realized_net") is not None]
        realized = sum(r["realized_net"] for r in realized_rows)
        pending_realized = sum(r.get("lot_note") == "Fee pending" and
                               str(r.get("lot_state", "")).startswith("Closed") for r in lots)
        missing = [r for r in active if
                   r["value"] is None and r["reconciliation"] != "DIFFERENCE"]
        differences = sum(1 for r in rows if r["reconciliation"] == "DIFFERENCE")
        nav = self.db.effective_nav(self.portfolio_id)
        nav_text = f"${nav:,.2f}" if nav is not None else "—"
        cash = self.db.account_value(self.portfolio_id, "TotalCashValue")
        cash_text = (f"{cash['value']:,.2f} {cash['currency']}" if cash else "—")
        issues = []
        if differences:
            issues.append(f"{differences:,} position difference{'s' if differences != 1 else ''}")
        if pending_realized:
            issues.append(f"{pending_realized:,} closed lot fee{'s' if pending_realized != 1 else ''} pending")
        pending_open = sum(r.get("data_status") == "FEE PENDING" for r in active)
        if pending_open:
            issues.append(f"{pending_open:,} holding fee{'s' if pending_open != 1 else ''} pending")
        if missing:
            tickers = ", ".join(r["display_ticker"] for r in missing[:3])
            suffix = ", …" if len(missing) > 3 else ""
            reason = ("price" if all(r["last_px"] is None for r in missing) else
                      "FX" if all(not r["fx_to_usd"] for r in missing) else "price or FX")
            issues.append(f"{len(missing):,} holding{'s' if len(missing) != 1 else ''} missing {reason}: {tickers}{suffix}")
        incomplete = bool(issues)
        def subtotal(key):
            covered = [r for r in eligible if r.get(key) is not None]
            if not covered:
                return "—"
            amount = sum(r[key] for r in covered)
            return f"{'Partial ' if len(covered) != len(active) else ''}${amount:,.2f}"
        amounts = {
            "nav": nav_text, "cash": cash_text,
            "holdings": subtotal("value"),
            "daily": subtotal("daily_pnl"),
            "open": subtotal("unrealized_pnl"),
            "realized": ("—" if differences or (pending_realized and not realized_rows) else
                         f"{'Partial ' if pending_realized else ''}${realized:,.2f}"),
        }
        for key, text in amounts.items():
            label = self.metric_values[key]
            label.setText(text)
            note = (" Saved session bars may reflect an unfinished trading day and are not live quotes."
                    if key == "daily" and any(str(r.get("price_source", "")).startswith("SAVED SESSION BAR")
                                              for r in eligible) else "")
            label.setToolTip((("Subtotal only: " + "; ".join(issues) + note) if text.startswith("Partial ") else
                              ("Incomplete: " + "; ".join(issues) + note) if text == "—" and incomplete else
                              "Current daily P&L needs a current-session price and prior close" if
                              key == "daily" and text == "—" else
                              "Not available from the connected account" if text == "—" else note.strip()))
            if key == "daily":
                excluded = [r for r in active if r["reconciliation"] == "DIFFERENCE"
                            or r["daily_pnl"] is None]
                if excluded:
                    details = []
                    for row in excluded:
                        reason = row.get("daily_pnl_note")
                        if row["reconciliation"] == "DIFFERENCE":
                            reason = "position quantity differs from broker"
                        elif not reason:
                            reason = ("missing current-session price" if row.get("last_px") is None else
                                      "missing current-session price or prior close")
                        details.append(f"{row['display_ticker']}: {reason}")
                    label.setToolTip(
                        ("Subtotal only" if text.startswith("Partial ") else "Daily P&L unavailable")
                        + f" — excluded {len(excluded)} of {len(active)} holdings:\n"
                        + "\n".join(details) + note)
        daily_label = self.metric_values["daily"]
        trend = "neutral" if incomplete or daily == 0 else "positive" if daily > 0 else "negative"
        if daily_label.property("pnlTrend") != trend:
            daily_label.setProperty("pnlTrend", trend)
            daily_label.style().unpolish(daily_label)
            daily_label.style().polish(daily_label)
        nav_record = self.db.account_value(self.portfolio_id, "NetLiquidation")
        self.metric_values["nav"].setToolTip(
            f"IBKR NetLiquidation, as of {nav_record['as_of']}" if nav_record and
            nav_record["currency"] == "USD" else
            "Mock NAV" if nav is not None else "IBKR NAV unavailable")
        if cash:
            self.metric_values["cash"].setToolTip(
                f"IBKR TotalCashValue, as of {cash['as_of']}; includes non-trade cash flows")
        self.data_notice.setText(" · ".join(issues) + " — review reconciliation and source data" if issues else "")
        self.data_notice.setVisible(bool(issues))
        self._update_status()

    def match_selected(self):
        sell = self.selected_row("Trades")
        if not sell or sell["side"] != "SELL":
            QMessageBox.information(self, "Select a SELL", "Select a SELL row in Trades first.")
            return
        dialog = MatchDialog(self.service.matching, sell, self)
        if dialog.exec() == QDialog.Accepted:
            try:
                self.service.matching.replace_sell_matches(sell["id"], dialog.allocations())
                self._refresh_models()
            except Exception as exc:
                QMessageBox.warning(self, "Match rejected", str(exc))

    def auto_match(self):
        try:
            self.service.matching.rebuild_auto()
            self._refresh_models()
        except Exception as exc:
            QMessageBox.warning(self, "Matching failed", str(exc))

    def clear_match(self):
        lot = self.selected_row("Lots")
        if lot and lot.get("match_id"):
            self.service.matching.clear(lot["match_id"])
            self._refresh_models()

    def backup(self):
        try:
            path = self.db.backup()
            QMessageBox.information(self, "Encrypted local backup created", str(path)
                                    + "\n\nThis backup requires the original Windows profile key. "
                                    "Export a portable recovery backup to recover after losing that profile.")
        except Exception as exc:
            QMessageBox.warning(self, "Backup failed", str(exc))

    def export_recovery_backup(self):
        from app.ui.recovery_ui import export_portable
        return export_portable(self, self.db)

    def restore_local_backup(self):
        from app.ui.recovery_ui import restore_backup
        return restore_backup(self, portable=False)

    def restore_recovery_backup(self):
        from app.ui.recovery_ui import restore_backup
        return restore_backup(self, portable=True)

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export workbook", "portfolio.xlsx", "Excel workbook (*.xlsx)")
        if path:
            try:
                export_workbook(path, self.models["Portfolio"].rows,
                                self.models["Trades"].rows,
                                self.models["Lots"].rows, COLUMNS)
            except Exception as exc:
                QMessageBox.warning(self, "Export failed", str(exc))

    def apply_settings(self):
        try:
            values = {}
            for key, widget in self.settings_fields.items():
                value = widget.currentText() if isinstance(widget, QComboBox) else widget.text().strip()
                if key == "provider" and value == "Demo":
                    value = "Mock"
                values[key] = "1" if key == "autosave" and value == "On" else "0" if key == "autosave" else value
            if int(values["ib_port"]) <= 0 or int(values["ib_client_id"]) < 0 or int(values["refresh_seconds"]) < 1:
                raise ValueError("Port, client ID, and refresh interval must be valid positive numbers")
            self.db.validate_account_change(values["ib_account_id"])
            portfolio_name = self.db.validate_portfolio_name(self.portfolio_id, values.pop("portfolio_name"))
            directory_changed = (not self._database_path_override and
                                 values["data_directory"] != self.db.setting("data_directory"))
            target_dir = validate_data_directory(values["data_directory"] or self.db.path.parent)
            if values["backup_directory"]:
                validate_data_directory(values["backup_directory"])
            target = target_dir / "portfolio.sqlite3"
            pending_move = directory_changed and target.resolve() != self.db.path.resolve()
            if pending_move and target.exists():
                raise FileExistsError(f"A portfolio database already exists at {target}")
            name_changed = portfolio_name != self.db.portfolio_name(self.portfolio_id)
            settings_changed = any(value != self.db.setting(key) for key, value in values.items()
                                   if key != "data_directory" or not self._database_path_override)
            if name_changed:
                self.db.rename_portfolio(self.portfolio_id, portfolio_name)
                self.setWindowTitle(window_title(portfolio_name))
            self.settings_fields["portfolio_name"].setText(portfolio_name)
            if name_changed and not settings_changed:
                self.statusBar().showMessage("Portfolio renamed", 5000)
                return
            for key, value in values.items():
                if key == "data_directory" and self._database_path_override:
                    continue
                self.db.set_setting(key, value)
            self._pending_data_directory = values["data_directory"] if pending_move else None
            self._set_refresh_interval()
            self._live_sync_pending = False
            self._live_sync_events.clear()
            self._start_provider()
            self._sync_progress_active = True
            if isinstance(self.market, IBKRMarketDataProvider):
                self._watch_live_sync()
            self._queue_configured_syncs()
            self._update_sync_button()
            self._refresh_models()
            if directory_changed and pending_move:
                self.statusBar().showMessage("Database will move when the app closes; restart afterward", 10000)
        except Exception as exc:
            QMessageBox.warning(self, "Settings rejected", str(exc))

    def _set_refresh_interval(self):
        self.refresh_timer.start(max(1, int(self.db.setting("refresh_seconds") or 30)) * 1000)

    def _stale_contracts(self, force=False):
        today = date.today().isoformat()
        current = ({row["con_id"] for row in self.db.broker_positions(self.portfolio_id)}
                   if self.db.metadata(f"broker_positions_refresh_{self.portfolio_id}") else None)
        return {p["con_id"] for p in self.db.rows("positions", self.portfolio_id) if p.get("con_id") and
                (current is None or p["con_id"] in current) and
                (force or self.db.metadata(f"history_refresh_{p['con_id']}") != today)}

    def _start_provider(self, force_history=False):
        if self._update_blocked():
            return
        if isinstance(self.market, IBKRMarketDataProvider):
            self.market.stop()
        provider = self.db.setting("provider")
        if provider == "IBKR":
            self._connection_error = ""
            self._last_connection_attempt = time.monotonic()
            self._disconnected_since = None
            try:
                self.market = IBKRMarketDataProvider(self.db.setting("ib_host"), self.db.setting("ib_port"),
                                                     self.db.setting("ib_client_id"), self.db.setting("data_mode"),
                                                     account_id=self.db.setting("ib_account_id"))
                self.service.market = self.market
                self._load_cached_quotes()
                current = ({row["con_id"] for row in self.db.broker_positions(self.portfolio_id)}
                           if self.db.metadata(f"broker_positions_refresh_{self.portfolio_id}") else None)
                subscriptions = [p for p in self.db.rows("positions", self.portfolio_id)
                                 if current is None or not p.get("con_id") or p["con_id"] in current]
                self.market.set_idea_contracts(self.calcs_tab.ideas)
                stale = self._stale_contracts(force_history)
                last_days = {} if force_history else {int(con_id): self.db.conn.execute(
                    "SELECT MAX(day) FROM historical_prices WHERE con_id=?", (con_id,)).fetchone()[0]
                    for con_id in stale}
                self.market.start(subscriptions, stale,
                                  trades=self.db.rows("trades", self.portfolio_id),
                                  benchmark_last_day=self.db.benchmark_latest_day(),
                                  history_last_days=last_days)
                self._history_attempt_day = date.today()
            except Exception as exc:
                self._connection_error = str(exc)
                self.market = MarketDataProvider()
                self.service.market = self.market
                self._load_cached_quotes()
        else:
            self.market = MockMarketDataProvider()
            self.service.market = self.market
            self.market.refresh(self.db.rows("positions", self.portfolio_id))
        self.calcs_tab.set_market_provider(self.market)
        self._refresh_models()

    def _load_cached_quotes(self):
        for position_id, saved in self.db.cached_quotes(
                self.portfolio_id, self.db.setting("ib_account_id")).items():
            if valid_price(saved["price"]) is not None:
                self.market.quotes[position_id] = {
                    "ticks": {}, "cached_price": saved["price"],
                    "cached_as_of": saved["as_of"], "mode": "Cached",
                    "status": "CACHED QUOTE"}

    def refresh_market(self, *, force=False):
        if isinstance(self.market, MockMarketDataProvider):
            ids = self.market.refresh(self.db.rows("positions", self.portfolio_id))
            self.models["Portfolio"].update_positions(ids)
            self._refresh_trade_lots()
            self._update_status()
        elif not force and time.monotonic() - self._last_connection_attempt < 3:
            self._update_status()
        else:
            self._start_provider(force_history=False)

    def _periodic_refresh(self):
        stamp = self.db.metadata("ibkr_flex_sync_completed")
        if stamp and stamp != self._last_flex_sync_stamp:
            self._last_flex_sync_stamp = stamp
            self._refresh_models()
        if self._analytics_process is not None and self._analytics_process.poll() is not None:
            self._analytics_process = None
            self.analytics_tab.refresh()
        if self._cash_process is not None and self._cash_process.poll() is not None:
            self._cash_process = None
            self.cash_tab.refresh()
        analytics_stamp = self.db.metadata("ibkr_analytics_sync_completed")
        if analytics_stamp and analytics_stamp != self._last_analytics_sync_stamp:
            self._last_analytics_sync_stamp = analytics_stamp
            self.analytics_tab.refresh()
        cash_stamp = self.db.metadata("ibkr_cash_sync_completed")
        self._advance_sync_queue()
        running = any(process is not None and process.poll() is None for process in
                      (self._sync_process, self._analytics_process, self._cash_process))
        if not self._sync_all_active and not running and not self._history_running():
            today = date.today().isoformat()
            if (self.db.setting("flex_query_id") and not trade_sync_paused(self.db)
                    and (not stamp or stamp[:10] != today)):
                self._launch_flex_sync()
            elif (self.db.setting("analytics_flex_query_id") and
                  (not analytics_stamp or analytics_stamp[:10] != today)):
                self._launch_analytics_sync()
            elif self.db.setting("cash_flex_query_id") and (not cash_stamp or cash_stamp[:10] != today):
                self._launch_cash_sync()
        self._update_sync_button()
        if isinstance(self.market, MockMarketDataProvider):
            self.refresh_market()
        elif (self.db.setting("provider") == "IBKR" and
              (not isinstance(self.market, IBKRMarketDataProvider) or
               self.market.thread is None or not self.market.thread.is_alive() or
               (self._disconnected_since is not None and
                time.monotonic() - self._disconnected_since >= 60)) and
              time.monotonic() - self._last_connection_attempt >= 30):
            self._start_provider(force_history=False)
        elif self._history_attempt_day != date.today() and self._stale_contracts():
            self._start_provider(force_history=False)

    def _remember_broker_instrument(self, broker_row):
        positions = self.db.rows("positions", self.portfolio_id)
        if any(p["con_id"] == broker_row["con_id"] for p in positions):
            return False
        symbol = broker_row["symbol"]
        candidate = next((p for p in positions if p["con_id"] is None and
                          p["ib_symbol"] == symbol and
                          p["security_type"] == broker_row["security_type"]), None)
        if candidate is None:
            position_id = self.db.add_position({
                "portfolio_id": self.portfolio_id,
                "display_ticker": broker_row["local_symbol"] if broker_row["security_type"] == "OPT" else symbol,
                "ib_symbol": symbol, "security_type": broker_row["security_type"],
                "currency": broker_row["currency"]})
        else:
            position_id = candidate["id"]
        try:
            multiplier = float(broker_row["multiplier"]) if broker_row["multiplier"] else None
        except (TypeError, ValueError):
            multiplier = None
        self.db.cache_contract(position_id, broker_row["con_id"], multiplier=multiplier,
                               details=broker_row)
        return position_id if candidate is None else None

    def _poll(self):
        was_connected = self.market.connected
        ids = self.market.drain()
        if was_connected and not self.market.connected:
            self._disconnected_since = time.monotonic()
        elif self.market.connected:
            self._disconnected_since = None
        self.calcs_tab.process_search_results()
        if self._live_sync_pending and (
                (was_connected and not self.market.connected) or
                (isinstance(self.market, IBKRMarketDataProvider) and not self.market.connected and
                 self.market.thread is not None and not self.market.thread.is_alive())):
            self._live_sync_pending = False
            self._finish_live_sync(success=False)
        if (not self._sync_all_active and not was_connected and self.market.connected and
            isinstance(self.market, IBKRMarketDataProvider)):
            self._launch_flex_sync()
        if ids:
            if any(str(position_id).startswith("idea:") for position_id in ids):
                self.calcs_tab.cache_idea_quotes(ids)
            self.models["Portfolio"].update_positions(ids)
            if not self.summary_refresh_timer.isActive():
                self.summary_refresh_timer.start(1000)
            if (self.tabs.currentWidget() is self.analytics_tab and
                    self.analytics_tab.sections.currentWidget() in
                    (self.analytics_tab.exposure_page, self.analytics_tab.returns_page) and
                    time.monotonic() - getattr(self, "_last_exposure_refresh", 0) >= 2):
                if self.analytics_tab.sections.currentWidget() is self.analytics_tab.returns_page:
                    self.analytics_tab.returns_page.refresh()
                else:
                    self.analytics_tab.refresh_exposure()
                self._last_exposure_refresh = time.monotonic()
            if (self.tabs.currentWidget() is self.calcs_tab and not self.calcs_tab.updates_paused and
                    not self.calcs_refresh_timer.isActive()):
                self.calcs_refresh_timer.start(1000)
            if not self.lots_refresh_timer.isActive():
                self.lots_refresh_timer.start(1000)
            if isinstance(self.market, IBKRMarketDataProvider) and (
                    not self.market.connected or time.monotonic() - self._last_quote_cache_at >= 15):
                account = self.db.setting("ib_account_id")
                positions = {p["id"]: p for p in self.db.rows("positions", self.portfolio_id)}
                saved = []
                for position_id in ids:
                    quote = self.market.quote(position_id)
                    if self.market.connected:
                        price, source = choose_price(quote.get("ticks", {}), quote.get("mode", "Live"))
                        as_of = quote.get("updated")
                    else:
                        price, source = quote.get("cached_price"), "CACHED QUOTE"
                        as_of = quote.get("cached_as_of")
                    if position_id in positions and valid_price(price) is not None and as_of:
                        saved.append((position_id, positions[position_id]["con_id"], price,
                                      source, as_of))
                if account and saved:
                    self.db.save_quotes(self.portfolio_id, account, saved)
                    if self.market.connected:
                        self._last_quote_cache_at = time.monotonic()
        if self.market.other_events:
            events = self.market.other_events[:]
            self.market.other_events.clear()
            new_instruments = set()
            exposure_updated = False
            fx_updates = {}
            execution_records = []
            needs_model_refresh = False
            for event in events:
                if self._live_sync_pending and event[0] in (
                        "broker_positions", "account_summary_end", "executions_end"):
                    self._live_sync_events.add(event[0])
                if event[0] == "contract":
                    _, position_id, con_id, tz, multiplier, details = event
                    try:
                        numeric_multiplier = float(multiplier) if multiplier else None
                    except (TypeError, ValueError):
                        numeric_multiplier = None
                    self.db.cache_contract(position_id, con_id, tz, numeric_multiplier, details)
                    self.models["Portfolio"].update_positions({position_id})
                elif event[0] == "exposure_calculation_error":
                    self.analytics_tab.exposure_page.calculation_errors[event[1]] = event[2]
                    exposure_updated = True
                elif event[0].startswith("exposure_"):
                    from app.finance.exposure import save_broker_fact
                    if save_broker_fact(self.db, self.portfolio_id, event):
                        if event[0] == "exposure_delta":
                            self.analytics_tab.exposure_page.calculation_errors.pop(event[1], None)
                        exposure_updated = True
                elif event[0] == "accounts":
                    accounts = event[1]
                    if not self.db.setting("ib_account_id") and len(accounts) == 1:
                        try:
                            self.db.validate_account_change(accounts[0])
                        except ValueError:
                            self._connection_error = "Set the IBKR account in a separate database"
                        else:
                            self.db.set_setting("ib_account_id", accounts[0])
                            self.settings_fields["ib_account_id"].setText(accounts[0])
                            self._update_account_heading()
                elif event[0] == "account_value":
                    _, account, metric, raw, currency = event
                    if account == self.db.setting("ib_account_id"):
                        try:
                            self.db.set_account_value(self.portfolio_id, metric, raw, currency,
                                                      datetime.now().isoformat(timespec="seconds"))
                            needs_model_refresh = True
                        except ValueError:
                            pass
                elif event[0] == "daily_pnl":
                    _, account, value = event
                    if (account == self.db.setting("ib_account_id") and
                            isinstance(value, (int, float)) and math.isfinite(value) and abs(value) < 1e15):
                        self.db.set_daily_pnl(self.portfolio_id, value,
                                              datetime.now().isoformat(timespec="seconds"))
                        if (self.tabs.currentWidget() is self.analytics_tab and
                                time.monotonic() - getattr(self, "_last_analytics_refresh", 0) >= 5):
                            self.analytics_tab.refresh()
                            self._last_analytics_refresh = time.monotonic()
                elif event[0] == "broker_positions":
                    account = self.db.setting("ib_account_id")
                    snapshots = event[1]
                    managed = event[2] if len(event) > 2 else []
                    if not account and len(snapshots) == 1:
                        account = next(iter(snapshots))
                        try:
                            self.db.validate_account_change(account)
                        except ValueError:
                            account = ""
                            self._connection_error = "Set the IBKR account in a separate database"
                        else:
                            self.db.set_setting("ib_account_id", account)
                            self.settings_fields["ib_account_id"].setText(account)
                            self._update_account_heading()
                    if account and (account in snapshots or account in managed):
                        broker_rows = [r for r in snapshots.get(account, []) if r["security_type"] != "CASH"]
                        self.db.save_broker_positions(self.portfolio_id, broker_rows,
                                                      datetime.now().isoformat(timespec="seconds"))
                        self.db.set_metadata(f"broker_positions_refresh_{self.portfolio_id}",
                                             datetime.now().isoformat(timespec="seconds"))
                        for broker_row in broker_rows:
                            new_id = self._remember_broker_instrument(broker_row)
                            if new_id is not None:
                                new_instruments.add(new_id)
                        needs_model_refresh = True
                elif event[0] == "broker_position_delta":
                    _, account, broker_row = event
                    if account == self.db.setting("ib_account_id") and broker_row["security_type"] != "CASH":
                        now = datetime.now().isoformat(timespec="seconds")
                        self.db.save_broker_position_delta(self.portfolio_id, broker_row, now)
                        self.db.set_metadata(f"broker_positions_refresh_{self.portfolio_id}", now)
                        if abs(broker_row["quantity"]) > 1e-9:
                            new_id = self._remember_broker_instrument(broker_row)
                            if new_id is not None:
                                new_instruments.add(new_id)
                        needs_model_refresh = True
                elif event[0] == "fx":
                    _, currency, rate = event
                    fx_updates[currency] = rate
                elif event[0] == "execution":
                    execution_records.append(event[1])
                elif event[0] == "executions_end":
                    self.db.set_metadata("ibkr_last_execution_poll",
                                         datetime.now().isoformat(timespec="seconds"))
                elif event[0] == "execution_error":
                    self.db.set_metadata("ibkr_last_execution_error", event[1])
                elif event[0] == "fx_history":
                    _, currency, bars = event
                    if bars:
                        self.db.save_fx_history(currency, bars)
                        self.service.matching.rebuild_auto()
                        needs_model_refresh = True
                elif event[0] == "benchmark_history":
                    us_now = datetime.now(ZoneInfo("America/New_York"))
                    last_completed = (us_now.date() if us_now.hour >= 17 else
                                      us_now.date() - timedelta(days=1))
                    completed = [bar for bar in event[1]
                                 if bar["day"] <= last_completed.isoformat()]
                    if self.db.save_benchmark_history(completed):
                        self.db.set_metadata("ibkr_benchmark_error", "")
                    elif not self.db.benchmark_latest_day():
                        self.db.set_metadata("ibkr_benchmark_error",
                                             "IBKR returned no completed S&P 500 daily closes")
                    self.analytics_tab.refresh()
                elif event[0] == "benchmark_error":
                    self.db.set_metadata("ibkr_benchmark_error", event[1])
                    self.analytics_tab.refresh()
                elif event[0] == "history":
                    _, con_id, bars = event
                    tz_row = self.db.conn.execute("SELECT time_zone FROM contracts WHERE con_id=? LIMIT 1", (con_id,)).fetchone()
                    try:
                        today = datetime.now(ZoneInfo(tz_row[0])).date() if tz_row and tz_row[0] else date.today()
                    except Exception:
                        today = date.today()
                    completed = [bar for bar in bars if bar["day"] < today.isoformat()]
                    if completed:
                        self.db.save_history(con_id, completed)
                        self.models["Portfolio"].update_positions({p["id"] for p in self.db.rows("positions", self.portfolio_id) if p.get("con_id") == con_id})
                        self._refresh_trade_lots()
                        if not self.summary_refresh_timer.isActive():
                            self.summary_refresh_timer.start(1000)
            if (exposure_updated and self.tabs.currentWidget() is self.analytics_tab and
                    self.analytics_tab.sections.currentWidget() is self.analytics_tab.exposure_page and
                    time.monotonic() - getattr(self, "_last_exposure_refresh", 0) >= 2):
                self.analytics_tab.refresh_exposure()
                self._last_exposure_refresh = time.monotonic()
            if execution_records:
                try:
                    outcome = BrokerIngestor(self.db, self.db.setting("ib_account_id"),
                                             self.portfolio_id).ingest_many(execution_records, "tws")
                    if outcome.changed_position_ids:
                        self.service.matching.rebuild_auto(outcome.changed_position_ids)
                        needs_model_refresh = True
                    self.db.set_metadata("ibkr_last_execution_error", "")
                except Exception as exc:
                    self.db.set_metadata("ibkr_last_execution_error", str(exc))
            if fx_updates:
                for currency, rate in fx_updates.items():
                    self.db.set_fx_rate(currency, date.today().isoformat(), rate, "IBKR live")
                needs_model_refresh = True
            if new_instruments and isinstance(self.market, IBKRMarketDataProvider):
                self.market.add_positions([p for p in self.db.rows("positions", self.portfolio_id)
                                           if p["id"] in new_instruments])
            if needs_model_refresh:
                self._refresh_models()
        required_live_events = {"broker_positions", "account_summary_end"}
        if self.db.setting("ib_account_id"):
            required_live_events.add("executions_end")
        if self._live_sync_pending and required_live_events <= self._live_sync_events:
            self._live_sync_pending = False
            self._finish_live_sync(success=True)
        self._advance_sync_queue()
        self._update_sync_button()
        self._update_status()

    def _update_status(self):
        connected = "Connected" if self.market.connected else "Disconnected"
        if isinstance(self.market, MockMarketDataProvider):
            connected = "Mock"
        elif self.db.setting("provider") == "IBKR" and not self.market.connected:
            connected = "Offline"
        active = [row for row in self.models["Portfolio"].rows
                  if abs(float(row.get("position_qty") or 0)) > 1e-9]
        sources = [str(row.get("price_source") or "") for row in active]
        live = sum(source == "LIVE LAST" for source in sources)
        delayed = sum(source == "DELAYED LAST" for source in sources)
        frozen = sum(source == "FROZEN LAST" for source in sources)
        current = live + delayed + frozen
        total = len(active)
        if connected == "Mock":
            mode = "Mock"
        elif connected == "Offline":
            mode = "Saved data" if any(row.get("last_px") is not None for row in active) else "Unavailable"
        elif not total:
            mode = "Waiting for positions"
        elif not current:
            mode = ("Saved prices; waiting for current quotes" if
                    any(row.get("last_px") is not None for row in active) else "Waiting for prices")
        else:
            kind = ("Live" if live else "Delayed" if delayed else "Frozen")
            mode = (f"{kind} ({current}/{total})" if current == total and
                    max(live, delayed, frozen) == total else
                    f"Partial {kind.lower()} ({current}/{total})")
        updated = max((row.get("last_updated") or "" for row in active), default="") or "—"
        quote_issues = []
        for row in active:
            status = str(self.market.quote(row["id"]).get("status") or "")
            if status and status not in ("OK", "DELAYED", "CACHED QUOTE", "MOCK", "WAITING FOR PRICE"):
                quote_issues.append(f"{row['display_ticker']}: {status}")
        issues_text = "; ".join(quote_issues[:3])
        if len(quote_issues) > 3:
            issues_text += f"; and {len(quote_issues) - 3} more"
        last_sync = (self.db.metadata("ibkr_flex_sync_completed") or "—").replace("T", " ")
        sync_status = self._trade_flex_display_status()
        if sync_status in ("failed", "paused"):
            link = status_link(self.theme, "details", sync_status)
            self.trade_sync_status.setText(
                f"Trades synced: {escape(last_sync)} ({link})")
            self.trade_sync_status.setToolTip("Click for the trade sync error and diagnostic log")
            self.trade_sync_status.setCursor(Qt.PointingHandCursor)
        else:
            self.trade_sync_status.setText(f"Trades synced: {escape(last_sync)} ({escape(sync_status)})")
            self.trade_sync_status.setToolTip("")
            self.trade_sync_status.unsetCursor()
        recent_check = (self.db.metadata("ibkr_last_execution_poll") or "—").replace("T", " ")
        self.statusBar().showMessage(
            f"IBKR: {connected}    Data: {mode}    Last price: {updated}"
            f"{'    Quote issues: ' + str(len(quote_issues)) if quote_issues else ''}")
        last_history = self.db.conn.execute("SELECT MAX(value) FROM app_metadata WHERE key LIKE 'history_refresh_%'").fetchone()[0] or "—"
        warnings = []
        if quote_issues:
            warnings.append(f"Quote issues: {issues_text}")
        if self._connection_error:
            warnings.append(f"Connection: {self._connection_error}")
        flex_error = self.db.metadata('ibkr_flex_sync_error')
        if sync_status in ("failed", "paused"):
            warnings.append(f"Trades sync {sync_status}: {flex_error or 'See technical details.'}")
        details = (
            f"Last historical refresh: {last_history}\n"
            f"Recent Gateway executions checked: {recent_check}\n"
            f"Historical Flex sync: {last_sync} ({sync_status})"
            f"\nFlex sync details: {flex_error or 'None'}\n"
            f"Connection details: {self._connection_error or 'None'}\n"
            f"Quote details: {'; '.join(quote_issues) or 'None'}\nDatabase: {self.db.path}")
        self.settings_status.update_status(self.db, self.portfolio_id, {
            "Connection": connected, "Market data": mode,
            "Subscribed symbols": f"{self.market.subscriptions:,}",
            "Current prices": f"{current}/{total}",
        }, warnings, details, refresh_history=(self.settings_dialog.isVisible() or
                                              not hasattr(self, "_settings_status_initialized")))
        self._settings_status_initialized = True
