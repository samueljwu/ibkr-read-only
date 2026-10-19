"""Native portfolio monitor UI."""

from datetime import date, datetime, timedelta
from html import escape
import json
import math
import shutil
from secrets import token_hex
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QByteArray, QItemSelectionModel, QModelIndex, QRect, QSize, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                               QGridLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox, QProgressBar, QProgressDialog, QPushButton, QTabWidget,
                               QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QTableView, QToolButton, QVBoxLayout, QWidget)

from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics, diagnostic_log_path
from app.platform import secret_store
from app.platform.history_status import pause_request_path, read_status, write_status
from app.finance.calculations import choose_price, valid_price
from app.finance.option_labels import option_label
from app.views.analytics_ui import AnalyticsTab
from app.views.cash_ui import CashMovementsTab
from app.views.calcs_ui import CalcsTab
from app.broker.broker_sync import BrokerIngestor
from app.storage.excel_io import export_workbook
from app.market.market_data import IBKRMarketDataProvider, MarketDataProvider, MockMarketDataProvider
from app.finance.services import PortfolioService
from app.flex.flex_import import flex_today, oldest_flex_date
from app.sync.sync_service import trade_sync_paused
from app.views.table_ui import (ReportTable, SortItem, TableClickAwayFilter,
                                copy_table_selection, display_number, handle_table_shortcut)
from app.views.ui_theme import (COLORS, TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING,
                          stylesheet, style_toolbar_dropdown)
from app.views.themed_checkbox import ThemedCheckBox
from app.views.help_visuals import HelpScreenshot
from app.views.history_import_ui import HistoryImportDialog
from app.views.action_icons import action_icon
from app.views.shortcuts import native_text, shortcut
from app.views.lot_matching_ui import LotMatchDialog
from app.views.tutorial_ui import TutorialOverlay
from app.views.update_dialog import UpdateDialog
from app.views import widgets
from app.views.widgets import enable_label_copy


def col(key, label, kind="text"):
    return key, label, kind


PORTFOLIO = [
    col("display_ticker", "Instrument"), col("position_qty", "Quantity", "number"),
    col("average_cost", "Open cost", "price"), col("last_px", "Last price", "price"),
    col("value", "Value USD", "money"), col("daily_pnl", "Daily P&L", "money"),
    col("unrealized_pnl", "Open P&L", "money"),
    col("unrealized_pct", "Open P&L %", "pct"), col("portfolio_pct", "Weight", "pct"),
    col("one_day", "DTD", "pct"), col("mtd", "MTD", "pct"), col("ytd", "YTD", "pct"),
    col("reconciliation", "Reconciliation"), col("data_status", "Data status"),
    col("price_source", "Price source"), col("last_updated", "Updated"),
]
TRADES = [
    col("trade_date", "Date", "date"), col("display_ticker", "Instrument"),
    col("side", "Side"), col("quantity", "Quantity", "number"),
    col("price", "Price", "price"), col("commission_usd", "Fee/rebate USD", "money"),
    col("accrued_interest_usd", "Accrued interest USD", "money"),
    col("gross_usd", "Value USD", "money"), col("match_status", "Status"),
    col("matched_qty", "Matched", "number"), col("remaining_qty", "Remaining", "number"),
    col("valuation_status", "Data status"), col("trade_id", "Reference"),
    col("source", "Origin"),
    col("realized_net", "Realized P&L USD", "money"),
    col("realized_pct", "Realized P&L %", "pct"),
    col("net_unrealized", "Open P&L USD", "money"),
    col("unrealized_pct", "Open P&L %", "pct"),
]
LOTS = [
    col("lot_state", "Status"), col("ticker", "Instrument"), col("buy_date", "Bought", "date"),
    col("buy_qty", "Shares", "number"), col("buy_price", "Buy price", "price"),
    col("sell_date", "Sold", "date"), col("sell_price", "Sell price", "price"),
    col("realized_net", "Realized USD", "money"),
    col("net_unrealized", "Open P&L USD", "money"),
    col("lot_note", "Note"),
    col("buy_commission", "Buy fees", "money"), col("sell_commission", "Sell fees", "money"),
    col("buy_accrued_interest", "Buy accrued interest", "money"),
    col("sell_accrued_interest", "Sell accrued interest", "money"),
    col("duration", "Days held", "number"),
    col("match_origin", "Match"),
    # Append logical columns to preserve saved sort indices and header layouts.
    col("realized_pct", "Realized P&L %", "pct"),
    col("unrealized_pct", "Open P&L %", "pct"),
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
        self.source_rows = []
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
                # Open P&L % was inserted after Open P&L at index 7.
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
        self.source_rows = list(all_rows)
        needle = self.filter_text.casefold()
        self.rows = [row for row in self.source_rows if not needle or needle in str(
            row.get("display_ticker") or row.get("ticker") or "").casefold()]
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
        self.refresh(self.source_rows)

    def set_side_filter(self, value):
        if self.name == "Trades":
            self.side_filter = value if value in ("All", "BUY", "SELL") else "All"
            self.refresh(self.source_rows)

    def set_hide_closed(self, checked):
        if self.name == "Portfolio":
            self.hide_closed = bool(checked)
            self.db.set_setting("hide_closed_positions", "1" if checked else "0")
            self.refresh(self.source_rows)

    def set_lot_filter(self, value):
        self.lot_filter = value
        self.refresh(self.source_rows)

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

    def update_positions(self, ids, rows=None):
        if self.name != "Portfolio" or not ids:
            return
        updated = {row["id"]: row for row in
                   (rows if rows is not None else self.service.portfolio_rows())}
        for number, row in enumerate(self.source_rows):
            if row["id"] in ids and row["id"] in updated:
                self.source_rows[number] = updated[row["id"]]
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
            if key == "commission_usd" and not row.get("commission_complete", True):
                return "Fee or rebate has not arrived from IBKR; P&L excludes it for now."
            if self.name == "Portfolio" and row.get("estimated_baseline") and key in (
                    "average_cost", "unrealized_pnl", "unrealized_pct", "data_status"):
                return (f"Estimated from the Activity Statement closing market price on "
                        f"{row['estimated_baseline']}. This is a performance starting value, "
                        "not the original purchase cost or tax basis; earlier gains are excluded.")
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
            if key == "daily_pnl" and row.get("daily_pnl_fee_pending"):
                return ("Estimated daily P&L excludes fees or rebates pending from IBKR. "
                        "It will update when they arrive.")
            if self.name == "Portfolio" and key in ("average_cost", "unrealized_pnl", "unrealized_pct") and row.get("open_pnl_fee_pending"):
                return "Estimated without pending fees or rebates; this value will update when they arrive."
            if self.name in ("Lots", "Trades") and key in ("realized_net", "realized_pct", "net_unrealized", "unrealized_pct") and row.get("fee_pending"):
                return "Estimated without pending fees or rebates; this value will update when they arrive."
            return {"gross_usd": "Gross trade value in USD, before commission.",
                    "one_day": "Price change from the prior session close in the instrument's currency. Excludes FX, trades, fees and position direction.",
                    "daily_pnl": "Security-position P&L in USD since the prior session, including today's trades, their commissions and FX changes. Excludes cash income, bond accrued interest and standalone FX effects.",
                    "accrued_interest_usd": "Bond accrued interest transferred at trade settlement; "
                                            "negative on BUY (cash paid out), positive on SELL (cash received). "
                                            "Distinct from periodic bond coupon income.",
                    "realized_net": "Realized clean-price P&L on matched shares after allocated commissions. Bond accrued interest is reported separately as interest. Shown on both trades in each match.",
                    "realized_pct": "Realized clean-price P&L divided by matched shares' clean-price cost and allocated buy commission. Shown on both trades in each match.",
                    "net_unrealized": "Open clean-price P&L on shares still held from this buy, after allocated buy commission. Bond accrued interest is separate.",
                    "unrealized_pct": ("Open P&L divided by the cost and allocated buy commissions of all shares still held."
                                       if self.name == "Portfolio" else
                                       "Open clean-price P&L divided by the remaining shares' clean-price cost and allocated buy commission."),
                    "match_status": "BUY shares stay open until matched to a sale. SELL shares need eligible buys."}.get(key)
        if role in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.EditRole:
                return "" if value is None else str(value)
            if value is None or value == "":
                if value is None and key in (
                    "gross_usd", "value", "daily_pnl", "unrealized_pnl", "portfolio_pct",
                    "realized_net", "realized_pct", "net_unrealized", "unrealized_pct"):
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
            palette = COLORS[self.theme]
            if key in ("daily_pnl", "unrealized_pnl", "realized_net", "realized_pct",
                       "net_unrealized", "unrealized_pct") and isinstance(value, (int, float)):
                return QColor(palette["positive"] if value > 0 else
                              palette["negative"] if value < 0 else
                              palette["grid_neutral_text"])
            if key in ("reconciliation", "data_status", "valuation_status") and value in (
                "DIFFERENCE", "CONTRACT ERROR", "CONTRACT DETAILS REQUIRED",
                "MULTIPLIER REQUIRED", "FX REQUIRED", "VERIFY SECURITY TYPE",
                "NO MARKET DATA PERMISSION", "MOCK PRICE UNAVAILABLE", "UNMATCHED SALE", "NO TRADES",
                "POSITION DIFFERENCE", "SAVED SESSION BAR", "CACHED CLOSE", "STALE QUOTE"):
                return QColor(palette["grid_warning_text"])
            return QColor(palette["grid_default_text"])
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
        palette = COLORS[model.theme]
        painter.setPen(QColor(palette["line"]))
        painter.drawLine(axis, bar.top() - 1, axis, bar.bottom())
        if width:
            color = QColor(palette["bar_positive"] if value > 0 else palette["bar_negative"])
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


class MainWindow(QMainWindow):
    DEFAULT_TAB_ORDER = ("Portfolio", "Trades", "Lots", "Cash", "Analytics", "Rebalance")
    TAB_LAYOUT_KEY = "tab_layout_v1"
    MIN_ZOOM = 70
    MAX_ZOOM = 150
    ZOOM_STEP = 10

    def __init__(self, database_path=None, *, recovery_key=None,
                 on_manage_portfolios=None):
        super().__init__()
        self.setWindowTitle("IBKR Dashboard")
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1450, max(640, available.width() - 40)),
                    min(850, max(480, available.height() - 40)))
        self.db = (Database(database_path, recovery_key=recovery_key) if recovery_key is not None else
                   Database(database_path) if database_path else Database())
        self._last_db_data_version = self.db.conn.execute("PRAGMA data_version").fetchone()[0]
        self._on_manage_portfolios = on_manage_portfolios
        self._base_font = self.font()
        saved_zoom = self.db.setting("ui_zoom_percent")
        try:
            zoom = int(saved_zoom)
        except (TypeError, ValueError):
            zoom = 100
        self.zoom_percent = zoom if self.MIN_ZOOM <= zoom <= self.MAX_ZOOM and zoom % self.ZOOM_STEP == 0 else 100
        self._move_process = None
        self._move_status_path = None
        self._move_progress = None
        self._move_source_path = None
        self.log = configure_diagnostics(self.db.path, "monitor")
        self.log.info("IBKR Dashboard opened; database=%s", self.db.path)
        self.portfolio_id = 1
        self._show_account_summary = self.db.setting("show_account_summary") != "0"
        self._compact_geometry = None
        self._history_attempt_day = None
        self._last_flex_sync_stamp = self.db.metadata("ibkr_flex_sync_completed")
        self._last_analytics_sync_stamp = self.db.metadata("ibkr_analytics_sync_completed")
        self._sync_process = None
        self._analytics_process = None
        self._cash_process = None
        self._history_process = None
        self._encryption_process = None
        self._encryption_status_path = None
        self._encryption_progress = None
        self._history_status_path = None
        self._history_resume_configuration = None
        self.history_import_dialog = None
        self._last_analytics_sync_attempt = 0.0
        self._last_cash_sync_day = None
        self._sync_all_queue = []
        self._sync_all_active = False
        self._sync_paused = False
        self._global_paused_history_path = None
        self._sync_progress_active = False
        self._live_sync_pending = False
        self._live_sync_events = set()
        self._live_sync_started_at = 0.0
        self._last_gateway_sync_request = None
        self._last_quote_cache_at = 0.0
        self._quote_issue_statuses = {}
        self._last_ui_status_at = 0.0
        self._last_history_status = None
        self._last_history_status_path = None
        self._stream_rows = None
        self._quote_dirty_tabs = set()
        self._last_connection_attempt = 0.0
        self._disconnected_since = None
        self._connection_error = ""
        self.market = MockMarketDataProvider()
        self.service = PortfolioService(self.db, self.market, self.portfolio_id)
        self.models = {name: GridModel(name, self.db, self.service) for name in COLUMNS}
        self.views = {}
        self.grid_tabs = {}
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
        self.sync_button.clicked.connect(self.sync_all)
        heading_row.addWidget(self.sync_button)
        self.pause_sync_button = QPushButton()
        self.pause_sync_button.setObjectName("secondary")
        self.pause_sync_button.clicked.connect(self.toggle_sync_pause)
        heading_row.addWidget(self.pause_sync_button)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("themeToggle")
        self.theme_button.clicked.connect(self.toggle_theme)
        heading_row.addWidget(self.theme_button)
        self.help_button = QPushButton()
        self.help_button.setObjectName("secondary")
        self.help_button.setAccessibleName("Help")
        self.help_button.setToolTip("Help")
        self.help_button.clicked.connect(self.open_help)
        heading_row.addWidget(self.help_button)
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("secondary")
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setToolTip("Settings")
        self.settings_button.clicked.connect(self.open_settings)
        heading_row.addWidget(self.settings_button)
        for button in (self.sync_button, self.pause_sync_button, self.theme_button,
                       self.help_button, self.settings_button):
            button.setFixedSize(34, 34)
            button.setIconSize(QSize(20, 20))
        root_layout.addLayout(heading_row)
        summary = QWidget()
        summary.setObjectName("summary")
        self.summary = summary
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(14, 10, 14, 9)
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
        self.tabs.tabBar().setExpanding(False)
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
        self.trade_sync_status = QLabel()
        self.trade_sync_status.setObjectName("tradeSyncStatus")
        self.trade_sync_status.setTextFormat(Qt.RichText)
        self.trade_sync_status.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.trade_sync_status.linkActivated.connect(self.show_trade_sync_details)
        self.statusBar().addPermanentWidget(self.trade_sync_status)
        self.sync_progress_label.hide()
        self.sync_progress_bar.hide()
        for name in COLUMNS:
            self._make_grid_tab(name)
        self.cash_tab = CashMovementsTab(self.db, self.portfolio_id)
        self.tabs.addTab(self.cash_tab, "Cash")
        self.analytics_tab = AnalyticsTab(
            self.db, self.service, self.portfolio_id,
            request_price_history=self._request_levels_history)
        self.tabs.addTab(self.analytics_tab, "Analytics")
        self.calcs_tab = CalcsTab(self.db, self.service, self.portfolio_id)
        self.calcs_tab.remove_row_shortcut.setEnabled(False)
        self.tabs.addTab(self.calcs_tab, "Rebalance")
        widgets.persist_tab_order(self.tabs, self.db, self.TAB_LAYOUT_KEY,
                                  ids=list(self.DEFAULT_TAB_ORDER), remember_active=True)
        self.tabs.currentChanged.connect(self._refresh_special_tab)
        self.tabs.currentChanged.connect(self._expand_for_tab_selection)
        self.tabs.tabBarClicked.connect(self._expand_for_tab_selection)
        self._make_settings_tab()
        self._make_help_dialog()
        self._make_menu()
        self.tutorial = TutorialOverlay(self, self._tutorial_back,
                                        self._tutorial_next, self._tutorial_exit)
        self._tutorial_index = -1
        self._tutorial_original_tab = None
        self._update_match_lots_availability()
        self._table_click_away_filter = TableClickAwayFilter(self)
        QApplication.instance().installEventFilter(self._table_click_away_filter)
        enable_label_copy(self)
        self.next_tab_shortcut = QShortcut(shortcut("Ctrl+Tab", "Meta+Tab"), self)
        self.next_tab_shortcut.setContext(Qt.WindowShortcut)
        self.next_tab_shortcut.activated.connect(lambda: self._step_tab(1))
        self.previous_tab_shortcut = QShortcut(shortcut("Ctrl+Shift+Tab", "Meta+Shift+Tab"), self)
        self.previous_tab_shortcut.setContext(Qt.WindowShortcut)
        self.previous_tab_shortcut.activated.connect(lambda: self._step_tab(-1))
        if sys.platform == "darwin":
            help_key = QKeySequence(QKeySequence.HelpContents)
            if help_key != QKeySequence("F1"):
                self.help_shortcut = QShortcut(help_key, self)
                self.help_shortcut.setContext(Qt.ApplicationShortcut)
                self.help_shortcut.activated.connect(self.open_help)
        self._restore_columns()
        self._restore_default_views()
        self._update_summary_visibility()
        self.theme = self.db.setting("ui_theme") or "light"
        self._apply_zoom()
        self._sync_progress_active = True
        self._start_provider()
        if isinstance(self.market, IBKRMarketDataProvider):
            self._watch_live_sync()
        self.lots_refresh_timer = QTimer(self)
        self.lots_refresh_timer.setSingleShot(True)
        self.lots_refresh_timer.timeout.connect(self._refresh_lots_and_summary)
        self.calcs_refresh_timer = QTimer(self)
        self.calcs_refresh_timer.setSingleShot(True)
        self.calcs_refresh_timer.timeout.connect(self._refresh_calcs_live)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(250)
        self._encryption_poll_timer = QTimer(self)
        self._encryption_poll_timer.timeout.connect(self._poll_encryption)
        self._move_poll_timer = QTimer(self)
        self._move_poll_timer.timeout.connect(self._poll_database_move)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._periodic_refresh)
        self._set_refresh_interval()
        self._update_status()
        self._queue_configured_syncs()
        self._update_sync_button()

    def _refresh_special_tab(self, index):
        widget = self.tabs.widget(index)
        self._update_summary_visibility()
        name = next((name for name, page in self.grid_tabs.items() if widget is page), None)
        if name in self._quote_dirty_tabs:
            if name == "Portfolio":
                self._refresh_model(name, self._stream_rows)
                self._quote_dirty_tabs.discard(name)
            else:
                self.lots_refresh_timer.stop()
                self._refresh_lots_and_summary()
        if widget is self.calcs_tab or widget is self.cash_tab or widget is self.analytics_tab:
            widget.refresh()

    def _step_tab(self, direction):
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + direction) % count)

    def _update_summary_visibility(self):
        self.summary.setVisible((self._show_account_summary or self._compact_geometry is not None) and
                                self.tabs.currentWidget() is not self.calcs_tab)

    def _expand_for_tab_selection(self, _index):
        if self._compact_geometry is not None:
            self.compact_summary_action.setChecked(False)

    def _fit_compact_summary(self):
        self.tabs.setFixedHeight(self.tabs.tabBar().sizeHint().height() + 2)
        self.centralWidget().layout().activate()
        self.resize(self.width(), self.sizeHint().height())

    def _set_compact_summary(self, compact):
        self.expand_full_view_action.setEnabled(compact)
        if compact:
            if self._compact_geometry is not None:
                return
            self.tabs.setCurrentIndex(self._tab_index("Portfolio"))
            self._compact_geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
            self._compact_was_maximized = self.isMaximized()
            self._compact_status_visible = self.statusBar().isVisible()
            self._compact_tab_heights = (self.tabs.minimumHeight(), self.tabs.maximumHeight())
            if self._compact_was_maximized:
                self.showNormal()
            self._update_summary_visibility()
            self.statusBar().hide()
            self.tabs.currentWidget().hide()
            self._fit_compact_summary()
            self.resize(min(1040, self._compact_geometry.width()), self.height())
            self._fit_compact_summary()
        elif self._compact_geometry is not None:
            geometry = self._compact_geometry
            was_maximized = self._compact_was_maximized
            self._compact_geometry = None
            self.tabs.setMinimumHeight(self._compact_tab_heights[0])
            self.tabs.setMaximumHeight(self._compact_tab_heights[1])
            self.tabs.currentWidget().show()
            self.statusBar().setVisible(self._compact_status_visible)
            self._update_summary_visibility()
            self.setGeometry(geometry)
            if was_maximized:
                self.showMaximized()

    def _set_account_summary_visible(self, visible):
        self._show_account_summary = bool(visible)
        self.db.set_setting("show_account_summary", "1" if visible else "0")
        self._update_summary_visibility()

    def _refresh_calcs_live(self):
        if self.tabs.currentWidget() is self.calcs_tab and not self._sync_paused:
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
                self.calcs_tab.refresh(source_rows=self._stream_rows)

    def _apply_theme(self, theme):
        self.theme = theme if theme in ("light", "dark") else "light"
        self.setStyleSheet(stylesheet(self.theme, self.zoom_percent))
        for visual in self.help_screenshots:
            visual.set_theme(self.theme)
        self.analytics_tab.set_theme(self.theme)
        color = COLORS[self.theme]["text"]
        self.sync_button.setIcon(action_icon("sync", color))
        self._update_pause_sync_button()
        self.theme_button.setIcon(action_icon("sun" if self.theme == "dark" else "moon", color))
        self.help_button.setIcon(action_icon("help", color))
        self.settings_button.setIcon(action_icon("settings", color))
        theme_action = "Switch to light mode" if self.theme == "dark" else "Switch to dark mode"
        self.theme_button.setAccessibleName(theme_action)
        self.theme_button.setToolTip(theme_action)
        for model in self.models.values():
            model.theme = self.theme
            if model.rowCount() and model.columnCount():
                model.dataChanged.emit(model.index(0, 0),
                                       model.index(model.rowCount() - 1, model.columnCount() - 1))

    def toggle_theme(self):
        theme = "light" if self.theme == "dark" else "dark"
        self.db.set_setting("ui_theme", theme)
        self._apply_theme(theme)

    def _apply_zoom(self):
        font = self._base_font
        scaled_font = QFont(font)
        if font.pointSizeF() > 0:
            scaled_font.setPointSizeF(font.pointSizeF() * self.zoom_percent / 100)
        else:
            scaled_font.setPixelSize(round(font.pixelSize() * self.zoom_percent / 100))
        self.setFont(scaled_font)
        for table in self.findChildren(QTableView):
            header = table.verticalHeader()
            base = header.property("zoom_base_row_height")
            if base is None:
                base = header.defaultSectionSize()
                header.setProperty("zoom_base_row_height", base)
            header.setDefaultSectionSize(round(base * self.zoom_percent / 100))
        self.zoom_level_action.setText(f"Zoom: {self.zoom_percent}%")
        self.zoom_in_action.setEnabled(self.zoom_percent < self.MAX_ZOOM)
        self.zoom_reset_action.setEnabled(self.zoom_percent != 100)
        self._apply_theme(self.theme)
        if self._compact_geometry is not None:
            self._fit_compact_summary()

    def set_zoom(self, percent):
        percent = max(self.MIN_ZOOM, min(self.MAX_ZOOM, percent))
        if percent == self.zoom_percent:
            return
        self.zoom_percent = percent
        self.db.set_setting("ui_zoom_percent", str(percent))
        self._apply_zoom()

    def zoom_in(self):
        self.set_zoom(self.zoom_percent + self.ZOOM_STEP)

    def zoom_out(self):
        self.set_zoom(self.zoom_percent - self.ZOOM_STEP)

    def _zoom_out_or_remove_rows(self):
        table = self.calcs_tab.table
        focus = QApplication.focusWidget()
        if (self.tabs.currentWidget() is self.calcs_tab and focus is not None and
                (focus is table or table.isAncestorOf(focus)) and
                self.calcs_tab._zero_or_remove_selected_rows()):
            return
        self.zoom_out()

    def _update_pause_sync_button(self):
        action = "Resume sync" if self._sync_paused else "Pause sync"
        self.pause_sync_button.setIcon(action_icon(
            "play" if self._sync_paused else "pause", COLORS[self.theme]["text"]))
        self.pause_sync_button.setAccessibleName(action)
        self.pause_sync_button.setToolTip(action + " across the app")

    def toggle_sync_pause(self):
        self._sync_paused = not self._sync_paused
        if self._sync_paused:
            self._live_sync_pending = False
            self._live_sync_events.clear()
            if isinstance(self.market, IBKRMarketDataProvider):
                self.market.stop()
                self.market.connected = False
            if (self._history_process is not None and self._history_status_path is not None
                    and read_status(self._history_status_path).get("source") != "legacy"):
                request = pause_request_path(self._history_status_path)
                if not request.exists():
                    self.pause_history_import()
                    if request.exists():
                        self._global_paused_history_path = self._history_status_path
        else:
            path = self._global_paused_history_path
            if path is not None and pause_request_path(path).exists():
                try:
                    pause_request_path(path).unlink(missing_ok=True)
                except OSError:
                    self._sync_paused = True
                    QMessageBox.warning(self, "Sync", "Could not resume the history import in the data folder.")
                    return
                if self._history_process is not None and self._history_process.poll() is None:
                    self.history_import_dialog.set_running(True)
                    self.history_import_dialog.show_status(read_status(path))
            if self._history_process is not None and self._history_process.poll() is not None:
                self._poll_history_import()
            if (path is not None and self._history_process is None
                    and read_status(path)["state"] == "paused"):
                self._global_paused_history_path = None
                self.start_history_import()
            if self._history_process is None:
                self._global_paused_history_path = None
            self._start_provider()
            self._advance_sync_queue()
        self._update_pause_sync_button()
        self._update_sync_button()
        self._update_status()

    def sync_all(self):
        """Refresh live IBKR state and start every configured Flex catch-up."""
        if self._sync_paused or self._history_process is not None:
            return
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
                    self._sync_all_queue.append(launch)
                else:
                    missing.append(label)
        self._sync_all_active = bool(self._sync_all_queue)
        self._advance_sync_queue(retry=True)
        self.cash_tab.refresh()
        self.analytics_tab.refresh()
        self._update_sync_button()
        if missing:
            self.statusBar().showMessage("Sync: configure " + ", ".join(missing) + " in Settings", 10000)

    def _queue_configured_syncs(self):
        if self._sync_paused or self._history_process is not None:
            return
        if not self.db.setting("ib_account_id"):
            return
        self._sync_all_queue = [launch for key, launch in (
            ("flex_query_id", self._launch_flex_sync),
            ("analytics_flex_query_id", self._launch_analytics_sync),
            ("cash_flex_query_id", self._launch_cash_sync)) if self.db.setting(key)]
        self._sync_all_active = bool(self._sync_all_queue)
        self._advance_sync_queue()

    def _advance_sync_queue(self, retry=False):
        if self._history_process is not None:
            return
        self._record_trade_sync_exit()
        if self._sync_paused:
            return
        if any(process is not None and process.poll() is None for process in
               (self._sync_process, self._analytics_process, self._cash_process)):
            return
        if not self._sync_all_queue:
            self._sync_all_active = False
            return
        while self._sync_all_queue:
            launch = self._sync_all_queue.pop(0)
            launch(manual=True, retry=retry)
            if any(process is not None and process.poll() is None for process in
                   (self._sync_process, self._analytics_process, self._cash_process)):
                break
        if not self._sync_all_queue and not any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process)):
            self._sync_all_active = False

    def _update_sync_button(self):
        running = self._live_sync_pending or self._sync_all_active or any(process is not None and process.poll() is None for process in
                      (self._sync_process, self._analytics_process, self._cash_process))
        sync_action = ("Sync paused" if self._sync_paused else
                       "Importing Flex history…" if self._history_process is not None else
                       "Syncing IBKR…" if running else "Sync IBKR")
        self.sync_button.setAccessibleName(sync_action)
        self.sync_button.setEnabled(not self._sync_paused and not running and self._history_process is None)
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
                statuses.append(f"{label}: {status}" + (f" · {completed}" if completed else "") +
                                (f" · {error[:120]}" if error else ""))
        self.sync_button.setToolTip(
            ("Sync is paused. Use Resume sync to continue.\n" if self._sync_paused else
             sync_action + " — refresh live IBKR data and fetch completed Flex history.\n") +
            "\n".join(statuses))
        self._update_sync_progress()

    def _update_sync_progress(self):
        if self._sync_paused:
            running = (self._history_process is not None or any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process)))
            self.sync_progress_label.setText("Pausing sync…" if running else "Sync paused")
            self.sync_progress_label.show()
            self.sync_progress_bar.setVisible(running)
            return
        if self._history_process is not None:
            self.sync_progress_label.setText("Importing Flex history…")
            self.sync_progress_label.show()
            self.sync_progress_bar.show()
            return
        if self._live_sync_pending and time.monotonic() - self._live_sync_started_at > 90:
            self._live_sync_pending = False
        running = [label for label, process in (
            ("Trades", self._sync_process), ("Analytics", self._analytics_process),
            ("Cash", self._cash_process)) if process is not None and process.poll() is None]
        visible = self._sync_progress_active and (self._live_sync_pending or self._sync_all_active or bool(running))
        if visible:
            stage = ("live IBKR data" if self._live_sync_pending else
                     f"{running[0]} history" if running else "IBKR history")
            self.sync_progress_label.setText(f"Syncing {stage}…")
        else:
            self._sync_progress_active = False
        self.sync_progress_label.setVisible(visible)
        self.sync_progress_bar.setVisible(visible)

    def _watch_live_sync(self):
        self._live_sync_pending = True
        self._live_sync_events.clear()
        self._live_sync_started_at = time.monotonic()

    def _update_account_heading(self):
        account = self.db.setting("ib_account_id")
        self.heading.setText(account or "Account not set")

    def _sync_launch(self, module, flag):
        if getattr(sys, "frozen", False):
            return [sys.executable, flag, str(self.db.path)], self.db.path.parent
        return [sys.executable, "-m", module, str(self.db.path)], Path(__file__).resolve().parent.parent

    def open_history_import(self):
        if self.history_import_dialog is None:
            self.history_import_dialog = HistoryImportDialog(self)
            enable_label_copy(self.history_import_dialog)
        self.history_import_dialog.refresh_flex_limit()
        self._refresh_history_import_coverage()
        self.history_import_dialog.ensure_content_fits()
        self.history_import_dialog.show()
        self.history_import_dialog.raise_()
        self.history_import_dialog.activateWindow()

    def _refresh_history_import_coverage(self):
        if self.history_import_dialog is not None:
            account = self.db.setting("ib_account_id")
            self.history_import_dialog.show_imported_periods(
                self.db.completed_flex_windows(account),
                self.db.monthly_history(self.portfolio_id),
                has_older_history=self.db.has_legacy_history(self.portfolio_id))

    def start_legacy_history_import(self):
        dialog = self.history_import_dialog
        if dialog is None or self._history_process is not None:
            return
        if self._sync_paused:
            QMessageBox.warning(self, "Import Data", "Resume sync before importing history.")
            return
        if (self._live_sync_pending or self._sync_all_active or self._sync_all_queue or any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process))):
            QMessageBox.warning(self, "Import Data", "Wait for the current IBKR sync to finish before importing history.")
            return
        if not self.db.setting("ib_account_id") or not self.db.path.is_file():
            QMessageBox.warning(self, "Import Data", "Apply the IBKR account and save the portfolio first.")
            return
        reports, _ = QFileDialog.getOpenFileNames(
            self, "Select both PortfolioAnalyst and Activity Statement CSVs", "", "CSV files (*.csv)")
        if not reports:
            return
        if len(reports) != 2:
            QMessageBox.warning(self, "Import Data", "Select both CSV reports together (Ctrl-click).")
            return
        self._launch_legacy_history_worker(["--reports", *reports], action="import")

    def remove_legacy_history_import(self):
        dialog = self.history_import_dialog
        if dialog is None or self._history_process is not None:
            return
        if self._sync_paused:
            QMessageBox.warning(self, "Import Data", "Resume sync before removing history.")
            return
        if (self._live_sync_pending or self._sync_all_active or self._sync_all_queue or any(
                process is not None and process.poll() is None for process in
                (self._sync_process, self._analytics_process, self._cash_process))):
            QMessageBox.warning(self, "Import Data", "Wait for the current IBKR sync to finish before removing history.")
            return
        if not self.db.has_legacy_history(self.portfolio_id):
            self._refresh_history_import_coverage()
            return
        answer = QMessageBox.question(
            self, "Remove older CSV history",
            "Remove all saved older CSV history for this account?\n\n"
            "This clears monthly history, reported since-inception performance, "
            "Activity Statement evidence, and closing stock quantities. "
            "Flex history and the original CSV files stay in place. "
            "A database backup will be created first.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._launch_legacy_history_worker(["--remove"], action="remove")

    def _launch_legacy_history_worker(self, operation_args, *, action):
        dialog = self.history_import_dialog
        self._history_resume_configuration = None
        self._history_status_path = self.db.path.parent / f"history-import-{uuid4().hex}.json"
        status = read_status(self._history_status_path)
        status.update(source="legacy", action=action, state="starting", message="")
        try:
            write_status(self._history_status_path, status)
            args = ["--status-file", str(self._history_status_path),
                    "--database", str(self.db.path),
                    *operation_args]
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--legacy-history-worker", *args]
                workdir = self.db.path.parent
            else:
                command = [sys.executable, "-m", "scripts.legacy_history_worker", *args]
                workdir = Path(__file__).resolve().parent.parent
            self._history_process = subprocess.Popen(
                command, cwd=workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            status.update(state="failed", finished_at=datetime.now().isoformat(timespec="seconds"),
                          message="Could not launch the older history worker or write its progress file.")
            try:
                write_status(self._history_status_path, status)
            except OSError:
                pass
            dialog.show_status(status)
            return
        dialog.set_running(True, pausable=False)
        dialog.show_status(status)
        self._update_sync_button()

    def start_history_import(self):
        dialog = self.history_import_dialog
        if dialog is None or self._history_process is not None:
            return
        try:
            if self._sync_paused:
                raise ValueError("Resume sync before importing history.")
            if self._live_sync_pending or self._sync_all_active or self._sync_all_queue or any(
                    process is not None and process.poll() is None for process in
                    (self._sync_process, self._analytics_process, self._cash_process)):
                raise ValueError("Wait for the current IBKR sync to finish before importing history.")
            first_date = dialog.from_date.date()
            last_date = dialog.to_date.date()
            if not first_date.isValid() or not last_date.isValid():
                raise ValueError("Enter valid dates as YYYY-MM-DD.")
            first = first_date.toPython()
            last = last_date.toPython()
            if first > last or last >= flex_today():
                raise ValueError("Choose an ordered date range ending before today.")
            account = self.db.setting("ib_account_id")
            if not account:
                raise ValueError("Apply the IBKR account in Settings first.")
            source = dialog.source.currentData()
            sources = ("trades", "analytics", "cash") if source == "all" else (source,)
            query_keys = {"trades": "flex_query_id", "analytics": "analytics_flex_query_id",
                          "cash": "cash_flex_query_id"}
            for item in sources:
                if not self.db.setting(query_keys[item]):
                    raise ValueError(f"Apply the {item.capitalize()} Flex query ID in Settings first.")
            try:
                configured = bool(secret_store.load_flex_token(account))
            except Exception:
                raise ValueError("Could not access the OS credential store. Save the Flex token in Settings first.") from None
            if not configured:
                raise ValueError("Save the Flex token for this account in Settings first.")
            if not self.db.path.is_file():
                raise ValueError("History import requires a saved local database.")
        except ValueError as exc:
            QMessageBox.warning(self, "Import Data", str(exc))
            return

        configuration = (account, source, first.isoformat(), last.isoformat(),
                         tuple(self.db.setting(query_keys[item]) for item in sources))
        previous = read_status(self._history_status_path) if self._history_status_path else {}
        resuming = (previous.get("state") == "paused" and
                    self._history_resume_configuration == configuration)
        if not resuming:
            # A unique file prevents stale results from a previous worker being mistaken for this run.
            self._history_status_path = self.db.path.parent / f"history-import-{uuid4().hex}.json"
        status = read_status(self._history_status_path)
        status.update(source=source, from_date=first.isoformat(), to_date=last.isoformat())
        try:
            pause_request_path(self._history_status_path).unlink(missing_ok=True)
            write_status(self._history_status_path, status)
            args = ["--status-file", str(self._history_status_path), "--source", source,
                    "--from-date", first.isoformat(), "--to-date", last.isoformat(),
                    "--database", str(self.db.path)]
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--history-worker", *args]
                workdir = self.db.path.parent
            else:
                command = [sys.executable, "-m", "scripts.history_worker", *args]
                workdir = Path(__file__).resolve().parent.parent
            self._history_process = subprocess.Popen(
                command, cwd=workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._history_resume_configuration = configuration
        except OSError:
            status.update(state="failed", finished_at=datetime.now().isoformat(timespec="seconds"),
                          message="Could not launch the history worker or write its progress file. Check access to the data folder.")
            try:
                write_status(self._history_status_path, status)
            except OSError:
                pass
            dialog.show_status(status)
            return
        dialog.set_running(True)
        self._refresh_history_import_coverage()
        dialog.show_status(status)
        self._update_sync_button()

    def pause_history_import(self):
        if self._history_process is None or self._history_status_path is None:
            return
        try:
            pause_request_path(self._history_status_path).touch()
        except OSError:
            QMessageBox.warning(self, "Import Data", "Could not request a pause in the data folder.")
            return
        self.history_import_dialog.set_pause_requested()

    def _poll_history_import(self):
        process = self._history_process
        if process is None:
            return
        code = process.poll()
        status = read_status(self._history_status_path)
        if code is not None:
            try:
                pause_request_path(self._history_status_path).unlink(missing_ok=True)
            except OSError:
                pass
            if (status["state"] not in ("completed", "completed_with_gaps", "paused", "failed", "needs_review") or
                    code and status["state"] in ("completed", "completed_with_gaps")):
                status.update(state="failed", pid=None,
                              finished_at=datetime.now().isoformat(timespec="seconds"),
                              message=status.get("message") or
                              f"History worker exited with code {code} without a completion result. Check local diagnostic logs.")
                try:
                    write_status(self._history_status_path, status)
                except OSError:
                    pass
            self._history_process = None
            self.history_import_dialog.set_running(False)
            # A failed import can still have committed earlier windows.
            self._refresh_models()
            self.cash_tab.refresh()
            self.analytics_tab.refresh()
            self._update_sync_button()
            if self._global_paused_history_path == self._history_status_path and not self._sync_paused:
                self._global_paused_history_path = None
                if status["state"] == "paused":
                    self.start_history_import()
                    return
            notice = ("Older CSV history removal finished; open File → Import Data to review results."
                      if status.get("action") == "remove" else
                      "History import finished; open File → Import Data to review results.")
            self.statusBar().showMessage(notice, 15000)
        if (code is not None or status != self._last_history_status or
                self._history_status_path != self._last_history_status_path):
            self._last_history_status = status.copy()
            self._last_history_status_path = self._history_status_path
            self._refresh_history_import_coverage()
            self.history_import_dialog.show_status(status)

    def _launch_flex_sync(self, *, manual=False, retry=False):
        if self._sync_paused or self._history_process is not None:
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("flex_query_id"):
            if manual:
                QMessageBox.warning(self, "IBKR trade sync", "Set the IBKR account and Flex query ID in Settings first.")
            return
        # An IBKR restriction pauses automatic attempts; only an explicit
        # user retry (Sync IBKR) resumes them.
        if trade_sync_paused(self.db) and not retry:
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
            self.db.set_metadata("ibkr_flex_sync_stage", "")
            self.db.set_metadata("ibkr_flex_sync_run_id", "")
            self.db.set_metadata("ibkr_flex_sync_started", now.isoformat(timespec="seconds"))
            command, workdir = self._sync_launch("scripts.sync_trades", "--sync-trades")
            self.log.info("Launching trade sync; manual=%s", manual)
            self._sync_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not manual:
                self._last_gateway_sync_request = now
        except OSError as exc:
            self.db.set_metadata("ibkr_flex_sync_status", "failed")
            self.db.set_metadata("ibkr_flex_sync_error", str(exc))
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
        status = self.db.metadata("ibkr_flex_sync_status") or "never"
        if (status == "failed" and self.db.metadata("ibkr_flex_sync_stage") == "requesting Flex report"
                and self.db.metadata("ibkr_flex_sync_error").startswith("Flex error 1003:")):
            return "statement pending"
        return status

    def show_trade_sync_details(self, _link=""):
        if self.db.metadata("ibkr_flex_sync_status") != "failed":
            return
        pending = self._trade_flex_display_status() == "statement pending"
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
        details = (f"{'IBKR response' if pending else 'Error'}: {error or 'No error was recorded.'}\n"
                   f"{'Checked at' if pending else 'Failed at'}: {failed_at}\nStage: {stage}\nDate window: {window}\n"
                   f"Run ID: {row['id'] if row else 'Not recorded'}\n"
                   f"Last successful sync: {self.db.metadata('ibkr_flex_sync_completed') or 'Never'}\n\n"
                   f"Diagnostic log: {diagnostic_log_path(self.db.path, 'sync-trades')}")
        if trade_sync_paused(self.db):
            QMessageBox.warning(self, "IBKR trade sync paused",
                                "Automatic Trades sync is paused after an IBKR restriction. "
                                "Press Sync IBKR to retry.\n\n" + details)
        elif pending:
            QMessageBox.information(self, "IBKR trade statement pending", details)
        else:
            QMessageBox.warning(self, "IBKR trade sync failed", details)

    def _launch_analytics_sync(self, *, manual=False, retry=False):
        if self._sync_paused or self._history_process is not None:
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("analytics_flex_query_id"):
            if manual:
                QMessageBox.warning(self, "Analytics history", "Set the IBKR account and Analytics Flex query ID in Settings first.")
            return
        if self._analytics_process is not None and self._analytics_process.poll() is None:
            return
        if (not manual and self._last_analytics_sync_attempt and
                time.monotonic() - self._last_analytics_sync_attempt < 300):
            return
        self._last_analytics_sync_attempt = time.monotonic()
        try:
            self.db.set_metadata("ibkr_analytics_sync_status", "starting")
            self.db.set_metadata("ibkr_analytics_sync_error", "")
            command, workdir = self._sync_launch("scripts.sync_analytics", "--sync-analytics")
            self.log.info("Launching analytics history sync; manual=%s", manual)
            self._analytics_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.analytics_tab.refresh()
        except OSError as exc:
            self.db.set_metadata("ibkr_analytics_sync_status", "failed")
            self.db.set_metadata("ibkr_analytics_sync_error", str(exc))
            self.log.error("Analytics history sync launch failed: %s: %s", type(exc).__name__, exc)

    def _launch_cash_sync(self, *, manual=False, retry=False):
        if self._sync_paused or self._history_process is not None:
            return
        if not self.db.setting("ib_account_id") or not self.db.setting("cash_flex_query_id"):
            if manual:
                QMessageBox.warning(self, "Cash history", "Set the IBKR account and Cash Flex query ID in Settings first.")
            return
        if self._cash_process is not None and self._cash_process.poll() is None:
            return
        if not manual and self._last_cash_sync_day == flex_today():
            return
        if not manual:
            self._last_cash_sync_day = flex_today()
        try:
            self.db.set_metadata("ibkr_cash_sync_status", "starting")
            self.db.set_metadata("ibkr_cash_sync_error", "")
            command, workdir = self._sync_launch("scripts.sync_cash", "--sync-cash")
            self.log.info("Launching cash history sync; manual=%s", manual)
            self._cash_process = subprocess.Popen(
                command,
                cwd=workdir,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.db.set_metadata("ibkr_cash_sync_status", "failed")
            self.db.set_metadata("ibkr_cash_sync_error", str(exc))
            self.log.error("Cash history sync launch failed: %s: %s", type(exc).__name__, exc)

    def _make_grid_tab(self, name):
        page = QWidget()
        self.grid_tabs[name] = page
        layout = QVBoxLayout(page)
        widgets.apply_layout(layout, "page")
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
        if name == "Lots":
            self.match_lots_button = widgets.button("Match lots…", self.match_lots, variant="secondary")
            toolbar.addWidget(self.match_lots_button)
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
        if name == "Lots":
            view.selectionModel().selectionChanged.connect(self._update_match_lots_availability)
            self.models[name].modelReset.connect(self._update_match_lots_availability)
        if name == "Portfolio":
            view.doubleClicked.connect(lambda _: self.show_position_details())
        elif name == "Lots":
            view.doubleClicked.connect(lambda _: self.match_lots())
        self.tabs.addTab(page, "Overview" if name == "Portfolio" else name)

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
        widgets.apply_layout(layout, "page")
        form = widgets.form_layout()
        advanced_form = widgets.form_layout()
        self.settings_fields = {}
        definitions = [
            ("provider", "Market provider", ["Mock", "IBKR"]),
            ("ib_host", "IBKR host", None), ("ib_port", "IBKR port", None),
            ("ib_client_id", "IBKR client ID", None),
            ("ib_account_id", "IBKR account", None),
            ("flex_query_id", "Trades Flex query ID", None),
            ("analytics_flex_query_id", "Analytics Flex query ID", None),
            ("cash_flex_query_id", "Cash Flex query ID", None),
            ("data_mode", "Preferred data mode", ["Live", "Frozen", "Delayed"]),
            ("refresh_seconds", "Refresh interval (seconds)", None),
            ("backup_directory", "Backup directory", None),
        ]
        for key, label, options in definitions:
            if options:
                widget = QComboBox()
                widget.addItems(options)
                stored = self.db.setting(key)
                widget.setCurrentText("On" if key == "autosave" and stored == "1" else
                                      "Off" if key == "autosave" else stored)
            else:
                stored = self.db.setting(key)
                widget = QLineEdit("" if stored is None else str(stored))
            self.settings_fields[key] = widget
            target = advanced_form if key in (
                "provider", "ib_client_id", "data_mode", "refresh_seconds",
                "backup_directory") else form
            target.addRow(label, widget)
        self.database_location = QLabel(str(self.db.path))
        self.database_location.setTextFormat(Qt.PlainText)
        self.database_location.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.database_location.setWordWrap(True)
        self.database_location.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        advanced_form.addRow(QLabel("Current database"))
        advanced_form.addRow(self.database_location)
        advanced_form.addRow(widgets.button("Move database…", self.move_database, variant="secondary"))
        layout.addLayout(form)
        token_row = QHBoxLayout()
        self.flex_token_input = QLineEdit()
        self.flex_token_input.setEchoMode(QLineEdit.Password)
        self.flex_token_input.setPlaceholderText("Paste a new Flex token to save or replace it")
        token_row.addWidget(self.flex_token_input)
        token_row.addWidget(widgets.button("Save Flex token", self.save_flex_token, variant="secondary"))
        layout.addLayout(token_row)
        self.flex_token_status = QLabel()
        self.flex_token_status.setTextFormat(Qt.PlainText)
        self.flex_token_status.setWordWrap(True)
        layout.addWidget(self.flex_token_status)
        self.settings_fields["ib_account_id"].textChanged.connect(self._refresh_flex_token_status)
        self._refresh_flex_token_status()
        self.use_ibkr_button = QPushButton("Use IBKR")
        self.use_ibkr_button.clicked.connect(self._use_ibkr)
        self.use_ibkr_button.setVisible(self.db.setting("provider") != "IBKR")
        layout.addWidget(self.use_ibkr_button)
        self.advanced_button = QPushButton("Advanced settings")
        self.advanced_button.setObjectName("secondary")
        layout.addWidget(self.advanced_button)
        self.advanced_settings = QWidget()
        self.advanced_settings.setLayout(advanced_form)
        self.advanced_settings.hide()
        self.advanced_button.clicked.connect(
            lambda: self.advanced_settings.setVisible(not self.advanced_settings.isVisible()))
        layout.addWidget(self.advanced_settings)
        row = QHBoxLayout()
        self._button(row, "Apply Settings", self.apply_settings)
        row.addStretch()
        layout.addLayout(row)
        self.settings_info = QLabel()
        self.settings_info.setObjectName("settingsInfo")
        self.settings_info.setWordWrap(True)
        self.settings_info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.settings_info)
        layout.addStretch()
        scroller = QScrollArea()
        scroller.setObjectName("settingsScroll")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.NoFrame)
        scroller.setWidget(page)
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("Settings")
        self.settings_dialog.resize(620, 650)
        self.settings_dialog.finished.connect(lambda _result: self.flex_token_input.clear())
        dialog_layout = QVBoxLayout(self.settings_dialog)
        dialog_layout.addWidget(scroller)
        close_button = widgets.dialog_buttons(self.settings_dialog, QDialogButtonBox.Close)
        close_button.rejected.connect(self.settings_dialog.reject)
        dialog_layout.addWidget(close_button)

    def open_settings(self):
        self._refresh_flex_token_status()
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def _refresh_flex_token_status(self):
        account = self.settings_fields["ib_account_id"].text().strip()
        if not account or account != self.db.setting("ib_account_id"):
            message = "Apply the IBKR account in Settings before saving its Flex token."
        else:
            try:
                configured = bool(secret_store.load_flex_token(account))
            except Exception:
                message = "Flex token status unavailable; check OS credential store access."
            else:
                message = f"Flex token for {account}: {'configured' if configured else 'not configured'}."
        self.flex_token_status.setText(message)

    def save_flex_token(self):
        token = self.flex_token_input.text().strip()
        self.flex_token_input.clear()
        account = self.settings_fields["ib_account_id"].text().strip()
        try:
            if self._history_process is not None:
                raise ValueError("Wait for the history import to finish before replacing its token.")
            if not account or account != self.db.setting("ib_account_id"):
                raise ValueError("Apply the IBKR account in Settings before saving its Flex token.")
            if not token.isascii() or not token.isdecimal() or len(token) < 16:
                raise ValueError("Enter a valid numeric Flex token of at least 16 digits.")
        except ValueError as exc:
            QMessageBox.warning(self, "Flex token", str(exc))
        else:
            try:
                secret_store.save_flex_token(account, token)
            except Exception:
                # Never display credential backend exceptions, which may contain a token.
                QMessageBox.warning(self, "Flex token", "Could not save the Flex token in the OS credential store. Check local credential store access.")
            else:
                self._refresh_flex_token_status()
        finally:
            token = None

    def _use_ibkr(self):
        self.settings_fields["provider"].setCurrentText("IBKR")
        self.apply_settings()

    def _make_help_dialog(self):
        self.help_dialog = QDialog(self)
        self.help_dialog.setWindowTitle("Help · IBKR Dashboard")
        self.help_dialog.resize(760, 600)
        dialog_layout = QVBoxLayout(self.help_dialog)
        guide_layout = QHBoxLayout()
        self.help_navigation = QListWidget()
        self.help_navigation.setObjectName("helpNavigation")
        self.help_navigation.setFixedWidth(165)
        self.help_navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.help_navigation.addItem("Take a tour…")
        self.help_sections = QStackedWidget()
        self.help_navigation.currentRowChanged.connect(self._on_help_navigation_changed)
        self.help_section_indices = {}
        self.help_step_targets = {}
        self.help_screenshots = []
        guide_layout.addWidget(self.help_navigation)
        page_layout = QVBoxLayout()
        jump_layout = QHBoxLayout()
        jump_layout.addWidget(QLabel("Jump to"))
        self.help_jump_menu = QComboBox()
        self.help_jump_menu.setObjectName("helpJumpMenu")
        self.help_jump_menu.setFixedWidth(210)
        self.help_jump_menu.activated.connect(self._jump_to_help_step)
        jump_layout.addWidget(self.help_jump_menu)
        jump_layout.addStretch()
        page_layout.addLayout(jump_layout)
        page_layout.addWidget(self.help_sections, 1)
        guide_layout.addLayout(page_layout, 1)
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
            content.setProperty("helpTitle", title)
            scroller.setWidget(page)
            self.help_sections.addWidget(scroller)
            self.help_section_indices[title] = self.help_navigation.count()
            self.help_step_targets[title] = []
            self.help_navigation.addItem(title)
            return content

        def add_note(content, body):
            label = QLabel(body)
            label.setObjectName("helpBody")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            content.addWidget(label)

        def add_picture(content, name, description):
            image = HelpScreenshot(name, description)
            self.help_screenshots.append(image)
            content.addWidget(image)

        def add_step(content, title, body=None, screenshot=None, jump_label=None):
            if content.property("hasHelpStep"):
                divider = QFrame()
                divider.setObjectName("helpDivider")
                divider.setFixedHeight(1)
                content.addWidget(divider)
            content.setProperty("hasHelpStep", True)
            heading = QLabel(title)
            heading.setObjectName("helpStep")
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            heading.setWordWrap(True)
            heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            content.addWidget(heading)
            menu_label = jump_label or title
            if menu_label[:1].isdigit():
                menu_label = menu_label.split("  ", 1)[-1]
            menu_label = menu_label.split("→", 1)[0].strip()
            self.help_step_targets[content.property("helpTitle")].append((menu_label, heading))
            if body:
                add_note(content, body)
            if screenshot:
                add_picture(content, screenshot, title)

        def add_disclosure(content, title, lines):
            toggle = QToolButton()
            toggle.setText(title)
            toggle.setCheckable(True)
            toggle.setArrowType(Qt.RightArrow)
            toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            toggle.setAutoRaise(True)
            details = QWidget()
            detail_layout = QVBoxLayout(details)
            detail_layout.setContentsMargins(14, 0, 0, 0)
            detail_layout.setSpacing(4)
            for line in lines:
                add_note(detail_layout, line)
            details.hide()

            def show_details(checked):
                toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
                details.setVisible(checked)

            toggle.toggled.connect(show_details)
            content.addWidget(toggle)
            content.addWidget(details)

        start_content = add_section("Start")
        add_step(start_content, "1  Find the Socket Port in TWS or IB Gateway", jump_label="Port")
        add_note(start_content, "TWS: Global Configuration → API → Settings · live 7496 / paper 7497")
        add_note(start_content, "Gateway: Configure → Settings → API → Settings · live 4001 / paper 4002")
        add_note(start_content, "Enable socket clients and Read-Only API. Use the Socket Port shown there; it may differ from the defaults.")
        add_step(start_content, "2  Settings → connect to IBKR",
                 "Enter the host, Socket Port and account. Click Use IBKR, or Apply Settings if IBKR is already selected.",
                 "start_connection", jump_label="Connect")
        add_picture(start_content, "start_apply", "Use IBKR and Apply Settings")
        add_step(start_content, "3  Sync IBKR",
                 "Click the circular arrows in the top toolbar. Hover for each source's status.",
                 "start_sync", jump_label="Sync")

        # Keep the Flex field lists and CSV requirements aligned with HISTORICAL_REPORTS.md.
        add_step(start_content, "4  Client Portal → Performance & Reports → Flex Queries",
                 "Create three Activity Flex Queries (XML): Trades, Analytics and Cash. Use one USD-base account, custom dates, all currencies and instruments, and no symbol filter.",
                 jump_label="Flex queries")
        add_disclosure(start_content, "Trades query · fields to select", (
            "Trades → Executions: Account ID, Trade ID, IB Execution ID, Trade Date, Trade Time (or Date/Time), Buy/Sell, Quantity, Trade Price, Conid, Symbol, Asset Class, Currency, Multiplier, FX Rate to Base, IB Commission, IB Commission Currency. Options also need Expiry, Put/Call and Strike.",
            "Corporate Actions: Account ID, Type, Description, Date/Time, Transaction ID, Conid, Symbol, Asset Class, Currency, Quantity, Proceeds, Multiplier, FX Rate to Base.",
            "Options, Exercises and Expirations: Account ID, Date, Transaction Type, Asset Class, Conid, Quantity, Trade Price, Proceeds.",
            "Transfers (ACATS, Internal): Account ID, Date, Direction, Asset Class, Conid, Symbol, Quantity, Currency, FX Rate to Base, Transaction ID. Account Information: Currency.",
        ))
        add_disclosure(start_content, "Analytics query · fields to select", (
            "NAV Summary in Base: Account ID, Report Date, Total, Cash. Cash Transactions: Account ID, Date/Time, Type, Amount, Currency, FX Rate to Base.",
            "Transfers (ACATS, Internal): Account ID, Report Date, Date, Direction, Asset Class, Position Amount in Base, Currency, FX Rate to Base, Transaction ID. Account Information: Currency.",
        ))
        add_disclosure(start_content, "Cash query · fields to select", (
            "Cash Transactions: Account ID, Currency, Date/Time, Type, Amount, Description, Conid, Symbol, FX Rate to Base. Add Report Date or Transaction ID if offered.",
        ))
        add_step(start_content, "5  Settings → apply query IDs, then save the Flex token",
                 "Use the matching Trades, Analytics and Cash query IDs. Apply the account before saving its token.",
                 "start_flex", jump_label="Flex token")
        add_step(start_content, "6  File → Import Data → import Flex history",
                 "Select All reports, set From and To through yesterday, then click Import history. Check progress and coverage gaps.",
                 "start_import", jump_label="Import history")
        add_step(start_content, "7  Import older CSV history (optional)",
                 f"For history before {oldest_flex_date().isoformat()}, use a PortfolioAnalyst since-inception monthly Detailed CSV and an Activity Statement CSV for the same USD-base account. The statement must run from inception through a month end before that date; PortfolioAnalyst must include that month.",
                 "setup_older_csv", jump_label="Older CSV")
        add_note(start_content, "PortfolioAnalyst: Reports → Default Reports → Since Inception → Detailed (CSV). Activity Statement: Statements → Activity Statement → CSV.")
        add_note(start_content, "Include Account Information, Net Asset Value, Change in NAV, and dated activity or positions in the Activity Statement.")
        add_note(start_content, "If that older statement is unavailable in Client Portal, ask IBKR whether an archived CSV is available.")
        add_note(start_content, "File → Import Data → Import older CSV history… Select both files together (Ctrl-click). This adds monthly history, not missing daily NAV or cash dates.")

        portfolio_content = add_section("Portfolio")
        add_step(portfolio_content, "1  Overview → inspect holdings",
                 "Use Columns, Hide closed or search. Scroll right for Reconciliation and Data status; double-click a holding for its trades and lots.",
                 "portfolio_overview")
        add_step(portfolio_content, "2  Trades → filter BUY or SELL",
                 "Search an instrument to narrow the table. Matched realized P&L appears on both trade rows.",
                 "portfolio_trades")
        add_step(portfolio_content, "3  Cash → choose a date range",
                 "Cash Flex supplies deposits, dividends, interest, tax and fees.",
                 "portfolio_cash_range")
        add_picture(portfolio_content, "portfolio_cash", "Imported Cash transactions")
        add_note(portfolio_content, "Lots filters Open and Closed; use Match lots… to change allocations.")

        matched_content = add_section("Matched lots")
        add_step(matched_content, "1  Lots → select a lot → Match lots…",
                 "Open = held · Closed = sold. Double-clicking a lot also opens matching.",
                 "matched_lots", jump_label="Open matching")
        add_step(matched_content, "2  Drag a sale onto an eligible purchase",
                 "Sales are above purchases. The purchase must be earlier and have free quantity.",
                 "matched_sale", jump_label="Drag to match")
        add_picture(matched_content, "matched_purchase", "Purchases before a manual match")
        add_step(matched_content, "3  Review chosen and automatic quantities → Save changes",
                 "The preview updates before saving. Cancel discards the edit.", "matched_after",
                 jump_label="Save changes")
        add_note(matched_content, "Split selected purchase to allocate smaller parts; Remove split joins them. Reset sale to automatic removes your choice.")
        add_step(matched_content, "Other matching actions", screenshot="matched_menu",
                 jump_label="Other actions")
        add_note(matched_content, "Rebuild automatic matches keeps manual choices. Auto-match remaining: FIFO (oldest), LIFO (newest), Max realized gain or Max unrealized gain (highest cost).")
        add_note(matched_content, "Clear match for selected lot frees its shares. Short-sale covers are automatic and cannot be edited here. Missing fees, FX or multiplier can prevent a match.")

        analytics_content = add_section("Analytics")
        add_step(analytics_content, "P&L → choose a range and chart metric",
                 "Toggle S&P 500 or drag the chart to focus dates. Adjusted change removes external flows; gaps need more NAV and flow history.",
                 "analytics_pnl")
        add_step(analytics_content, "Capital deployment → choose a view",
                 "Net deployed, buy/sell activity and cash/portfolio share the same range controls.",
                 "analytics_capital")
        add_step(analytics_content, "Returns → select an instrument",
                 "Inspect matched lots and income below. ≈ marks partial data; hover for the reason. Open bonds need accrued interest.",
                 "analytics_returns")
        add_step(analytics_content, "Exposure → group and inspect positions",
                 "Choose Sector or Custom and Adjusted or Market value. Select a slice or category; create or manage custom sets here.",
                 "analytics_exposure")
        add_step(analytics_content, "Monthly P&L → review each month",
                 "Partial months and missing baselines may leave returns blank. Import older PortfolioAnalyst CSVs through File → Import Data.",
                 "analytics_monthly")
        add_step(analytics_content, "Buy / sell levels → choose a stock",
                 "Zoom the chart or load older prices while connected to IBKR. Trade markers need verified split history.",
                 "analytics_levels")

        rebalance_content = add_section("Rebalance")
        add_step(rebalance_content, "1  Set a Cash target or add an idea",
                 "Type a ticker and click Add. Reset targets clears the worksheet's target inputs.",
                 "rebalance_setup", jump_label="Targets")
        add_step(rebalance_content, "2  Edit tinted Target weight cells",
                 "The Buy / Sell USD estimate updates. Enter saves an edit; Esc cancels it.",
                 "rebalance_table", jump_label="Target weights")
        add_step(rebalance_content, "3  Show → Upside / downside",
                 "Enter price or percentage assumptions to compare cases.",
                 "rebalance_show", jump_label="Upside / downside")
        add_picture(rebalance_content, "rebalance_cases", "Upside and downside case inputs")
        add_note(rebalance_content, "This worksheet does not place orders.")

        views_content = add_section("Views & data")
        add_step(views_content, "1  Right-click a column heading",
                 "Sort, set decimal places or hide the column. Columns → Show all columns restores hidden columns.",
                 "views_columns", jump_label="Columns")
        add_step(views_content, "2  View → Save current tab view",
                 "Saved views remember filters and layout. Drag tabs to reorder; View also resets tab order, changes zoom and toggles the account summary.",
                 "views_view_menu", jump_label="View menu")
        add_step(views_content, "3  File → export, back up or import",
                 "Export Workbook and Create SQLite Backup are here. Import Data opens the dated history importer.",
                 "views_file_menu", jump_label="File menu")
        add_note(views_content, "Live values may be provisional. Hover over missing values for details.")

        shortcuts_content = add_section("Shortcuts")
        next_tab_keys = (f"{native_text(shortcut('Ctrl+Tab', 'Meta+Tab'))} / "
                         f"{native_text(shortcut('Ctrl+Shift+Tab', 'Meta+Shift+Tab'))}")
        help_keys = native_text(QKeySequence(QKeySequence.HelpContents)
                                if sys.platform == "darwin" else QKeySequence("F1"))
        if sys.platform == "darwin":
            help_keys += " / F1"
        for group, rows in (
            ("Navigate", ((next_tab_keys, "Next / previous tab"),
                          (help_keys, "Open this guide"))),
            ("Display", ((native_text(shortcut("Ctrl+M", "Ctrl+Shift+M")), "Compact summary"),
                         (native_text(shortcut("Ctrl+L", "Ctrl+Shift+L")), "Full view"),
                         (native_text(shortcut("Ctrl+H", "Ctrl+Shift+H")), "Show / hide account summary"),
                         (" / ".join(native_text(shortcut(key)) for key in ("Ctrl++", "Ctrl+=")), "Zoom in"),
                         (native_text(shortcut("Ctrl+Shift+-")), "Zoom out"),
                         (native_text(shortcut("Ctrl+0")), "Reset zoom"))),
            ("Tables and inputs", (("Enter / F2", "Edit cell; Enter saves and moves down"),
                                   ("Esc", "Cancel edit"),
                                   ("Arrow keys", "Move and save current edit"),
                                   ("Shift+Space", "Select row"),
                                   (native_text(shortcut("Ctrl+Space")), "Select column"),
                                   (native_text(QKeySequence(QKeySequence.Copy)), "Copy selection"),
                                   (native_text(shortcut("Ctrl+-")),
                                    "Rebalance: remove idea or zero target; elsewhere zoom out"))),
        ):
            group_label = QLabel(group)
            group_label.setObjectName("helpSection")
            shortcuts_content.addWidget(group_label)
            self.help_step_targets["Shortcuts"].append((group, group_label))
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 2)
            grid.setHorizontalSpacing(20)
            grid.setVerticalSpacing(5)
            for number, (keys, action) in enumerate(rows):
                key_label = QLabel(keys)
                key_label.setObjectName("helpKey")
                description = QLabel(action)
                description.setWordWrap(True)
                grid.addWidget(key_label, number, 0)
                grid.addWidget(description, number, 1)
            grid.setColumnStretch(1, 1)
            shortcuts_content.addLayout(grid)
        for content in (start_content, portfolio_content, matched_content, analytics_content,
                        rebalance_content, views_content, shortcuts_content):
            content.addStretch()
        self.help_navigation.setCurrentRow(self.help_section_indices["Start"])
        buttons = widgets.dialog_buttons(self.help_dialog, QDialogButtonBox.Close)
        buttons.rejected.connect(self.help_dialog.reject)
        dialog_layout.addWidget(buttons)

    def _on_help_navigation_changed(self, row):
        if row == 0:
            self.start_tutorial()
        elif row > 0:
            self.help_sections.setCurrentIndex(row - 1)
            title = self.help_navigation.item(row).text()
            self.help_jump_menu.clear()
            self.help_jump_menu.addItem("Choose a section…")
            for label, _ in self.help_step_targets[title]:
                self.help_jump_menu.addItem(label)

    def _jump_to_help_step(self, index):
        if index < 1:
            return
        title = self.help_navigation.currentItem().text()
        targets = self.help_step_targets.get(title, ())
        if index > len(targets):
            return
        target = targets[index - 1][1]
        scroller = self.help_sections.currentWidget()
        scroller.verticalScrollBar().setValue(max(0, target.y() - 8))
        self.help_jump_menu.setCurrentIndex(0)

    def open_help(self):
        current = self.tabs.currentWidget()
        section = ("Analytics" if current is self.analytics_tab else
                   "Rebalance" if current is self.calcs_tab else
                   "Matched lots" if current is self.views["Lots"].parentWidget() else "Portfolio")
        self.help_navigation.setCurrentRow(self.help_section_indices[section])
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

    def start_tutorial(self):
        if self._tutorial_index < 0:
            self._tutorial_original_tab = self.tabs.currentWidget()
        self.help_dialog.hide()
        self._tutorial_index = 0
        self._show_tutorial_step()

    def _tutorial_steps(self):
        return (
            ("Portfolio", "Overview", "Start with the account overview",
             "The top summary shows account totals. The Overview table lists holdings, prices and broker position differences."),
            ("Trades", "Trades", "Review your trades",
             "Use Trades to inspect executions and their realized or open P&L. Filter BUY and SELL from this tab's toolbar."),
            ("Lots", "Lots", "Inspect open and matched lots",
             "Lots shows which purchases remain open and how sales are matched. Select a lot and use Matches → Match lots to review or adjust matching."),
            ("Cash", "Cash", "Follow cash activity",
             "Cash groups dated deposits, income, tax, fees and other activity from imported reports."),
            ("Analytics", "Analytics", "Explore account history",
             "Analytics contains performance, returns, exposure and price charts. Results can be partial when historical data is missing."),
            ("Rebalance", "Rebalance", "Plan a rebalance",
             "Set target weights and price scenarios here to estimate trades. This worksheet does not place orders."),
            (None, "sync", "Refresh data with Sync IBKR",
             "After configuring IBKR, use this button to refresh live data and recent reports. Hover over it for source status."),
            (None, "settings", "Configure the connection in Settings",
             "Open Settings to choose IBKR, enter the host, port, account and Flex query IDs, then apply. Save the Flex token there."),
            (None, "help", "Return to the guide anytime",
             "Open Help or press F1 for setup instructions and details about each area. Use File → Import Data for older reports."),
        )

    def _tutorial_target_rect(self, target):
        if target in ("sync", "settings", "help"):
            widget = {"sync": self.sync_button, "settings": self.settings_button,
                      "help": self.help_button}[target]
            return widget.geometry().translated(widget.parentWidget().mapTo(self, widget.parentWidget().rect().topLeft()))
        for index in range(self.tabs.count()):
            if self.tabs.tabBar().tabData(index) == target or self.tabs.tabText(index) == target:
                bar = self.tabs.tabBar()
                rect = bar.tabRect(index)
                return QRect(bar.mapTo(self, rect.topLeft()), rect.size())
        return QRect(self.tabs.mapTo(self, self.tabs.rect().topLeft()), self.tabs.size())

    def _show_tutorial_step(self):
        steps = self._tutorial_steps()
        tab, target, title, body = steps[self._tutorial_index]
        if tab is not None:
            for index in range(self.tabs.count()):
                if self.tabs.tabBar().tabData(index) == tab or self.tabs.tabText(index) == tab:
                    self.tabs.setCurrentIndex(index)
                    break
        self.centralWidget().layout().activate()
        self.tutorial.setGeometry(self.rect())
        self.tutorial.show_step(self._tutorial_index + 1, len(steps), title, body,
                                self._tutorial_target_rect(target), self.theme)
        QTimer.singleShot(0, self._refresh_tutorial_position)

    def _refresh_tutorial_position(self):
        if self._tutorial_index < 0 or not self.tutorial.isVisible():
            return
        target = self._tutorial_steps()[self._tutorial_index][1]
        self.tutorial.setGeometry(self.rect())
        self.tutorial.update_target(self._tutorial_target_rect(target))

    def _tutorial_back(self):
        if self._tutorial_index > 0:
            self._tutorial_index -= 1
            self._show_tutorial_step()

    def _tutorial_next(self):
        if self._tutorial_index + 1 == len(self._tutorial_steps()):
            self._tutorial_exit()
        else:
            self._tutorial_index += 1
            self._show_tutorial_step()

    def _tutorial_exit(self):
        self.tutorial.hide()
        self._tutorial_index = -1
        if self._tutorial_original_tab is not None:
            self.tabs.setCurrentWidget(self._tutorial_original_tab)
        self._tutorial_original_tab = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_tutorial_index", -1) >= 0:
            self._show_tutorial_step()

    def _make_menu(self):
        self.file_menu = self.menuBar().addMenu("File")
        file_menu = self.file_menu
        if self._on_manage_portfolios is not None:
            file_menu.addAction("Portfolios…", lambda: self._on_manage_portfolios(self))
            file_menu.addSeparator()
        file_menu.addAction("Import Data…", self.open_history_import)
        for title, handler in (("Export Workbook", self.export), ("Create SQLite Backup", self.backup)):
            action = QAction(title, self)
            action.triggered.connect(handler)
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction("Encrypt Database…", self.encrypt_database)
        file_menu.addAction("Show Database Recovery Code…", self.show_database_recovery_code)
        view_menu = self.menuBar().addMenu("View")
        self.show_summary_action = view_menu.addAction("Show account summary")
        self.show_summary_action.setCheckable(True)
        self.show_summary_action.setChecked(self._show_account_summary)
        self.show_summary_action.setShortcut(shortcut("Ctrl+H", "Ctrl+Shift+H"))
        self.show_summary_action.toggled.connect(self._set_account_summary_visible)
        self.compact_summary_action = view_menu.addAction("Compact summary view")
        self.compact_summary_action.setCheckable(True)
        self.compact_summary_action.setShortcut(shortcut("Ctrl+M", "Ctrl+Shift+M"))
        self.compact_summary_action.toggled.connect(self._set_compact_summary)
        self.expand_full_view_action = view_menu.addAction(
            "Expand full view", lambda: self.compact_summary_action.setChecked(False))
        self.expand_full_view_action.setShortcut(shortcut("Ctrl+L", "Ctrl+Shift+L"))
        self.expand_full_view_action.setEnabled(False)
        view_menu.addSeparator()
        self.zoom_in_action = view_menu.addAction("Zoom in", self.zoom_in)
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl++"), QKeySequence("Ctrl+=")])
        self.zoom_out_action = view_menu.addAction("Zoom out", self.zoom_out)
        self.zoom_out_action.setShortcut(QKeySequence("Ctrl+Shift+-"))
        self.zoom_out_context_shortcut = QShortcut(QKeySequence("Ctrl+-"), self)
        self.zoom_out_context_shortcut.setContext(Qt.WindowShortcut)
        self.zoom_out_context_shortcut.activated.connect(self._zoom_out_or_remove_rows)
        self.zoom_reset_action = view_menu.addAction("Reset zoom", lambda: self.set_zoom(100))
        self.zoom_reset_action.setShortcut(QKeySequence("Ctrl+0"))
        self.zoom_level_action = view_menu.addAction("Zoom: 100%")
        self.zoom_level_action.setEnabled(False)
        view_menu.addSeparator()
        self.save_view_action = view_menu.addAction(
            "Save current tab view",
            lambda: self.save_default_view(self._current_table_name()) if self._current_table_name() else None)
        self.restore_view_action = view_menu.addAction(
            "Restore saved tab view",
            lambda: self.restore_default_view(self._current_table_name()) if self._current_table_name() else None)
        view_menu.addSeparator()
        self.reset_tabs_action = view_menu.addAction("Reset tab order", self.reset_tab_order)
        view_menu.aboutToShow.connect(lambda: (self.save_view_action.setEnabled(self._current_table_name() is not None),
            self.restore_view_action.setEnabled(self._current_table_name() is not None and
                self._saved_default_view(self._current_table_name()) is not None)))
        self.matches_menu = self.menuBar().addMenu("Matches")
        self.match_lots_action = self.matches_menu.addAction("Match lots…", self.match_lots)
        self.matches_menu.addAction("Rebuild automatic matches", self.auto_match)
        self.auto_match_menu = QMenu("Auto-match remaining", self.matches_menu)
        self.auto_match_menu.setToolTipsVisible(True)
        self.matches_menu.addMenu(self.auto_match_menu)
        self.auto_match_menu.addAction("FIFO", self.auto_match_fifo)
        self.auto_match_menu.addAction("LIFO", self.auto_match_lifo)
        self.auto_match_menu.addAction("Max realized gain", self.auto_match_max_gain)
        unrealized_action = self.auto_match_menu.addAction("Max unrealized gain", self.auto_match_max_unrealized_gain)
        unrealized_action.setToolTip("Match highest USD cost lots first, leaving lower cost lots open")
        self.matches_menu.addSeparator()
        self.matches_menu.addAction("Clear match for selected lot", self.clear_match)
        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction("Take a tour…", self.start_tutorial)
        self.help_guide_action = QAction("User guide", self)
        self.help_guide_action.setShortcut(QKeySequence("F1"))
        self.help_guide_action.setShortcutContext(Qt.ApplicationShortcut)
        self.help_guide_action.triggered.connect(self.open_help)
        help_menu.addAction(self.help_guide_action)
        help_menu.addSeparator()
        help_menu.addAction("Check for updates…", self.check_for_updates)

    def check_for_updates(self):
        dialog = getattr(self, "_update_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return
        self._update_dialog = UpdateDialog(self._install_update, self)
        self._update_dialog.show()

    def _install_update(self, installer, installed):
        app = QApplication.instance()
        app.pending_update = (installer, installed)
        app.closeAllWindows()

        def cancel_if_still_open():
            if any(widget.isVisible() and widget.isWindow() for widget in app.topLevelWidgets()):
                app.pending_update = None
                shutil.rmtree(installer.parent, ignore_errors=True)
                QMessageBox.warning(None, "Update postponed",
                                    "Close the open portfolio window before updating.")

        QTimer.singleShot(0, cancel_if_still_open)

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
            if name == "Lots" and (not use_saved or not state or
                                   self.db.setting("lots_pnl_percentages_version") != "1"):
                self._place_lot_pnl_percentages()
                self.db.set_setting("lots_pnl_percentages_version", "1")
            if name == "Portfolio" and self.db.setting("price_move_bars_intro_applied") != "1":
                for column in (9, 10, 11):
                    view.showColumn(column)
                self.db.set_setting("price_move_bars_intro_applied", "1")
        if not use_saved:
            self.db.set_setting("header_layout_version", "3")
        if not portfolio_saved:
            self.db.set_setting("portfolio_columns_version", "4")

    def _place_lot_pnl_percentages(self):
        view = self.views["Lots"]
        header = view.horizontalHeader()
        columns = {key: index for index, (key, _, _) in enumerate(LOTS)}
        for amount, percent in (("realized_net", "realized_pct"),
                                ("net_unrealized", "unrealized_pct")):
            column = columns[percent]
            source = header.visualIndex(column)
            target = header.visualIndex(columns[amount])
            header.moveSection(source, target + (source > target))
            view.setColumnWidth(column, 125)
            view.showColumn(column)

    def _current_table_name(self):
        name = self.tabs.tabBar().tabData(self.tabs.currentIndex())
        return name if name in self.DEFAULT_TAB_ORDER else None

    def _tab_index(self, name):
        return next((index for index in range(self.tabs.count())
                     if self.tabs.tabBar().tabData(index) == name), -1)

    def reset_tab_order(self):
        for destination, name in enumerate(self.DEFAULT_TAB_ORDER):
            source = self._tab_index(name)
            if source >= 0 and source != destination:
                self.tabs.tabBar().moveTab(source, destination)
        self.statusBar().showMessage("Tab order reset", 5000)

    def _saved_default_view(self, name):
        try:
            state = json.loads(self.db.setting(f"default_view_{name}"))
        except (TypeError, ValueError):
            return None
        if name in ("Analytics", "Rebalance"):
            valid_versions = (1, 2) if name in ("Analytics", "Rebalance") else (1,)
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
                not dates_valid or
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
        if name == "Lots" and "realized_pct" not in state.get("columns", []):
            self._place_lot_pnl_percentages()
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

    def _stop_history_import_on_close(self):
        process = self._history_process
        if process is None:
            return True
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except OSError:
            if process.poll() is None:
                return False
        except subprocess.TimeoutExpired:
            return False

        if self._history_status_path is not None:
            try:
                pause_request_path(self._history_status_path).unlink(missing_ok=True)
            except OSError:
                pass
            status = read_status(self._history_status_path)
            if status["state"] in ("starting", "running"):
                status.update(state="interrupted", pid=None,
                              failed_window=status.get("current_window"), current_window=None,
                              finished_at=datetime.now().isoformat(timespec="seconds"),
                              message="Import interrupted when the app closed. Completed windows remain saved; select dates to import again.")
                try:
                    write_status(self._history_status_path, status)
                except OSError:
                    self.log.warning("Could not save interrupted history import status")
        self._history_process = None
        return True

    def closeEvent(self, event):
        if self._encryption_process is not None or self._move_process is not None:
            event.ignore()
            return
        if not self._stop_history_import_on_close():
            event.ignore()
            QMessageBox.warning(self, "Import Data", "Could not stop the history worker. Try closing the app again.")
            return
        self.calcs_tab.save_view()
        for name, view in self.views.items():
            state = bytes(view.horizontalHeader().saveState().toBase64()).decode("ascii")
            self.db.set_setting(f"header_{name}", state)
            model = self.models[name]
            self.db.set_setting(f"sort_{name}", f"{model.sort_column}:{'desc' if model.sort_order == Qt.DescendingOrder else 'asc'}")
        self.timer.stop()
        self.refresh_timer.stop()
        self.lots_refresh_timer.stop()
        self.calcs_refresh_timer.stop()
        if isinstance(self.market, IBKRMarketDataProvider):
            self.market.stop()
        for table in self.findChildren(ReportTable):
            table.finish_layout_persistence()
        self.db.conn.close()
        QApplication.instance().removeEventFilter(self._table_click_away_filter)
        super().closeEvent(event)

    def selected_row(self, name):
        index = self.views[name].currentIndex()
        return self.models[name].rows[index.row()] if index.isValid() else None

    def _selected_match_lot(self):
        view = self.views["Lots"]
        indexes = view.selectedIndexes()
        if not indexes:
            return None
        index = view.currentIndex()
        if not index.isValid() or not view.selectionModel().isSelected(index):
            index = indexes[0]
        lot = self.models["Lots"].rows[index.row()]
        return lot if lot.get("position_id") is not None else None

    def _update_match_lots_availability(self, *_):
        enabled = self._selected_match_lot() is not None
        self.match_lots_button.setEnabled(enabled)
        if hasattr(self, "match_lots_action"):
            self.match_lots_action.setEnabled(enabled)

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

    def _build_table_menu(self, name, point):
        view = self.views[name]
        index = view.indexAt(point)
        if index.isValid() and index not in view.selectedIndexes():
            view.setCurrentIndex(index)
        menu = QMenu(self)
        copy_action = menu.addAction("Copy", lambda: copy_table_selection(view))
        copy_action.setEnabled(bool(view.selectedIndexes()))
        if name == "Portfolio":
            row = self.selected_row("Portfolio")
            if row:
                menu.addSeparator()
                menu.addAction("Position details", lambda checked=False: self.show_position_details())
        elif name == "Lots":
            row = self._selected_match_lot()
            if row:
                menu.addSeparator()
                menu.addAction("Match lots…", lambda checked=False: self.match_lots())
        return menu

    def _table_context_menu(self, name, point):
        view = self.views[name]
        self._build_table_menu(name, point).exec(view.viewport().mapToGlobal(point))

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
        if name == "Lots":
            self._update_match_lots_availability()

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
        trades = [t for t in self.service.matching.trade_rows() if t["position_id"] == position["id"]]
        lots = [lot for lot in self.service.lots_rows() if lot.get("position_id") == position["id"]]
        for title, records, specs in (
            ("Trades", trades, [("trade_date", "Date"), ("side", "Side"), ("quantity", "Qty"),
                                ("price", "Price"), ("commission_usd", "Fee USD"),
                                ("accrued_interest_usd", "Accrued interest USD"), ("match_status", "Match")]),
            ("Lots", lots, [("buy_date", "Buy"), ("buy_qty", "Qty"), ("buy_price", "Cost"),
                             ("sell_date", "Sale"), ("realized_net", "Realized USD"),
                             ("net_unrealized", "Open P&L USD")])):
            layout.addWidget(QLabel(title))
            table = ReportTable(len(records), len(specs))
            table.setHorizontalHeaderLabels([label for _, label in specs])
            numeric_keys = {"quantity", "price", "commission_usd", "accrued_interest_usd", "buy_qty", "buy_price",
                            "sell_price", "realized_net", "net_unrealized"}
            numeric_formats = {column: (2, "plain") for column, (key, _) in enumerate(specs)
                               if key in numeric_keys}
            table.configure_numbers(self.db, f"position_details_{title.lower()}_decimals_{self.portfolio_id}",
                                    numeric_formats)
            table.remember_layout(self.db, f"position_details_{title.lower()}_layout_{self.portfolio_id}")
            for row_num, record in enumerate(records):
                for col_num, (key, _) in enumerate(specs):
                    value = record.get(key)
                    table.setItem(row_num, col_num, table.numeric_item(col_num, value)
                                  if key in numeric_keys else SortItem("" if value is None else str(value)))
            table.setSortingEnabled(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            layout.addWidget(table)
        buttons = widgets.dialog_buttons(dialog, QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        enable_label_copy(dialog)
        dialog.exec()

    @staticmethod
    def _pct(value):
        return f"{value:,.2%}" if value is not None else "—"

    def _show_all_columns(self, name):
        for column in range(self.models[name].columnCount()):
            self.views[name].showColumn(column)

    def _refresh_models(self):
        if hasattr(self, "lots_refresh_timer"):
            self.lots_refresh_timer.stop()
        with self.service.shared_rows():
            portfolio = self.service.portfolio_rows()
            lots = self.service.lots_rows(portfolio)
            trades = self.service.trade_rows(lots)
        self._stream_rows = portfolio
        self._quote_dirty_tabs.clear()
        for name in self.models:
            self._refresh_model(name, portfolio if name == "Portfolio" else
                                lots if name == "Lots" else trades)
        if self.tabs.currentWidget() is self.calcs_tab:
            self._refresh_calcs_live()
        if self.tabs.currentWidget() is self.cash_tab:
            self.cash_tab.refresh()
        if self.tabs.currentWidget() is self.analytics_tab:
            self.analytics_tab.refresh(portfolio_rows=portfolio, lots=lots, trade_rows=trades)
        self._refresh_summary(portfolio, lots)

    def _refresh_lots_and_summary(self):
        rows = self._stream_rows if self._stream_rows is not None else self.service.portfolio_rows()
        lots = self.service.lots_rows(rows)
        current = self.tabs.currentWidget()
        if current is self.grid_tabs["Lots"]:
            self._refresh_model("Lots", lots)
            self._quote_dirty_tabs.discard("Lots")
        elif current is self.grid_tabs["Trades"]:
            self._refresh_model("Trades", self.service.trade_rows(lots))
            self._quote_dirty_tabs.discard("Trades")
        self._refresh_summary(rows, lots)

    def _refresh_summary(self, rows=None, lots=None):
        self._update_account_heading()
        rows = (rows if rows is not None else
                self._stream_rows if self._stream_rows is not None else
                self.service.portfolio_rows())
        lots = lots if lots is not None else self.service.lots_rows(rows)
        active = [r for r in rows if abs(float(r["position_qty"] or 0)) > 1e-9]
        eligible = [r for r in active if r["reconciliation"] != "DIFFERENCE"]
        daily_scope = [r for r in rows if abs(float(r["position_qty"] or 0)) > 1e-9
                       or r.get("traded_today")]
        daily = sum(r["daily_pnl"] for r in daily_scope
                    if r["reconciliation"] != "DIFFERENCE" and r["daily_pnl"] is not None)
        closed_lots = [r for r in lots if str(r.get("lot_state", "")).startswith("Closed")]
        realized_rows = [r for r in closed_lots if r.get("realized_net") is not None]
        realized = sum(r["realized_net"] for r in realized_rows)
        pending_realized = sum(bool(r.get("fee_pending")) for r in closed_lots)
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
        pending_open = sum(bool(r.get("open_pnl_fee_pending")) for r in active)
        if pending_open:
            issues.append(f"{pending_open:,} holding fee{'s' if pending_open != 1 else ''} pending")
        if missing:
            tickers = ", ".join(r["display_ticker"] for r in missing[:3])
            suffix = ", …" if len(missing) > 3 else ""
            reason = ("price" if all(r["last_px"] is None for r in missing) else
                      "FX" if all(not r["fx_to_usd"] for r in missing) else "price or FX")
            issues.append(f"{len(missing):,} holding{'s' if len(missing) != 1 else ''} missing {reason}: {tickers}{suffix}")
        incomplete = bool(issues)
        def subtotal(key, scope=active):
            covered = [r for r in scope if r["reconciliation"] != "DIFFERENCE" and
                       r[key] is not None]
            if not covered:
                return "—"
            amount = sum(r[key] for r in covered)
            return f"{'Partial ' if len(covered) != len(scope) else ''}${amount:,.2f}"
        daily_text = subtotal("daily_pnl", daily_scope)
        daily_fee_pending = any(r.get("daily_pnl_fee_pending") and r["daily_pnl"] is not None
                                for r in daily_scope if r["reconciliation"] != "DIFFERENCE")
        daily_pct_available = (daily_text != "—" and nav is not None and
                               math.isfinite(nav) and nav > 0)
        if daily_pct_available:
            daily_text += f" ({daily / nav:.2%})"
        if daily_fee_pending:
            daily_text = "Estimated " + daily_text
        open_text = subtotal("unrealized_pnl")
        open_fee_pending = open_text != "—" and any(
            r.get("open_pnl_fee_pending") and r["unrealized_pnl"] is not None for r in eligible)
        if open_fee_pending:
            open_text = "Estimated " + open_text
        realized_text = ("—" if differences or (closed_lots and not realized_rows) else
                         f"{'Partial ' if len(realized_rows) < len(closed_lots) else ''}${realized:,.2f}")
        if pending_realized and realized_text != "—":
            realized_text = "Estimated " + realized_text
        amounts = {
            "nav": nav_text, "cash": cash_text,
            "holdings": subtotal("value"),
            "daily": daily_text,
            "open": open_text,
            "realized": realized_text,
        }
        for key, text in amounts.items():
            label = self.metric_values[key]
            label.setText(text)
            note = ""
            if key == "daily":
                note = " Security-position P&L excludes cash income and standalone FX effects."
                if daily_fee_pending:
                    note += " Pending fees or rebates are excluded until IBKR supplies them."
                if daily_pct_available:
                    note += " Percentage is the displayed P&L divided by current NAV."
            if (key == "open" and open_fee_pending) or (key == "realized" and pending_realized):
                note += " Pending fees or rebates are excluded until IBKR supplies them."
            if key == "daily" and any(str(r.get("price_source", "")).startswith("SAVED SESSION BAR")
                                      for r in eligible):
                note += " Saved session bars may reflect an unfinished trading day and are not live quotes."
            label.setToolTip((("Subtotal only: " + "; ".join(issues) + note) if
                              text.startswith(("Partial ", "Estimated Partial ")) else
                              ("Incomplete: " + "; ".join(issues) + note) if text == "—" and incomplete else
                              "Current daily P&L needs a current-session price and prior close" if
                              key == "daily" and text == "—" else
                              "Not available from the connected account" if text == "—" else note.strip()))
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
        if self._compact_geometry is not None:
            self._fit_compact_summary()

    def match_lots(self):
        lot = self._selected_match_lot()
        if lot is None:
            self.statusBar().showMessage("Select a lot in the Lots tab to match it", 8000)
            return
        dialog = LotMatchDialog(self.service, lot["position_id"], lot["ticker"], self,
                                theme=self.theme, sale_id=lot.get("sell_trade_id"))
        if dialog.exec() == QDialog.Accepted:
            self._refresh_models()

    def auto_match(self):
        self._run_matching(self.service.matching.rebuild_auto)

    def auto_match_fifo(self):
        self._run_matching(self.service.matching.auto_fifo)

    def auto_match_lifo(self):
        self._run_matching(self.service.matching.auto_lifo)

    def auto_match_max_gain(self):
        self._run_matching(self.service.matching.auto_max_gain)

    def auto_match_max_unrealized_gain(self):
        self._run_matching(self.service.matching.auto_max_unrealized_gain)

    def _run_matching(self, action):
        try:
            action()
            self._refresh_models()
        except Exception as exc:
            QMessageBox.warning(self, "Matching failed", str(exc))

    def clear_match(self):
        lot = self.selected_row("Lots")
        if not (lot and lot.get("match_id")):
            self.statusBar().showMessage("Select a matched lot in the Lots tab to clear its match", 8000)
            return
        try:
            self.service.matching.clear(lot["match_id"])
            self._refresh_models()
        except Exception as exc:
            QMessageBox.warning(self, "Clear match failed", str(exc))

    def backup(self):
        try:
            path = self.db.backup()
            QMessageBox.information(self, "Backup created", str(path))
            if self.db.recovery_code:
                self.show_database_recovery_code()
                self.db.recovery_code = None
        except Exception as exc:
            QMessageBox.warning(self, "Backup failed", str(exc))

    def show_database_recovery_code(self):
        code = secret_store.load_database_key(self.db.path)
        if code is None:
            QMessageBox.information(self, "Database recovery code",
                                    "A recovery code will be created when the database is encrypted or backed up.")
            return
        message = QMessageBox(self)
        message.setWindowTitle("Save database recovery code")
        message.setIcon(QMessageBox.Warning)
        message.setText("Save this recovery code outside the data and backup folders. "
                        "You need it to open an encrypted database if its stored key is unavailable, "
                        "including on another device or user account.\n\n" + code)
        message.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        message.exec()

    def encrypt_database(self):
        if self.db.encrypted:
            QMessageBox.information(self, "Encrypt Database", "This database is already encrypted.")
            return
        if (self._history_process is not None or self._sync_all_active or self._sync_all_queue or
                any(process is not None and process.poll() is None for process in
                    (self._sync_process, self._analytics_process, self._cash_process))):
            QMessageBox.warning(self, "Encrypt Database", "Wait for imports and synchronization to finish.")
            return
        try:
            code = secret_store.load_database_key(self.db.path) or token_hex(32)
            secret_store.save_database_key(self.db.path, code)
            self.show_database_recovery_code()
            self._encryption_status_path = self.db.path.parent / f"database-encryption-{uuid4().hex}.json"
            self._pause_for_database_worker()
            args = ["--database", str(self.db.path), "--status-file", str(self._encryption_status_path)]
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--encrypt-database", *args]
                workdir = self.db.path.parent
            else:
                command = [sys.executable, "-m", "scripts.encrypt_database", *args]
                workdir = Path(__file__).resolve().parent.parent
            self._encryption_process = subprocess.Popen(
                command, cwd=workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.setEnabled(False)
            self._encryption_progress = QProgressDialog("Encrypting database…", None, 0, 0)
            self._encryption_progress.setWindowTitle("Encrypt Database")
            self._encryption_progress.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
            self._encryption_progress.setWindowModality(Qt.ApplicationModal)
            self._encryption_progress.show()
            self._encryption_poll_timer.start(250)
        except Exception as exc:
            self._encryption_process = None
            self._resume_after_encryption()
            QMessageBox.warning(self, "Encrypt Database", str(exc))

    def _resume_after_encryption(self):
        self._resume_database_worker(self.db.path)

    def _pause_for_database_worker(self):
        if isinstance(self.market, IBKRMarketDataProvider):
            self.market.stop()
        for timer in (self.timer, self.refresh_timer, self.lots_refresh_timer,
                      self.calcs_refresh_timer):
            timer.stop()
        self.db.conn.close()

    def _resume_database_worker(self, path, *, start_services=True):
        reopened = Database(path)
        self.db.__dict__.update(reopened.__dict__)
        self.setEnabled(True)
        if start_services:
            self._start_database_services()

    def _start_database_services(self):
        self.timer.start(250)
        self._set_refresh_interval()
        self._start_provider()

    def move_database(self):
        if (self._move_process is not None or self._encryption_process is not None or
                self._history_process is not None or self._sync_all_active or self._sync_all_queue or
                any(process is not None and process.poll() is None for process in
                    (self._sync_process, self._analytics_process, self._cash_process))):
            QMessageBox.warning(self, "Move database", "Wait for imports and synchronization to finish.")
            return
        source = self.db.path
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a parent folder for the database", str(source.parent.parent))
        if not directory:
            return
        root = Path(directory).resolve()
        if root == source.parent.parent.resolve():
            QMessageBox.information(self, "Move database", "The database is already in this folder.")
            return
        if root == source.parent.resolve() or source.parent.resolve() in root.parents:
            QMessageBox.warning(self, "Move database", "Choose a folder outside the current portfolio folder.")
            return
        answer = QMessageBox.question(
            self, "Move database",
            f"Copy the database into a new portfolio folder inside:\n{root}\n\n"
            "The original will be kept. The app will use the verified copy when the move finishes.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if (self._history_process is not None or self._sync_all_active or self._sync_all_queue or
                any(process is not None and process.poll() is None for process in
                    (self._sync_process, self._analytics_process, self._cash_process))):
            QMessageBox.warning(self, "Move database", "Wait for imports and synchronization to finish.")
            return
        paused = False
        try:
            self.calcs_tab.save_view()
            for table in self.findChildren(ReportTable):
                table.finish_layout_persistence()
            self._move_source_path = source
            self._move_status_path = source.parent / f"database-move-{uuid4().hex}.json"
            self._pause_for_database_worker()
            paused = True
            args = ["--database", str(source), "--directory", str(root),
                    "--status-file", str(self._move_status_path)]
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--move-database", *args]
                workdir = source.parent
            else:
                command = [sys.executable, "-m", "scripts.move_database", *args]
                workdir = Path(__file__).resolve().parent.parent
            self._move_process = subprocess.Popen(
                command, cwd=workdir, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.setEnabled(False)
            self._move_progress = QProgressDialog("Copying and verifying database…", None, 0, 0)
            self._move_progress.setWindowTitle("Move database")
            self._move_progress.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
            self._move_progress.setWindowModality(Qt.ApplicationModal)
            self._move_progress.show()
            self._move_poll_timer.start(250)
        except Exception as exc:
            self._move_process = None
            if paused:
                self._resume_database_worker(source)
            QMessageBox.warning(self, "Move database", str(exc))

    def _poll_database_move(self):
        if self._move_process is None or self._move_process.poll() is None:
            return
        code = self._move_process.returncode
        self._move_poll_timer.stop()
        self._move_process = None
        if self._move_progress is not None:
            self._move_progress.close()
            self._move_progress = None
        try:
            result = json.loads(self._move_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"state": "failed"}
        try:
            self._move_status_path.unlink(missing_ok=True)
        except OSError:
            pass
        completed = (code == 0 and result.get("state") == "completed" and
                     isinstance(result.get("database"), str))
        path = Path(result["database"]) if completed else self._move_source_path
        try:
            self._resume_database_worker(path, start_services=not completed)
        except Exception:
            QMessageBox.critical(self, "Move database", "The portfolio could not be reopened. "
                                 "Both database files were preserved; restart the app to recover.")
            QApplication.quit()
            return
        if completed:
            self.database_location.setText(str(self.db.path))
            self.log = configure_diagnostics(self.db.path, "monitor")
            answer = QMessageBox.question(
                self, "Database moved",
                "The app is now using:\n" + str(self.db.path) +
                "\n\nThe original database remains at:\n" + str(self._move_source_path) +
                "\n\nRestart the app now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                try:
                    command = ([sys.executable] if getattr(sys, "frozen", False) else
                               [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")])
                    subprocess.Popen(command, cwd=self.db.path.parent)
                    self.settings_dialog.close()
                    self.close()
                except OSError as exc:
                    self._start_database_services()
                    QMessageBox.warning(self, "Restart failed", str(exc))
            else:
                self._start_database_services()
        else:
            QMessageBox.warning(self, "Database move failed",
                                "The original database remains in use. Check the destination folder and try again.")

    def _poll_encryption(self):
        if self._encryption_process is None or self._encryption_process.poll() is None:
            return
        code = self._encryption_process.returncode
        self._encryption_poll_timer.stop()
        self._encryption_process = None
        if self._encryption_progress is not None:
            self._encryption_progress.close()
            self._encryption_progress = None
        try:
            result = json.loads(self._encryption_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"state": "failed"}
        try:
            self._encryption_status_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._resume_after_encryption()
        except Exception:
            QMessageBox.critical(self, "Encrypt Database", "The portfolio could not be reopened. "
                                 "The database files were preserved; close the app and restore using the recovery code.")
            QApplication.quit()
            return
        if code == 0 and result.get("state") == "completed" and self.db.encrypted:
            QMessageBox.information(self, "Database encrypted",
                                    "The database and new backups are encrypted. The previous plaintext copy remains at:\n" +
                                    result["plaintext_copy"] + "\nRemove it after checking an encrypted backup restore.")
        else:
            QMessageBox.warning(self, "Encrypt Database",
                                "Encryption did not complete. The original database remains available.")

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
            if self._history_process is not None:
                raise ValueError("Wait for the history import to finish before applying Settings.")
            values = {}
            for key, widget in self.settings_fields.items():
                value = widget.currentText() if isinstance(widget, QComboBox) else widget.text().strip()
                values[key] = "1" if key == "autosave" and value == "On" else "0" if key == "autosave" else value
            if int(values["ib_port"]) <= 0 or int(values["ib_client_id"]) < 0 or int(values["refresh_seconds"]) < 1:
                raise ValueError("Port, client ID, and refresh interval must be valid positive numbers")
            self.db.validate_account_change(values["ib_account_id"])
            for key, value in values.items():
                self.db.set_setting(key, value)
            self._refresh_history_import_coverage()
            self._refresh_flex_token_status()
            self.use_ibkr_button.setVisible(values["provider"] != "IBKR")
            self._set_refresh_interval()
            self._start_provider()
            self._queue_configured_syncs()
            self._update_sync_button()
            self._refresh_models()
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

    def _request_levels_history(self, position, first_saved_day=None):
        if not isinstance(self.market, IBKRMarketDataProvider):
            raise RuntimeError("Connect to IBKR in Settings before loading older prices.")
        return self.market.request_price_history(position, first_saved_day)

    def _start_provider(self, force_history=False):
        if self._sync_paused:
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
        if self._sync_paused:
            return
        if isinstance(self.market, MockMarketDataProvider):
            ids = self.market.refresh(self.db.rows("positions", self.portfolio_id))
            self.models["Portfolio"].update_positions(ids)
            self._refresh_model("Lots")
            self._update_status()
        elif not force and time.monotonic() - self._last_connection_attempt < 3:
            self._update_status()
        else:
            self._start_provider(force_history=False)

    def _periodic_refresh(self):
        data_version = self.db.conn.execute("PRAGMA data_version").fetchone()[0]
        database_changed = data_version != self._last_db_data_version
        self._last_db_data_version = data_version
        stamp = self.db.metadata("ibkr_flex_sync_completed")
        flex_completed = bool(stamp and stamp != self._last_flex_sync_stamp)
        if flex_completed:
            self._last_flex_sync_stamp = stamp
        if database_changed or flex_completed:
            self._refresh_models()
        analytics_updated = False
        if self._analytics_process is not None and self._analytics_process.poll() is not None:
            self._analytics_process = None
            analytics_updated = True
        if self._cash_process is not None and self._cash_process.poll() is not None:
            self._cash_process = None
            self.cash_tab.refresh()
        analytics_stamp = self.db.metadata("ibkr_analytics_sync_completed")
        if analytics_stamp and analytics_stamp != self._last_analytics_sync_stamp:
            self._last_analytics_sync_stamp = analytics_stamp
            analytics_updated = True
        if analytics_updated:
            self.analytics_tab.refresh()
        cash_stamp = self.db.metadata("ibkr_cash_sync_completed")
        self._advance_sync_queue()
        if self._sync_paused:
            self._update_sync_button()
            return
        running = any(process is not None and process.poll() is None for process in
                      (self._sync_process, self._analytics_process, self._cash_process))
        if not self._sync_all_active and not running:
            today = date.today().isoformat()
            if (not trade_sync_paused(self.db) and self.db.setting("flex_query_id")
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
                "display_ticker": (option_label(symbol, broker_row.get("expiry"),
                    broker_row.get("option_right"), broker_row.get("strike"))
                    or broker_row.get("local_symbol") or symbol)
                    if broker_row["security_type"] == "OPT" else symbol,
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
        self._poll_history_import()
        if self._sync_paused:
            self._advance_sync_queue()
            self._update_sync_progress()
            if time.monotonic() - self._last_ui_status_at >= 1:
                self._update_sync_button()
                self._update_status()
            return
        was_connected = self.market.connected
        was_sync_pending = self._live_sync_pending
        ids = self.market.drain()
        quote_issue_changed = False
        for position_id in (key for key in ids if isinstance(key, int)):
            status = str(self.market.quote(position_id).get("status") or "")
            issue = status if status not in ("", "OK", "DELAYED", "CACHED QUOTE",
                                             "MOCK", "WAITING FOR PRICE") else ""
            if issue != self._quote_issue_statuses.get(position_id, ""):
                self._quote_issue_statuses[position_id] = issue
                quote_issue_changed = True
        if was_connected and not self.market.connected:
            self._disconnected_since = time.monotonic()
            # Persist the last known prices once, then resume the normal throttle.
            self._last_quote_cache_at = 0.0
        elif self.market.connected:
            self._disconnected_since = None
        self.calcs_tab.process_search_results()
        if self._live_sync_pending and (
                (was_connected and not self.market.connected) or
                (isinstance(self.market, IBKRMarketDataProvider) and not self.market.connected and
                 self.market.thread is not None and not self.market.thread.is_alive())):
            self._live_sync_pending = False
        if (not self._sync_all_active and not was_connected and self.market.connected and
            isinstance(self.market, IBKRMarketDataProvider)):
            self._launch_flex_sync()
        if ids:
            if any(str(position_id).startswith("idea:") for position_id in ids):
                self.calcs_tab.cache_idea_quotes(ids)
            position_ids = {key for key in ids if isinstance(key, int)}
            if position_ids:
                # Quotes change valuations only for their own instruments.
                changed = self.service.portfolio_rows(position_ids)
                known_ids = {row["id"] for row in self._stream_rows or ()}
                if self._stream_rows is None or any(
                        row["id"] not in known_ids for row in changed):
                    self._refresh_models()
                else:
                    by_id = {row["id"]: row for row in changed}
                    self._stream_rows = [by_id.get(row["id"], row) for row in self._stream_rows]
                    self._quote_dirty_tabs.update(self.grid_tabs)
                    if self.tabs.currentWidget() is self.grid_tabs["Portfolio"]:
                        self.models["Portfolio"].update_positions(position_ids, changed)
                        self._quote_dirty_tabs.discard("Portfolio")
                    if not self.lots_refresh_timer.isActive():
                        self.lots_refresh_timer.start(1000)
            if (self.tabs.currentWidget() is self.analytics_tab and
                    self.analytics_tab.sections.currentWidget() is self.analytics_tab.exposure_page and
                    time.monotonic() - getattr(self, "_last_exposure_refresh", 0) >= 2):
                self.analytics_tab.refresh_exposure(self._stream_rows)
                self._last_exposure_refresh = time.monotonic()
            if (self.tabs.currentWidget() is self.calcs_tab and
                    not self.calcs_refresh_timer.isActive()):
                self.calcs_refresh_timer.start(1000)
            if (isinstance(self.market, IBKRMarketDataProvider) and
                    time.monotonic() - self._last_quote_cache_at >= 15):
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
                    needs_model_refresh = True
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
                        if self.db.save_fx_history(currency, bars):
                            # Only a trade whose FX was previously unknown can
                            # change a match; re-matching the whole book on every
                            # rate update is needless work.
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
                        if (self.tabs.currentWidget() is self.analytics_tab and
                                self.analytics_tab.sections.currentWidget() is self.analytics_tab.levels_page):
                            self.analytics_tab.levels_page.refresh()
                            self.analytics_tab.levels_page.set_history_message(
                                con_id, "IBKR returned daily prices; earlier dates may still be unavailable.")
                        needs_model_refresh = True
                    elif (self.tabs.currentWidget() is self.analytics_tab and
                          self.analytics_tab.sections.currentWidget() is self.analytics_tab.levels_page):
                        self.analytics_tab.levels_page.set_history_message(
                            con_id, "IBKR returned no completed daily prices for this request.")
                elif event[0] == "history_error":
                    _, con_id, message = event
                    if (self.tabs.currentWidget() is self.analytics_tab and
                            self.analytics_tab.sections.currentWidget() is self.analytics_tab.levels_page):
                        self.analytics_tab.levels_page.set_history_message(con_id, message)
                elif event[0] == "chart_history_complete":
                    _, con_id, reached_five_years = event
                    self.analytics_tab.levels_page.set_history_complete(
                        con_id, reached_five_years)
            if (exposure_updated and self.tabs.currentWidget() is self.analytics_tab and
                    self.analytics_tab.sections.currentWidget() is self.analytics_tab.exposure_page and
                    time.monotonic() - getattr(self, "_last_exposure_refresh", 0) >= 2):
                self.analytics_tab.refresh_exposure(self._stream_rows)
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
        self._advance_sync_queue()
        self._update_sync_progress()
        if (was_connected != self.market.connected or quote_issue_changed or
                was_sync_pending != self._live_sync_pending or
                time.monotonic() - self._last_ui_status_at >= 1):
            self._update_sync_button()
            self._update_status()

    def _update_status(self):
        self._last_ui_status_at = time.monotonic()
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
            status = ("STALE QUOTE" if row.get("data_status") == "STALE QUOTE" else
                      str(self.market.quote(row["id"]).get("status") or ""))
            if status and status not in ("OK", "DELAYED", "CACHED QUOTE", "MOCK", "WAITING FOR PRICE"):
                quote_issues.append(f"{row['display_ticker']}: {status}")
        issues_text = "; ".join(quote_issues[:3])
        if len(quote_issues) > 3:
            issues_text += f"; and {len(quote_issues) - 3} more"
        last_sync = (self.db.metadata("ibkr_flex_sync_completed") or "—").replace("T", " ")
        sync_status = self._trade_flex_display_status()
        if sync_status in ("failed", "statement pending", "paused"):
            link_color = COLORS[self.theme]["warning" if sync_status == "statement pending" else "negative"]
            self.trade_sync_status.setText(
                f"Trades synced: {escape(last_sync)} (<a href=\"details\" "
                f"style=\"color: {link_color}; text-decoration: underline;\">{sync_status}</a>)")
            self.trade_sync_status.setToolTip(
                "Click for the Flex statement response and diagnostic log" if sync_status == "statement pending"
                else "Click to review the paused automatic sync and retry" if sync_status == "paused"
                else "Click for the trade sync error and diagnostic log")
            self.trade_sync_status.setCursor(Qt.PointingHandCursor)
        else:
            self.trade_sync_status.setText(f"Trades synced: {escape(last_sync)} ({escape(sync_status)})")
            self.trade_sync_status.setToolTip("")
            self.trade_sync_status.unsetCursor()
        recent_check = (self.db.metadata("ibkr_last_execution_poll") or "—").replace("T", " ")
        self.statusBar().showMessage(
            ("Pausing sync…    " if self._sync_paused and
             (self._history_process is not None or any(
                 process is not None and process.poll() is None for process in
                 (self._sync_process, self._analytics_process, self._cash_process))) else
             "Sync paused    " if self._sync_paused else "") +
            f"IBKR: {connected}    Data: {mode}    Last price: {updated}"
            f"{'    Quote issues: ' + str(len(quote_issues)) if quote_issues else ''}")
        last_history = self.db.conn.execute("SELECT MAX(value) FROM app_metadata WHERE key LIKE 'history_refresh_%'").fetchone()[0] or "—"
        self.settings_info.setText(
            f"Connection: {connected}    Market data: {mode}    Subscribed symbols: {self.market.subscriptions:,}\n"
            f"Current prices: {current}/{total}    Quote issues: {issues_text or 'none'}\n"
            f"Last historical refresh: {last_history}\n"
            f"Recent Gateway executions checked: {recent_check}\n"
            f"Historical Flex sync: {last_sync} ({sync_status})"
            f"  {self.db.metadata('ibkr_flex_sync_error')[:120]}\n"
            f"{self._connection_error[:120]}\nDatabase: {self.db.path}")
