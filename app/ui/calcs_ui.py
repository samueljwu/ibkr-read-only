"""Editable target weights and read-only trade estimates."""

import json
import math
import re
import time
from copy import deepcopy
from datetime import date, datetime

from PySide6.QtCore import QByteArray, QEvent, QStringListModel, Qt, QTimer
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QCompleter, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from app.finance.calcs import calculate_targets, scale_target_weights, scenario_metrics
from app.finance.calculations import choose_price, valid_price
from app.broker.market_data import IBKRMarketDataProvider
from app.ui.table_ui import InputReportTable, SortItem, fit_option_name_column
from app.ui.ui_theme import (TOOLBAR_GROUP_SPACING, TOOLBAR_ITEM_SPACING,
                          style_toolbar_dropdown)


def _text(value, places=2):
    return "—" if value is None else f"{value:,.{places}f}"


class CalcsTable(InputReportTable):
    pass


class CalcsTab(QWidget):
    HEADERS = ("Position", "Quantity", "Value USD", "Current weight",
               "Target weight", "Buy / Sell USD", "Est. units",
               "Current price", "Upside type", "Upside value", "Upside result",
               "Downside type", "Downside value", "Downside result", "Upside / downside", "Idea")
    NUMERIC_COLUMNS = (1, 2, 3, 5, 6, 7, 10, 13, 14)
    DEFAULT_DECIMALS = {1: 4, 2: 2, 3: 4, 5: 2, 6: 4, 7: 2, 10: 2, 13: 2, 14: 2}

    def __init__(self, db, service, portfolio_id, parent=None):
        super().__init__(parent)
        self.setObjectName("calcsPage")
        self.db, self.service, self.portfolio_id = db, service, portfolio_id
        self._loading = False
        self.market = None
        self.require_ibkr_ideas = False
        self._selected_contract = None
        self._search_request = None
        self._search_labels = {}
        self._last_idea_quote_cache_at = 0.0
        self._frozen_snapshot = None
        self._keyboard_controls = {}
        self._scenario_controls = {}
        self._setting_key = f"calcs_targets_{portfolio_id}"
        self._scenario_key = f"calcs_scenarios_{portfolio_id}"
        self._ideas_key = f"rebalance_ideas_{portfolio_id}"
        self.mode = "individual"
        self._view_key = f"calcs_view_mode_v2_{portfolio_id}"
        self.view_mode = self.db.setting(self._view_key) or self.db.setting(f"calcs_view_mode_{portfolio_id}")
        if self.view_mode not in ("allocation", "cases", "all"):
            self.view_mode = "all"
        elif not self.db.setting(self._view_key) and self.view_mode == "allocation":
            self.view_mode = "all"
        self.targets = {}
        self.cash_target = None
        self._target_error = ""
        self.column_decimals = self._load_decimals()
        self._load()
        self._load_scenarios()
        self._load_ideas()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(TOOLBAR_GROUP_SPACING)
        self.mode_label = QLabel()
        self.mode_label.setWordWrap(True)
        self.mode_label.setMinimumWidth(0)
        self.mode_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(self.mode_label, 1)
        view_controls = QHBoxLayout()
        view_controls.setSpacing(TOOLBAR_ITEM_SPACING)
        view_controls.addWidget(QLabel("Show"))
        self.view_choice = QComboBox()
        style_toolbar_dropdown(self.view_choice)
        self.view_choice.addItem("All", "all")
        self.view_choice.addItem("Allocation only", "allocation")
        self.view_choice.addItem("Upside / downside", "cases")
        self.view_choice.setCurrentIndex(self.view_choice.findData(self.view_mode))
        self.view_choice.setFixedWidth(240)
        self.view_choice.setToolTip("Choose the allocation columns, scenario columns, or all columns")
        self.view_choice.currentIndexChanged.connect(lambda: self._set_view_mode(self.view_choice.currentData()))
        view_controls.addWidget(self.view_choice)
        top.addLayout(view_controls)
        self.pause_button = QPushButton("Pause updates")
        self.pause_button.setObjectName("rebalanceSyncButton")
        self.pause_button.setProperty("syncState", "active")
        self.pause_button.setToolTip("Freeze prices, weights and cash in Rebalance while the other tabs keep syncing")
        self.pause_button.clicked.connect(self.toggle_pause)
        top.addWidget(self.pause_button)
        reset = QPushButton("Reset targets")
        reset.setObjectName("secondary")
        reset.clicked.connect(self.reset_targets)
        top.addWidget(reset)
        layout.addLayout(top)
        add_row = QHBoxLayout()
        add_row.addWidget(QLabel("Add idea"))
        self.idea_input = QLineEdit()
        self.idea_input.setPlaceholderText("Ticker, e.g. NVDA")
        self.idea_input.setMaxLength(60)
        self.idea_input.setFixedWidth(230)
        self.idea_input.textEdited.connect(self._idea_text_edited)
        self.idea_input.returnPressed.connect(self.add_idea)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._search_ideas)
        self._suggestion_model = QStringListModel(self)
        self._completer = QCompleter(self._suggestion_model, self)
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.popup().setMinimumWidth(440)
        self._completer.activated[str].connect(self._select_suggestion)
        self.idea_input.setCompleter(self._completer)
        add_row.addWidget(self.idea_input)
        add_button = QPushButton("Add")
        add_button.setObjectName("secondary")
        add_button.clicked.connect(self.add_idea)
        add_row.addWidget(add_button)
        add_row.addStretch()
        layout.addLayout(add_row)
        self.idea_notice = QLabel()
        self.idea_notice.setObjectName("notice")
        self.idea_notice.hide()
        layout.addWidget(self.idea_notice)

        cash_panel = QWidget()
        cash_panel.setObjectName("calcsCashPanel")
        cash_panel.setMinimumHeight(92)
        cash_layout = QGridLayout(cash_panel)
        cash_layout.setContentsMargins(12, 5, 12, 5)
        cash_layout.setHorizontalSpacing(18)
        title = QLabel("Cash allocation")
        title.setObjectName("calcsCashTitle")
        cash_layout.addWidget(title, 0, 0, 1, 3)
        for column, title in enumerate(("Current cash / weight", "Target weight · input", "Cash change")):
            label = QLabel(title)
            label.setObjectName("calcsFieldLabel")
            cash_layout.addWidget(label, 1, column)
        self.cash_value = QLabel("—")
        self.cash_editor = QDoubleSpinBox()
        self._configure_weight_editor(self.cash_editor, cash=True)
        self.cash_editor.editingFinished.connect(lambda: self._cash_changed(self.cash_editor.value()))
        self._watch_editor(self.cash_editor, None, 4)
        self.cash_gap = QLabel("—")
        for column, widget in enumerate((self.cash_value, self.cash_editor, self.cash_gap)):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            cash_layout.addWidget(widget, 2, column)
            cash_layout.setColumnStretch(column, 1)
        self.cash_editor.setFixedWidth(170)
        self.cash_editor.setFixedHeight(25)
        self.cash_editor.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        cash_layout.setAlignment(self.cash_editor, Qt.AlignLeft | Qt.AlignVCenter)
        for column in range(3):
            label = cash_layout.itemAtPosition(1, column).widget()
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(cash_panel)

        self.table = CalcsTable(0, len(self.HEADERS))
        self.table.setToolTip("Tinted cells are editable. Drag column headers to reorder them. Arrows move between cells; Enter saves and moves down; Esc cancels an edit. Select a full row with Shift+Space, then Ctrl+- to remove an idea or set a holding's target to 0%.")
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.verticalHeader().setDefaultSectionSize(25)
        self.remove_row_shortcut = QShortcut(QKeySequence("Ctrl+-"), self.table)
        self.remove_row_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.remove_row_shortcut.activated.connect(self._zero_or_remove_selected_row)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(65)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        for column, width in enumerate((155, 90, 110, 112, 145, 138, 150,
                                        118, 100, 180, 115, 110, 180, 115, 135, 80)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.table.setColumnWidth(column, width)
        saved_header = self.db.setting(f"calcs_header_v5_{portfolio_id}")
        if saved_header:
            header.restoreState(QByteArray.fromBase64(saved_header.encode("ascii")))
        else:
            legacy_header = self.db.setting(f"calcs_header_v4_{portfolio_id}")
            if legacy_header:
                self._restore_legacy_header(legacy_header)
        header.setSectionsMovable(True)
        for column, minimum in ((4, 145), (8, 100), (9, 180), (11, 110), (12, 180), (15, 80)):
            self.table.setColumnWidth(column, max(minimum, self.table.columnWidth(column)))
        header.sectionResized.connect(self._keep_editor_column_wide)
        layout.addWidget(self.table)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.totals = QLabel()
        self.totals.setWordWrap(True)
        layout.addWidget(self.totals)
        self.refresh()
        self._apply_view_mode()
        header.sortIndicatorChanged.connect(self._keep_idea_visible_after_sort)

    def _set_view_mode(self, mode):
        if mode not in ("allocation", "cases", "all"):
            return
        self.view_mode = mode
        self.db.set_setting(self._view_key, mode)
        self._apply_view_mode()

    @property
    def updates_paused(self):
        return self._frozen_snapshot is not None

    def toggle_pause(self):
        if self.updates_paused:
            self._frozen_snapshot = None
            self.pause_button.setText("Pause updates")
            self.pause_button.setToolTip("Freeze prices, weights and cash in Rebalance while the other tabs keep syncing")
        else:
            holdings = deepcopy([row for row in self.service.portfolio_rows()
                                 if abs(float(row.get("position_qty") or 0)) > 1e-9])
            self._reconcile_acquired_ideas(holdings)
            self._frozen_snapshot = {
                "holdings": holdings,
                "ideas": deepcopy(self._idea_rows()),
                "nav": self.db.effective_nav(self.portfolio_id),
                "cash_record": deepcopy(self.db.account_value(self.portfolio_id, "TotalCashValue")),
                "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.pause_button.setText("Resume updates")
            self.pause_button.setToolTip("Rebalance is frozen; click to use the latest prices, weights and cash")
        self.pause_button.setProperty("syncState", "paused" if self.updates_paused else "active")
        self.refresh()

    def _update_frozen_idea(self, ticker):
        if not self.updates_paused:
            return
        idea_id = f"idea:{ticker}"
        self._frozen_snapshot["ideas"] = [row for row in self._frozen_snapshot["ideas"]
                                          if row["id"] != idea_id]
        self._frozen_snapshot["ideas"].extend(row for row in self._idea_rows()
                                              if row["id"] == idea_id)

    def _apply_view_mode(self):
        visible = ({0, 1, 2, 3, 4, 5, 6, 15} if self.view_mode == "allocation" else
                   {0, 7, 8, 9, 10, 11, 12, 13, 14, 15} if self.view_mode == "cases" else
                   set(range(self.table.columnCount())))
        for column in range(self.table.columnCount()):
            self.table.setColumnHidden(column, column not in visible or column == 15 and not self.ideas)
        if self.table.currentRow() >= 0 and self.table.isColumnHidden(self.table.currentColumn()):
            first_visible = next(column for column in range(self.table.columnCount())
                                 if not self.table.isColumnHidden(column))
            self.table.select_cell(self.table.currentRow(), first_visible)

    def _load(self):
        try:
            saved = json.loads(self.db.setting(self._setting_key) or "{}")
        except (TypeError, ValueError):
            saved = {}
        self.mode = saved.get("mode") if saved.get("mode") in ("individual", "proportional") else "individual"
        targets = saved.get("targets") if isinstance(saved.get("targets"), dict) else {}
        self.targets = {key: rounded for key, value in targets.items()
                        if (rounded := self._rounded_weight(value)) is not None}
        self.cash_target = self._rounded_weight(saved.get("cash_target"), cash=True)

    @staticmethod
    def _rounded_weight(value, *, cash=False):
        try:
            fraction = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(fraction) or not (-9.9999 if cash else 0) <= fraction <= 9.9999:
            return None
        return round(fraction * 100, 2) / 100

    def _save(self):
        self.db.set_setting(self._setting_key, json.dumps({
            "mode": self.mode, "targets": self.targets, "cash_target": self.cash_target}))

    def _load_scenarios(self):
        try:
            saved = json.loads(self.db.setting(self._scenario_key) or "{}")
        except (TypeError, ValueError):
            saved = {}
        self.scenarios = saved if isinstance(saved, dict) else {}

    def _save_scenarios(self):
        self.db.set_setting(self._scenario_key, json.dumps(self.scenarios))

    def _load_ideas(self):
        try:
            saved = json.loads(self.db.setting(self._ideas_key) or "{}")
        except (TypeError, ValueError):
            saved = {}
        self.ideas = {}
        if isinstance(saved, dict):
            for ticker, details in saved.items():
                if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", ticker):
                    continue
                price = details.get("price_usd") if isinstance(details, dict) else None
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                normalized = {"price_usd": price if price is not None and math.isfinite(price) and price > 0 else None}
                if isinstance(details, dict):
                    con_id = details.get("con_id")
                    try:
                        con_id = int(con_id) if con_id else None
                    except (TypeError, ValueError):
                        con_id = None
                    if con_id and con_id > 0 and details.get("security_type") == "STK":
                        normalized.update({"con_id": con_id, "symbol": str(details.get("symbol") or ticker),
                                           "security_type": "STK", "currency": str(details.get("currency") or "USD"),
                                           "primary_exchange": str(details.get("primary_exchange") or ""),
                                           "description": str(details.get("description") or ""),
                                           "quote_as_of": str(details.get("quote_as_of") or ""),
                                           "quote_source": str(details.get("quote_source") or "")})
                self.ideas[ticker] = normalized

    def _save_ideas(self):
        self.db.set_setting(self._ideas_key, json.dumps(self.ideas))

    def _idea_rows(self):
        rows = []
        for ticker, details in sorted(self.ideas.items()):
            currency = details.get("currency", "USD")
            quote = self.market.quote(f"idea:{ticker}") if self.market else {}
            price, source = choose_price(quote.get("ticks", {}), quote.get("mode", "Live"))
            if price is None and valid_price(quote.get("cached_price")) is not None:
                price, source = quote["cached_price"], f"CACHED QUOTE {quote.get('cached_as_of', '')}"
            if price is None:
                price = details["price_usd"] if details.get("con_id") or not self.require_ibkr_ideas else None
                source = (f"CACHED QUOTE {details.get('quote_as_of', '')}" if details.get("con_id") and price else
                          "Manual USD/share scenario price" if price else "IBKR price unavailable" if details.get("con_id") else
                          "Unverified idea; select an IBKR contract to get prices")
            if price is None and quote.get("status"):
                source = quote["status"]
            elif price is not None and quote.get("updated"):
                source = f"{quote.get('mode', 'Live').upper()} · {source} · {quote['updated']}"
            fx = self.db.fx_rate(currency, date.today().isoformat())
            display = f"{ticker} · {'idea' if details.get('con_id') or not self.require_ibkr_ideas else 'unverified'}"
            rows.append({"id": f"idea:{ticker}", "display_ticker": display,
                         "ib_symbol": ticker, "position_qty": 0.0, "value": 0.0,
                         "unit_price_usd": price * fx if price is not None and fx is not None else None,
                         "last_px": price, "currency": currency, "security_type": "STK",
                         "hypothetical": True, "verified_idea": bool(details.get("con_id")),
                         "contract_tooltip": (f"IBKR #{details['con_id']} · {details.get('description', '')} · "
                                              f"{details.get('primary_exchange', '')} · {currency}\n{source}"
                                              if details.get("con_id") else source),
                         "price_source": source or quote.get("status") or "IBKR price unavailable"})
        return rows

    def set_market_provider(self, market, *, require_ibkr=True):
        self.market = market
        self.require_ibkr_ideas = require_ibkr
        self._search_request = None
        self._search_labels = {}
        self._suggestion_model.setStringList([])
        self._selected_contract = None
        if isinstance(market, IBKRMarketDataProvider):
            market.set_idea_contracts(self.ideas)
        self.refresh()

    def _idea_text_edited(self, text):
        self._selected_contract = None
        self._search_request = None
        self._search_labels = {}
        self._suggestion_model.setStringList([])
        if len(text.strip()) >= 2 and self.require_ibkr_ideas:
            self._search_timer.start(300)

    def _search_ideas(self):
        if not isinstance(self.market, IBKRMarketDataProvider) or not self.market.connected:
            self._show_idea_notice("Connect to IBKR to search stock contracts.")
            return
        try:
            self._search_request = self.market.search(self.idea_input.text().strip())
            self._show_idea_notice("Searching IBKR…")
        except RuntimeError as exc:
            self._show_idea_notice(str(exc))

    def process_search_results(self):
        if self.market is None or self._search_request is None:
            return
        error = self.market.search_errors.pop(self._search_request, None)
        if error:
            self._search_request = None
            self._show_idea_notice(f"IBKR search failed: {error}")
            return
        results = self.market.search_results.pop(self._search_request, None)
        if results is None:
            return
        self._search_request = None
        self._search_labels = {}
        for result in results:
            if result.get("security_type") != "STK" or not result.get("con_id"):
                continue
            symbol = str(result.get("symbol") or "")
            currency = str(result.get("currency") or "")
            exchange = str(result.get("primary_exchange") or "")
            description = str(result.get("description") or "")
            label = f"{symbol} · {description} · {exchange} · {currency} · #{result['con_id']}"
            self._search_labels[label] = result
        self._suggestion_model.setStringList(list(self._search_labels))
        if self._search_labels:
            self.idea_input.completer().complete()
            self.idea_notice.hide()
        else:
            self._show_idea_notice("No IBKR stock matches. Try another symbol or company name.")

    def _select_suggestion(self, label):
        self._selected_contract = self._search_labels.get(label)
        self.idea_input.setText(label)
        self.idea_notice.hide()

    def _show_idea_notice(self, notice):
        self.idea_notice.setText(notice)
        self.idea_notice.show()

    def _reconcile_acquired_ideas(self, holdings):
        acquired = {}
        for row in holdings:
            if row.get("security_type") != "STK":
                continue
            for symbol in (row.get("ib_symbol"), str(row.get("display_ticker") or "").split()[0]):
                if symbol:
                    acquired[str(symbol).upper()] = row["id"]
        changed = False
        for ticker in list(self.ideas):
            if ticker not in acquired:
                continue
            former_id, current_id = f"idea:{ticker}", str(acquired[ticker])
            if former_id in self.targets and current_id not in self.targets:
                self.targets[current_id] = self.targets[former_id]
            if former_id in self.scenarios and current_id not in self.scenarios:
                self.scenarios[current_id] = self.scenarios[former_id]
            self.targets.pop(former_id, None)
            self.scenarios.pop(former_id, None)
            del self.ideas[ticker]
            changed = True
        if changed:
            self._save_ideas()
            self._save()
            self._save_scenarios()

    def add_idea(self):
        selected = self._selected_contract if self.require_ibkr_ideas else None
        if self.require_ibkr_ideas:
            if not isinstance(self.market, IBKRMarketDataProvider) or not self.market.connected:
                self._show_idea_notice("Connect to IBKR to add a stock idea.")
                return
            if not selected or not any(label == self.idea_input.text() and result is selected
                                       for label, result in self._search_labels.items()):
                self._show_idea_notice("Choose a stock from the IBKR suggestions before adding it.")
                return
        ticker = (str(selected["symbol"]).upper() if selected else self.idea_input.text().strip().upper())
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", ticker):
            notice = "Enter a ticker using letters, numbers, dots or hyphens (up to 20 characters)."
        elif ticker in self.ideas and not (selected and not self.ideas[ticker].get("con_id")):
            notice = f"{ticker} is already an idea."
        elif any(ticker in {str(row.get("ib_symbol") or "").upper(),
                               str(row.get("display_ticker") or "").split()[0].upper()}
                 for row in self.service.portfolio_rows()
                 if row.get("security_type") == "STK" and
                 abs(float(row.get("position_qty") or 0)) > 1e-9):
            notice = f"{ticker} is already in current positions."
        else:
            self.ideas[ticker] = ({"price_usd": None, "con_id": int(selected["con_id"]),
                                   "symbol": ticker, "security_type": "STK",
                                   "currency": str(selected["currency"]),
                                   "primary_exchange": str(selected.get("primary_exchange") or ""),
                                   "description": str(selected.get("description") or ""),
                                   "quote_as_of": "", "quote_source": ""}
                                  if selected else {"price_usd": None})
            self._save_ideas()
            if isinstance(self.market, IBKRMarketDataProvider):
                self.market.set_idea_contracts(self.ideas)
            self._update_frozen_idea(ticker)
            self.idea_input.clear()
            self._selected_contract = None
            self.idea_notice.hide()
            self.refresh()
            self._keep_idea_visible_after_sort()
            return
        self._show_idea_notice(notice)

    def _remove_idea(self, ticker):
        self.ideas.pop(ticker, None)
        self._update_frozen_idea(ticker)
        if isinstance(self.market, IBKRMarketDataProvider):
            self.market.set_idea_contracts(self.ideas)
        self.targets.pop(f"idea:{ticker}", None)
        self.scenarios.pop(f"idea:{ticker}", None)
        self._save_ideas()
        self._save()
        self._save_scenarios()
        self.refresh()

    def _zero_or_remove_selected_row(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if len(selected_rows) != 1:
            return
        row = selected_rows[0].row()
        if any(index.row() != row for index in self.table.selectedIndexes()):
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        position_id = item.data(Qt.UserRole)
        if isinstance(position_id, str) and position_id.startswith("idea:"):
            ticker = position_id.removeprefix("idea:")
            if ticker not in self.ideas:
                return
            column = max(0, self.table.currentColumn())
            self._remove_idea(ticker)
            if self.table.rowCount():
                self.table.select_cell(min(row, self.table.rowCount() - 1), column, focus=True)
        elif position_id is not None:
            self._position_changed(position_id, 0)

    def _idea_price_changed(self, ticker, value):
        if self._loading or ticker not in self.ideas or self.ideas[ticker].get("con_id"):
            return
        self.ideas[ticker]["price_usd"] = value if value > 0 else None
        self._save_ideas()
        self.refresh()

    def cache_idea_quotes(self, ids):
        cache_recent = time.monotonic() - self._last_idea_quote_cache_at < 15
        changed = False
        for idea_id in ids:
            ticker = str(idea_id).removeprefix("idea:")
            details = self.ideas.get(ticker)
            if not details or not details.get("con_id") or self.market is None:
                continue
            # A newly selected idea needs its first quote even when another idea
            # was cached moments ago. Throttle only repeat saves of this idea.
            if cache_recent and details.get("quote_as_of"):
                continue
            quote = self.market.quote(idea_id)
            price, source = choose_price(quote.get("ticks", {}), quote.get("mode", "Live"))
            if valid_price(price) is None or not quote.get("updated"):
                continue
            details["price_usd"] = float(price)  # Stored in the contract's currency for broker ideas.
            details["quote_as_of"] = quote["updated"]
            details["quote_source"] = source
            changed = True
        if changed:
            self._save_ideas()
            self._last_idea_quote_cache_at = time.monotonic()

    def save_view(self):
        self.db.set_setting(f"calcs_header_v5_{self.portfolio_id}",
                            bytes(self.table.horizontalHeader().saveState().toBase64()).decode("ascii"))

    def _restore_legacy_header(self, encoded):
        previous_table = CalcsTable(0, 12)
        previous = previous_table.horizontalHeader()
        if not previous.restoreState(QByteArray.fromBase64(encoded.encode("ascii", "ignore"))):
            return
        expanded = {8: (8, 9, 10), 9: (11, 12, 13), 10: (14,), 11: (15,)}
        order = [new_column for old_column in sorted(range(12), key=previous.visualIndex)
                 for new_column in expanded.get(old_column, (old_column,))]
        current = self.table.horizontalHeader()
        for visual, logical in enumerate(order):
            current.moveSection(current.visualIndex(logical), visual)
        for old, new in [(column, column) for column in range(8)] + [(10, 14), (11, 15)]:
            if previous.sectionSize(old) > 0:
                self.table.setColumnWidth(new, previous.sectionSize(old))

    def view_state(self):
        header = self.table.horizontalHeader()
        return {
            "version": 2,
            "mode": self.view_mode,
            "header": bytes(header.saveState().toBase64()).decode("ascii"),
            "sort_column": header.sortIndicatorSection(),
            "sort_order": "desc" if header.sortIndicatorOrder() == Qt.DescendingOrder else "asc",
            "decimals": {str(column): self.decimals_for(column) for column in self.NUMERIC_COLUMNS},
        }

    def apply_view_state(self, state):
        if not isinstance(state, dict) or state.get("version") not in (1, 2):
            return False
        legacy = state["version"] == 1
        remap = {8: 10, 9: 13, 10: 14, 11: 15} if legacy else {}
        mode = state.get("mode")
        if mode in ("allocation", "cases", "all"):
            self.view_choice.setCurrentIndex(self.view_choice.findData(mode))
        decimals = state.get("decimals")
        if isinstance(decimals, dict):
            self.column_decimals = {}
            for key, places in decimals.items():
                if not str(key).isdigit():
                    continue
                column = remap.get(int(key), int(key))
                if (column in self.NUMERIC_COLUMNS and isinstance(places, int) and
                        not isinstance(places, bool) and 0 <= places <= 30 and
                        places != self.DEFAULT_DECIMALS[column]):
                    self.column_decimals[column] = places
            self.db.set_setting(f"calcs_decimals_v3_{self.portfolio_id}",
                                json.dumps(self.column_decimals))
            self.refresh()
        saved = state.get("header")
        if isinstance(saved, str) and not legacy:
            self.table.horizontalHeader().restoreState(
                QByteArray.fromBase64(saved.encode("ascii", "ignore")))
        column = state.get("sort_column")
        if isinstance(column, int) and not isinstance(column, bool):
            column = remap.get(column, column)
        if (isinstance(column, int) and not isinstance(column, bool) and
                0 <= column < self.table.columnCount()):
            self.table.sortItems(column, Qt.DescendingOrder if state.get("sort_order") == "desc"
                                 else Qt.AscendingOrder)
        return True

    def _load_decimals(self):
        try:
            current = self.db.setting(f"calcs_decimals_v3_{self.portfolio_id}")
            previous = self.db.setting(f"calcs_decimals_v2_{self.portfolio_id}")
            saved = json.loads(current or previous or self.db.setting(f"calcs_decimals_{self.portfolio_id}") or "{}")
        except (TypeError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        old_to_new = ({} if current else {8: 10, 9: 13, 10: 14} if previous else
                      {9: 10, 11: 13, 12: 14})
        return {old_to_new.get(int(column), int(column)): places for column, places in saved.items()
                if str(column).isdigit() and old_to_new.get(int(column), int(column)) in self.NUMERIC_COLUMNS and
                isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 30}

    def decimals_for(self, column):
        return self.column_decimals.get(column, self.DEFAULT_DECIMALS[column])

    def set_column_decimals(self, column, places):
        if column not in self.NUMERIC_COLUMNS or not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 30:
            raise ValueError("Choose 0–30 decimal places for a numeric column")
        if places == self.DEFAULT_DECIMALS[column]:
            self.column_decimals.pop(column, None)
        else:
            self.column_decimals[column] = places
        self.db.set_setting(f"calcs_decimals_v3_{self.portfolio_id}", json.dumps(self.column_decimals))
        self.refresh()

    def _build_header_menu(self, column):
        menu = QMenu(self)
        menu.addAction("Sort ascending", lambda: self.table.sortItems(column, Qt.AscendingOrder))
        menu.addAction("Sort descending", lambda: self.table.sortItems(column, Qt.DescendingOrder))
        if column in self.NUMERIC_COLUMNS:
            decimals_menu = QMenu("Decimal places", menu)
            menu.addMenu(decimals_menu)
            group = QActionGroup(decimals_menu)
            group.setExclusive(True)
            for places in range(4):
                action = decimals_menu.addAction(f"{places} decimal place{'s' if places != 1 else ''}")
                action.setCheckable(True)
                action.setChecked(self.decimals_for(column) == places)
                group.addAction(action)
                action.triggered.connect(lambda checked=False, number=column, count=places:
                                         self.set_column_decimals(number, count))
            custom = decimals_menu.addAction("Custom…")
            custom.setCheckable(True)
            custom.setChecked(self.decimals_for(column) > 3)
            group.addAction(custom)
            custom.triggered.connect(lambda checked=False, number=column:
                                     self._choose_column_decimals(number))
        menu.addSeparator()
        menu.addAction("Hide column", lambda: self.table.hideColumn(column))
        menu.addAction("Show all columns", lambda: self.table._show_all_columns())
        return menu

    def _choose_column_decimals(self, column):
        places, accepted = QInputDialog.getInt(self, "Custom decimal places",
                                                "Decimal places (0–30):", self.decimals_for(column), 0, 30)
        if accepted:
            self.set_column_decimals(column, places)

    def _header_menu(self, point):
        header = self.table.horizontalHeader()
        column = header.logicalIndexAt(point)
        if column >= 0:
            self._build_header_menu(column).exec(header.mapToGlobal(point))

    def reset_targets(self):
        self.mode, self.targets, self.cash_target = "individual", {}, None
        self._target_error = ""
        self._save()
        self.refresh()

    def _position_changed(self, position_id, value):
        if self._loading:
            return
        self.targets = {key: self._rounded_weight(weight) for key, weight in self._shown_targets.items()}
        self.mode = "individual"
        self.targets[str(position_id)] = self._rounded_weight(value / 100)
        self.cash_target = None
        self._target_error = ""
        self._save()
        self.refresh()

    def _cash_changed(self, value):
        if self._loading:
            return
        cash_target = self._rounded_weight(value / 100, cash=True)
        try:
            adjusted = scale_target_weights(self._shown_targets, cash_target)
        except ValueError as exc:
            self._target_error = str(exc)
            self.refresh()
            return
        self.targets = adjusted
        self.mode = "proportional"
        self.cash_target = cash_target
        self._target_error = ""
        self._save()
        self.refresh()

    def _scenario_for(self, position_id):
        saved = self.scenarios.get(str(position_id))
        saved = saved if isinstance(saved, dict) else {}
        result = {}
        for side in ("up", "down"):
            mode = saved.get(f"{side}_mode")
            result[f"{side}_mode"] = mode if mode in ("price", "percent") else "price"
            try:
                value = float(saved.get(f"{side}_value", 0))
            except (TypeError, ValueError):
                value = 0
            result[f"{side}_value"] = value if math.isfinite(value) and value > 0 else 0
        return result

    def _scenario_value_changed(self, position_id, side, value):
        if self._loading:
            return
        scenario = self._scenario_for(position_id)
        if value == scenario[f"{side}_value"]:
            return
        scenario[f"{side}_value"] = value
        self.scenarios[str(position_id)] = scenario
        self._save_scenarios()
        self.refresh()

    def _scenario_mode_changed(self, position_id, side, new_mode):
        if self._loading:
            return
        focused = QApplication.focusWidget()
        keep_focus = (self._scenario_controls.get(focused, ())[:2] ==
                      (position_id, 8 if side == "up" else 11))
        scenario = self._scenario_for(position_id)
        old_mode = scenario[f"{side}_mode"]
        value = scenario[f"{side}_value"]
        price = self._scenario_prices.get(position_id)
        if old_mode != new_mode and value > 0:
            if price is None:
                value = 0
            elif old_mode == "price":
                value = ((value / price - 1) if side == "up" else (1 - value / price)) * 100
            else:
                value = price * (1 + value / 100 if side == "up" else 1 - value / 100)
            value = value if math.isfinite(value) and value > 0 else 0
        scenario[f"{side}_mode"] = new_mode
        scenario[f"{side}_value"] = value
        self.scenarios[str(position_id)] = scenario
        self._save_scenarios()
        self.refresh()
        if keep_focus:
            column = 8 if side == "up" else 11
            row = self._row_for_position(position_id)
            if row >= 0:
                self.table.select_cell(row, column)
                self.table.scrollToItem(self.table.item(row, column))
                self.table.cellWidget(row, column).setFocus()

    def _cell(self, row, column, value, *, tip="", sort_value=None, position_id=None):
        item = SortItem(value, sort_value)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if column:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if tip:
            item.setToolTip(tip)
        if position_id is not None:
            item.setData(Qt.UserRole, position_id)
        self.table.setItem(row, column, item)

    def _watch_editor(self, editor, position_id, column):
        if position_id is not None:
            commit = (lambda input_widget=editor, pid=position_id, col=column:
                      self._commit_table_editor(input_widget, pid, col)) if isinstance(editor, QDoubleSpinBox) else None
            self.table.register_input(editor, position_id, column, commit)
            return
        baseline = editor.value() if isinstance(editor, QDoubleSpinBox) else None
        self._keyboard_controls[editor] = (editor, position_id, column, baseline)
        editor.installEventFilter(self)
        if isinstance(editor, QDoubleSpinBox):
            line_edit = editor.lineEdit()
            self._keyboard_controls[line_edit] = (editor, position_id, column, baseline)
            line_edit.installEventFilter(self)

    def _commit_table_editor(self, editor, position_id, column):
        value = editor.value()
        if column == 4:
            self._position_changed(position_id, value)
        elif column == 7:
            self._idea_price_changed(str(position_id).removeprefix("idea:"), value)
        elif column in (9, 12):
            self._scenario_value_changed(position_id, "up" if column == 9 else "down", value)

    def _row_for_position(self, position_id):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(Qt.UserRole) == position_id:
                return row
        return -1

    def _keep_editor_column_wide(self, column, _old, width):
        minimum = {4: 145, 8: 100, 9: 180, 11: 110, 12: 180, 15: 80}.get(column)
        if minimum and width < minimum:
            self.table.setColumnWidth(column, minimum)

    def _keep_idea_visible_after_sort(self, *_):
        if self._loading or not self.ideas:
            return
        current = self.table.item(self.table.currentRow(), 0)
        selected_id = current.data(Qt.UserRole) if current else None
        idea_id = (selected_id if selected_id in {f"idea:{ticker}" for ticker in self.ideas}
                   else f"idea:{next(reversed(self.ideas))}")
        row = self._row_for_position(idea_id)
        item = self.table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        rect = self.table.visualItemRect(item)
        if rect.top() < 0 or rect.bottom() > self.table.viewport().height():
            horizontal = self.table.horizontalScrollBar().value()
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self.table.horizontalScrollBar().setValue(horizontal)

    def _select_without_entering_editor(self, row, column):
        self.table.select_cell(row, column)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and watched in self._scenario_controls:
            if self._navigate_scenario(watched, event):
                return True
        if event.type() != QEvent.KeyPress or watched not in self._keyboard_controls:
            return super().eventFilter(watched, event)
        key = event.key()
        is_enter = key in (Qt.Key_Return, Qt.Key_Enter)
        if (key not in (*CalcsTable.DIRECTIONS, Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter) or
            event.modifiers() not in ((Qt.NoModifier, Qt.KeypadModifier) if is_enter else (Qt.NoModifier,))):
            return super().eventFilter(watched, event)
        editor, position_id, column, baseline = self._keyboard_controls[watched]
        if key == Qt.Key_Escape:
            row = self._row_for_position(position_id) if position_id is not None else -1
            if isinstance(editor, QDoubleSpinBox):
                editor.blockSignals(True)
                editor.setValue(baseline)
            self.table.setFocus()
            if isinstance(editor, QDoubleSpinBox):
                editor.blockSignals(False)
            if row >= 0:
                self._select_without_entering_editor(row, column)
            event.accept()
            return True
        if isinstance(editor, QDoubleSpinBox):
            editor.blockSignals(True)
            editor.interpretText()
            value = editor.value()
            self.table.setFocus()
            editor.blockSignals(False)
            self._cash_changed(value)
        self.table.setFocus()
        if position_id is None:
            if (key == Qt.Key_Down or is_enter) and self.table.rowCount():
                self.table.select_cell(0, 4, focus=True)
        event.accept()
        return True

    def _navigate_scenario(self, watched, event):
        if event.modifiers() != Qt.NoModifier or event.key() not in CalcsTable.DIRECTIONS:
            return False
        position_id, column, editor = self._scenario_controls[watched]
        row = self._row_for_position(position_id)
        key = event.key()
        if key in (Qt.Key_Up, Qt.Key_Down):
            target_row = row + (-1 if key == Qt.Key_Up else 1)
            target_column = column
        else:
            header = self.table.horizontalHeader()
            visual = header.visualIndex(column) + (-1 if key == Qt.Key_Left else 1)
            while 0 <= visual < header.count() and self.table.isColumnHidden(header.logicalIndex(visual)):
                visual += -1 if key == Qt.Key_Left else 1
            if not 0 <= visual < header.count():
                return False
            target_row, target_column = row, header.logicalIndex(visual)
        if not 0 <= target_row < self.table.rowCount() or self.table.isColumnHidden(target_column):
            return False
        target = self.table.cellWidget(target_row, target_column)
        if not isinstance(target, (QComboBox, QDoubleSpinBox)):
            return False
        target_id = self.table.item(target_row, 0).data(Qt.UserRole)
        if isinstance(editor, QDoubleSpinBox):
            editor.blockSignals(True)
            editor.interpretText()
            value = editor.value()
            self.table.setFocus()
            editor.blockSignals(False)
            side = "up" if column == 9 else "down"
            if value != self._scenario_for(position_id)[f"{side}_value"]:
                self._scenario_value_changed(position_id, side, value)
        target_row = self._row_for_position(target_id)
        target = self.table.cellWidget(target_row, target_column)
        self.table.select_cell(target_row, target_column)
        self.table.scrollToItem(self.table.item(target_row, target_column))
        target.setFocus()
        if isinstance(target, QDoubleSpinBox):
            target.selectAll()
        event.accept()
        return True

    def _number(self, value, column, suffix=""):
        return "—" if value is None else f"{value:,.{self.decimals_for(column)}f}{suffix}"

    @staticmethod
    def _configure_weight_editor(editor, *, cash=False):
        editor.setObjectName("calcsTargetInput")
        editor.setButtonSymbols(QDoubleSpinBox.NoButtons)
        editor.setRange(-999.99 if cash else 0, 999.99)
        editor.setDecimals(2)
        editor.setSingleStep(0.5)
        editor.setSuffix(" %")

    def _weight_editor(self, row, value, callback):
        editor = QDoubleSpinBox()
        self._configure_weight_editor(editor)
        editor.setValue(100 * value)
        editor.editingFinished.connect(lambda: callback(editor.value()))
        self._cell(row, 4, "", sort_value=value)
        self.table.setCellWidget(row, 4, editor)
        self._watch_editor(editor, self.table.item(row, 0).data(Qt.UserRole), 4)

    def _idea_price_editor(self, row, ticker, price):
        editor = QDoubleSpinBox()
        editor.setObjectName("calcsTargetInput")
        editor.setButtonSymbols(QDoubleSpinBox.NoButtons)
        editor.setRange(0, 1_000_000_000)
        editor.setDecimals(4)
        editor.setSuffix(" USD")
        editor.setSpecialValueText("Enter price")
        editor.setValue(price or 0)
        self.table.setColumnWidth(7, max(self.table.columnWidth(7), editor.sizeHint().width() + 8))
        editor.setToolTip("Manual USD price per share for this portfolio idea; used only for estimated shares and scenario prices")
        self._cell(row, 7, "", sort_value=price)
        self.table.setCellWidget(row, 7, editor)
        self._watch_editor(editor, f"idea:{ticker}", 7)
        editor.editingFinished.connect(lambda symbol=ticker, spin=editor:
                                       self._idea_price_changed(symbol, spin.value()))

    def _scenario_editor(self, row, position_id, side, scenario, metrics, currency):
        mode_column, value_column, result_column = ((8, 9, 10) if side == "up" else (11, 12, 13))
        mode = scenario[f"{side}_mode"]
        value = scenario[f"{side}_value"]
        choice = QComboBox()
        choice.setObjectName("calcsTargetInput")
        choice.addItem("Price", "price")
        choice.addItem("%", "percent")
        choice.setCurrentIndex(0 if mode == "price" else 1)
        choice.setFixedHeight(23)
        editor = QDoubleSpinBox()
        editor.setObjectName("calcsTargetInput")
        editor.setButtonSymbols(QDoubleSpinBox.NoButtons)
        editor.setRange(0, 1_000_000_000)
        editor.setDecimals(4)
        editor.setSingleStep(1 if mode == "percent" else 0.5)
        editor.setSuffix(" %" if mode == "percent" else "")
        editor.setSpecialValueText("Enter")
        editor.setValue(value)
        editor.setFixedHeight(23)
        editor.setToolTip("Enter a positive loss percentage" if side == "down" and mode == "percent" else
                          f"Price in {currency}" if mode == "price" else "Enter a positive gain percentage")
        percent = metrics["upside_pct" if side == "up" else "downside_pct"]
        implied_price = metrics["upside_price" if side == "up" else "downside_price"]
        if mode == "percent":
            result_text = (self._number(implied_price, result_column, f" {currency}")
                           if implied_price is not None else "Price —")
            result_tip = (f"Implied {side} price from the current mark" if implied_price is not None else
                          "A current price is needed to show an implied price")
        else:
            result_text = self._number(percent * 100 if percent is not None else None, result_column, " %")
            result_tip = (metrics[f"{('upside' if side == 'up' else 'downside')}_reason"] or
                          f"Implied {side} percentage from the current mark")
        self._cell(row, mode_column, "", sort_value=mode)
        self.table.setCellWidget(row, mode_column, choice)
        self._cell(row, value_column, "", sort_value=value)
        self.table.setCellWidget(row, value_column, editor)
        self._cell(row, result_column, result_text, tip=result_tip, sort_value=percent)
        self._watch_editor(choice, position_id, mode_column)
        self._watch_editor(editor, position_id, value_column)
        self._scenario_controls[choice] = (position_id, mode_column, choice)
        self._scenario_controls[editor] = (position_id, value_column, editor)
        self._scenario_controls[editor.lineEdit()] = (position_id, value_column, editor)
        choice.installEventFilter(self)
        editor.installEventFilter(self)
        editor.lineEdit().installEventFilter(self)
        choice.currentIndexChanged.connect(
            lambda _, pid=position_id, which=side, box=choice:
            self._scenario_mode_changed(pid, which, box.currentData()))
        editor.editingFinished.connect(
            lambda pid=position_id, which=side, spin=editor:
            self._scenario_value_changed(pid, which, spin.value()))

    def refresh(self, *_):
        current_row = self.table.currentRow()
        current_item = self.table.item(current_row, 0) if current_row >= 0 else None
        selected_id = current_item.data(Qt.UserRole) if current_item else None
        selected_column = self.table.currentColumn()
        horizontal_scroll = self.table.horizontalScrollBar().value()
        vertical_scroll = self.table.verticalScrollBar().value()
        focus = QApplication.focusWidget()
        editor_focused = focus is not None and (self.table.isAncestorOf(focus) or
                                                focus is self.cash_editor or self.cash_editor.isAncestorOf(focus))
        self._loading = True
        try:
            if editor_focused:
                self.table.setFocus()
            self.table.setUpdatesEnabled(False)
            self._keyboard_controls = {}
            self._scenario_controls = {}
            self.table.clear_input_bindings()
            header = self.table.horizontalHeader()
            sort_column = header.sortIndicatorSection() if self.table.isSortingEnabled() else 0
            sort_order = header.sortIndicatorOrder() if self.table.isSortingEnabled() else Qt.AscendingOrder
            self.table.setSortingEnabled(False)
            snapshot = self._frozen_snapshot
            rows = (deepcopy(snapshot["holdings"]) if snapshot else
                    [row for row in self.service.portfolio_rows()
                     if abs(float(row.get("position_qty") or 0)) > 1e-9])
            self._reconcile_acquired_ideas(rows)
            rows.extend(deepcopy(snapshot["ideas"]) if snapshot else self._idea_rows())
            fit_option_name_column(self.table, 0, (row.get("display_ticker") for row in rows),
                                   self.db, f"option_name_width_Rebalance_{self.portfolio_id}")
            nav = snapshot["nav"] if snapshot else self.db.effective_nav(self.portfolio_id)
            cash_record = snapshot["cash_record"] if snapshot else self.db.account_value(self.portfolio_id, "TotalCashValue")
            if cash_record and cash_record["currency"] == "USD":
                cash = cash_record["value"]
                cash_note = f"IBKR TotalCashValue as of {cash_record['as_of']}"
            elif nav is not None and all(row.get("value") is not None for row in rows):
                cash = nav - sum(row["value"] for row in rows)
                cash_note = "Inferred from NAV less valued holdings; broker cash is unavailable"
            else:
                cash = None
                cash_note = "Cash is unavailable"
            calc = calculate_targets(rows, nav, cash, mode=self.mode,
                                     targets=self.targets, cash_target=self.cash_target)
            calculated = {row["id"]: row for row in calc.get("rows", ())}
            self._shown_targets = {}
            self._scenario_prices = {}
            # Sorting moves cell widgets with their rows. Discard the old widgets
            # before rebuilding so ideas cannot leave buttons or price editors on holdings.
            self.table.clearContents()
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                result = calculated.get(row["id"])
                weight = row["value"] / nav if row.get("value") is not None and nav and nav > 0 else None
                target = (result["target_weight"] if result else
                          self.targets.get(str(row["id"]), weight))
                if not isinstance(target, (int, float)) or not math.isfinite(target):
                    target = 0
                self._shown_targets[str(row["id"])] = target
                snapshot_note = f"Paused Rebalance snapshot · {snapshot['as_of']}\n" if snapshot else ""
                self._cell(index, 0, row["display_ticker"],
                           tip=snapshot_note + "\n".join(str(part) for part in
                               (row["display_ticker"], row.get("raw_display_ticker"),
                                row.get("contract_tooltip") or row.get("price_source")) if part),
                           position_id=row["id"])
                self._cell(index, 1, self._number(row["position_qty"], 1), sort_value=row["position_qty"])
                self._cell(index, 2, self._number(row.get("value"), 2), sort_value=row.get("value"))
                self._cell(index, 3, self._number(weight * 100 if weight is not None else None, 3, " %"), sort_value=weight)
                self._weight_editor(index, max(0, target),
                                    lambda value, position_id=row["id"]: self._position_changed(position_id, value))
                gap = result["gap_usd"] if result else None
                self._cell(index, 5, "—" if gap is None or abs(gap) < 0.005 else
                           ("Buy " if gap >= 0 else "Sell ") + "$" + self._number(abs(gap), 5), sort_value=gap)
                units = result["gap_quantity"] if result else None
                kind = "contracts" if row.get("security_type") == "OPT" else "shares" if row.get("security_type") == "STK" else "units"
                self._cell(index, 6, "—" if units is None or gap is None or abs(gap) < 0.005 else
                           ("Buy " if units >= 0 else "Sell ") + self._number(abs(units), 6, f" {kind}"),
                           tip="Estimate from current USD value per unit; before fees, spreads or price changes",
                           sort_value=units)
                try:
                    current_price = float(row.get("last_px"))
                except (TypeError, ValueError):
                    current_price = None
                if current_price is not None and (not math.isfinite(current_price) or current_price <= 0):
                    current_price = None
                self._scenario_prices[row["id"]] = current_price
                currency = row.get("currency") or "local currency"
                if row.get("hypothetical") and not row.get("verified_idea") and not self.require_ibkr_ideas:
                    self._idea_price_editor(index, row["ib_symbol"], current_price)
                else:
                    self._cell(index, 7, self._number(current_price, 7, f" {currency}"),
                               tip=snapshot_note + str(row.get("price_source") or "Current price unavailable"),
                               sort_value=current_price)
                scenario = self._scenario_for(row["id"])
                metrics = scenario_metrics(current_price, scenario["up_mode"], scenario["up_value"],
                                           scenario["down_mode"], scenario["down_value"])
                self._scenario_editor(index, row["id"], "up", scenario, metrics, currency)
                self._scenario_editor(index, row["id"], "down", scenario, metrics, currency)
                ratio = metrics["risk_reward"]
                reason = metrics["upside_reason"] or metrics["downside_reason"]
                self._cell(index, 14, self._number(ratio, 14),
                           tip=reason or "Upside gain % divided by downside loss %",
                           sort_value=ratio)
                if row.get("hypothetical"):
                    button = QPushButton("Remove")
                    button.setObjectName("secondary")
                    button.setProperty("rebalanceRemove", True)
                    button.setToolTip(f"Remove {row['ib_symbol']} from portfolio ideas")
                    button.clicked.connect(lambda _, symbol=row["ib_symbol"]: self._remove_idea(symbol))
                    self._cell(index, 15, "", sort_value=1)
                    self.table.setCellWidget(index, 15, button)
                else:
                    self._cell(index, 15, "", sort_value=0)
            self.table.setColumnHidden(15, not bool(self.ideas))
            self.table.setSortingEnabled(True)
            self.table.sortItems(sort_column, sort_order)
            if selected_id is not None:
                selected_row = self._row_for_position(selected_id)
                if selected_row >= 0 and 0 <= selected_column < self.table.columnCount():
                    self.table.select_cell(selected_row, selected_column)
            self.table.horizontalScrollBar().setValue(horizontal_scroll)
            self.table.verticalScrollBar().setValue(vertical_scroll)
            cash_weight = cash / nav if cash is not None and nav and nav > 0 else None
            self.cash_value.setText("—" if cash is None else
                                    f"${cash:,.2f}  ·  " + (_text(cash_weight * 100, 4) + "%" if cash_weight is not None else "weight unavailable"))
            self.cash_value.setToolTip((f"Paused Rebalance snapshot · {snapshot['as_of']}\n" if snapshot else "") + cash_note)
            cash_target = calc.get("cash_target")
            if cash_target is None:
                cash_target = self.cash_target if self.mode == "proportional" else cash_weight
            if not isinstance(cash_target, (int, float)) or not math.isfinite(cash_target):
                cash_target = 0
            self.cash_editor.setValue(100 * cash_target)
            self._watch_editor(self.cash_editor, None, 4)
            cash_gap = calc.get("cash_gap")
            self.cash_gap.setText("—" if cash_gap is None else
                                  ("Increase " if cash_gap >= 0 else "Decrease ") + f"${abs(cash_gap):,.2f}")
            self.mode_label.setText(("Paused · " if snapshot else "") +
                                    ("Cash target scales position weights" if self.mode == "proportional"
                                     else "Individual targets") +
                                    (f"  ·  NAV ${nav:,.2f}" if nav is not None else ""))
            if calc["error"]:
                self.status.setText(self._target_error or calc["error"])
                self.totals.setText("Buy / sell estimates unavailable until the inputs reconcile.")
            else:
                notes = [self._target_error] if self._target_error else []
                if abs(calc["difference"]) >= 0.01:
                    notes.append(f"NAV less holdings and cash: ${calc['difference']:,.2f}.")
                if any(row.get("hypothetical") and row.get("unit_price_usd") is None and
                       (calculated.get(row["id"]) or {}).get("gap_usd", 0) > 0.005 for row in rows):
                    notes.append("Shares need an IBKR price and a recent FX rate where applicable.")
                if self.require_ibkr_ideas and any(row.get("hypothetical") and not row.get("verified_idea") for row in rows):
                    notes.append("Existing unverified ideas need an IBKR contract selection.")
                self.status.setText(" ".join(notes))
                self.totals.setText(f"Buys ${calc['buy_usd']:,.2f}  ·  Sells ${calc['sell_usd']:,.2f}  ·  "
                                    f"Invested target {(1 - calc['cash_target']) * 100:,.2f}%")
            self.status.setVisible(bool(self.status.text()))
        finally:
            self.table.setUpdatesEnabled(True)
            self._loading = False
