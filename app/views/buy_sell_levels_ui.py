"""Daily stock closes with execution prices for the selected position."""

from datetime import date, timedelta
import math

from PySide6.QtCharts import QDateTimeAxis, QLineSeries, QScatterSeries, QValueAxis
from PySide6.QtCore import QByteArray, QDate, QDateTime, QLocale, QTime, Qt
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (QComboBox, QToolTip,
                               QVBoxLayout, QWidget, QSizePolicy)

from app.finance.splits import adjusted_execution_price, ledger_split_events
from app.finance.services import PortfolioService
from app.market.split_history import SplitHistoryClient, combine_split_history
from app.market.market_data import MarketDataProvider, five_year_start
from app.views import widgets
from app.views.chart_ui import ChartNavigation, NavigableChartView, base_chart, finish_chart, price_scale
from app.views.chart_colors import ChartColorControl
from app.views.toolbar import Toolbar
from app.views.table_ui import ReportTable, SortItem
from app.views.ui_theme import COLORS, style_toolbar_dropdown


TABLE_NUMBER_FORMATS = {2: (4, "plain"), 3: (2, "plain"),
                        5: (2, "usd"), 6: (2, "ratio_percent"),
                        7: (2, "usd"), 8: (2, "ratio_percent")}


def _stamp(day):
    parsed = date.fromisoformat(day)
    return QDateTime(QDate(parsed.year, parsed.month, parsed.day), QTime(12, 0)).toMSecsSinceEpoch()


def _valid_close(row):
    try:
        close = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return close if math.isfinite(close) and close > 0 else None


def _first_valid_price_day(prices):
    return next((row["day"] for row in prices if _valid_close(row) is not None), None)


def _trade_notional(row):
    try:
        notional = abs(float(row["gross_lc"]))
    except (KeyError, TypeError, ValueError):
        return 0.0
    return notional if math.isfinite(notional) else 0.0


def _marker_size(notional, largest_notional):
    """Scale marker area with original trade notional, within readable bounds."""
    if largest_notional <= 0:
        return 8.0
    return max(8.0, 18.0 * math.sqrt(notional / largest_notional))


def _price_axis(values, title, selected_decimals):
    min_step = 10 ** -selected_decimals if selected_decimals is not None else 0
    low, high, step, auto_decimals = price_scale(values, min_step=min_step)
    axis = QValueAxis()
    axis.setTitleText(title)
    axis.setRange(low, high)
    axis.setTickType(QValueAxis.TicksDynamic)
    axis.setTickAnchor(0)
    axis.setTickInterval(step)
    axis.setProperty("nonnegativePriceAxis", True)
    axis.setProperty("priceMinStep", min_step)
    axis.setProperty("priceAutoDecimals", selected_decimals is None)
    axis.setLabelFormat(f"%.{auto_decimals if selected_decimals is None else selected_decimals}f")
    return axis


class BuySellLevelsPanel(QWidget):
    """Chart a stock position's cached closes and its recorded executions."""

    def __init__(self, db, portfolio_id, parent=None, request_price_history=None,
                 portfolio_rows_provider=None, trade_rows_provider=None, split_history=None):
        super().__init__(parent)
        self.db = db
        self.portfolio_id = portfolio_id
        self.theme = "light"
        self._positions = {}
        self._trades = []
        self._request_price_history = request_price_history
        self._portfolio_rows_provider = portfolio_rows_provider
        self._trade_rows_provider = (trade_rows_provider or
                                     PortfolioService(db, MarketDataProvider(), portfolio_id).trade_rows)
        self._history_message = None
        self._history_exhausted = set()
        self._split_history = split_history or SplitHistoryClient(db, self)

        layout = QVBoxLayout(self)
        widgets.apply_layout(layout, "subpage")
        layout.setSpacing(8)
        self.toolbar = controls = Toolbar()
        self.stock_combo = style_toolbar_dropdown(QComboBox())
        self.stock_combo.setMinimumWidth(220)
        self.stock_combo.currentIndexChanged.connect(self._stock_changed)
        controls.add_control(self.stock_combo, "Stock")
        self.load_button = widgets.button("Load older prices", self._load_older_prices)
        controls.add_control(self.load_button)
        self.axis_decimals_combo = style_toolbar_dropdown(QComboBox())
        self.axis_decimals_combo.addItem("Auto", None)
        for places in range(9):
            self.axis_decimals_combo.addItem(str(places), places)
        saved_decimals = db.setting(f"analytics_levels_axis_decimals_{portfolio_id}")
        if saved_decimals.isdigit() and 0 <= int(saved_decimals) <= 8:
            self.axis_decimals_combo.setCurrentIndex(int(saved_decimals) + 1)
        self.axis_decimals_combo.currentIndexChanged.connect(self._axis_decimals_changed)
        controls.add_control(self.axis_decimals_combo, "Axis decimals")
        self.chart_colors = ChartColorControl(
            db, portfolio_id, (("price", "Daily close line", "accent"),), self._render)
        controls.add_control(self.chart_colors)
        self.note = widgets.hint("")
        self.chart_view = NavigableChartView()
        self.chart_view.setRenderHint(QPainter.Antialiasing)
        self.chart_view.setMinimumHeight(300)
        self.chart_view.setMinimumWidth(0)
        self.navigation = ChartNavigation(self.chart_view)
        self.zoom_in_button = self.navigation.zoom_in
        self.zoom_out_button = self.navigation.zoom_out
        self.zoom_reset_button = self.navigation.reset
        controls.add_control(self.navigation)
        self.layout_combo = widgets.layout_choice("Buy / sell chart and table")
        controls.add_control(self.layout_combo, "Layout", trailing=True)
        layout.addWidget(controls)
        layout.addWidget(self.note)

        self.table = ReportTable(0, 9)
        self.table.setHorizontalHeaderLabels((
            "Date", "Side", "Quantity", "Execution price", "Currency",
            "Realized P&L USD", "Realized P&L %", "Open P&L USD", "Open P&L %"))
        self.table.configure_numbers(db, f"analytics_levels_decimals_{portfolio_id}",
                                     TABLE_NUMBER_FORMATS)
        self.table.fit_header_widths()
        self.table.remember_layout(db, f"analytics_levels_table_layout_{portfolio_id}")
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.DescendingOrder)
        self.table.setMinimumHeight(140)
        self.table.setMinimumWidth(0)
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.splitter, self.layout_combo = widgets.chart_table_layout(
            layout, db, f"analytics_levels_splitter_{portfolio_id}",
            self.chart_view, self.table, display_combo=self.layout_combo)

    def view_state(self):
        return {
            "orientation": "horizontal" if self.splitter.orientation() == Qt.Horizontal else "vertical",
            "splitter": bytes(self.splitter.saveState().toBase64()).decode("ascii"),
            "axis_decimals": self.axis_decimals_combo.currentData(),
            "table": self.table.view_state(),
        }

    def apply_view_state(self, state):
        if not isinstance(state, dict):
            return
        if state.get("orientation") in ("horizontal", "vertical"):
            self.layout_combo.setCurrentIndex(1 if state["orientation"] == "horizontal" else 0)
        if "axis_decimals" in state:
            places = state["axis_decimals"]
            if places is None or type(places) is int and 0 <= places <= 8:
                self.axis_decimals_combo.setCurrentIndex(self.axis_decimals_combo.findData(places))
        saved = state.get("splitter")
        if isinstance(saved, str) and self.splitter.restoreState(
                QByteArray.fromBase64(saved.encode("ascii", "ignore"))):
            widgets.save_chart_table_split_state(
                self.db, self.splitter, f"analytics_levels_splitter_{self.portfolio_id}")
        self.table.apply_view_state(state.get("table"))

    def set_theme(self, theme):
        self.theme = theme
        self.chart_colors.set_theme(theme)
        self.navigation.set_theme(theme)
        self._render()

    def _stock_changed(self, *_):
        self._history_message = None
        self._render()

    def _axis_decimals_changed(self, *_):
        places = self.axis_decimals_combo.currentData()
        self.db.set_setting(f"analytics_levels_axis_decimals_{self.portfolio_id}",
                            "auto" if places is None else places)
        self._render()

    def set_history_message(self, con_id, message):
        position = self._positions.get(self.stock_combo.currentData())
        if position and position.get("con_id") == con_id:
            self._history_message = message
            self._render()

    def set_history_complete(self, con_id, reached_five_years):
        if not reached_five_years:
            self._history_exhausted.add(con_id)
        self.set_history_message(
            con_id, "Five years of prices loaded." if reached_five_years else None)

    def _load_older_prices(self):
        position = self._positions.get(self.stock_combo.currentData())
        if not position or self._request_price_history is None:
            return
        prices = self.db.history(int(position["con_id"]))
        first_saved_day = _first_valid_price_day(
            [row for row in prices if row["day"] >= five_year_start().isoformat()])
        try:
            started = self._request_price_history(position, first_saved_day)
            self._history_message = ("Requesting older daily prices from IBKR…" if started else
                                     "A price history request is already running for this stock.")
        except Exception as exc:
            self._history_message = str(exc)
        self._render()

    def refresh(self, portfolio_rows=None, trade_rows=None):
        if portfolio_rows is None:
            portfolio_rows = (self._portfolio_rows_provider() if self._portfolio_rows_provider
                              else self.db.rows("positions", self.portfolio_id))
        positions = [row for row in portfolio_rows
                     if row["security_type"] == "STK"]
        positions.sort(key=lambda row: (not bool(row["position_qty"]),
                                        row["display_ticker"].casefold(), row["id"]))
        self._positions = {row["id"]: row for row in positions}
        self._trades = [row for row in (trade_rows if trade_rows is not None else
                                      self._trade_rows_provider())
                        if row["security_type"] == "STK" and row["position_id"] in self._positions]
        selected = self.stock_combo.currentData()
        self.stock_combo.blockSignals(True)
        self.stock_combo.clear()
        for row in positions:
            label = row["display_ticker"] or row["ib_symbol"] or f"Stock {row['id']}"
            if not row["position_qty"]:
                label += " (closed)"
            self.stock_combo.addItem(label, row["id"])
        index = self.stock_combo.findData(selected)
        self.stock_combo.setCurrentIndex(max(0, index) if positions else -1)
        self.stock_combo.blockSignals(False)
        self._render()

    def _render(self, *_):
        colors = COLORS[self.theme]
        chart = base_chart(self.theme, show_legend=True)
        position = self._positions.get(self.stock_combo.currentData())
        for button in (self.zoom_in_button, self.zoom_out_button, self.zoom_reset_button):
            button.setEnabled(False)
        if position is None:
            self.note.setText("No stock positions are available.")
            self.load_button.setEnabled(False)
            self.table.setRowCount(0)
            finish_chart(self.chart_view, chart, self.theme, "No stock positions")
            return

        trades = sorted((row for row in self._trades if row["position_id"] == position["id"]),
                        key=lambda row: (row["trade_date"], row["executed_at"], row["id"]))
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(trades))
        for index, row in enumerate(reversed(trades)):
            for column, value in enumerate((row["trade_date"], row["side"],
                                            row["quantity"], row["price"], row["currency"],
                                            row.get("realized_net"), row.get("realized_pct"),
                                            row.get("net_unrealized"), row.get("unrealized_pct"))):
                item = (self.table.numeric_item(column, value)
                        if column in TABLE_NUMBER_FORMATS else SortItem(str(value), str(value)))
                if column in (5, 6, 7, 8) and isinstance(value, (int, float)) and math.isfinite(value):
                    tone = "positive" if value > 0 else "negative" if value < 0 else "grid_neutral_text"
                    item.setForeground(QColor(colors[tone]))
                self.table.setItem(index, column, item)
        if sorting:
            self.table.setSortingEnabled(True)

        con_id = position.get("con_id")
        prices = self.db.history(int(con_id)) if con_id else []
        chart_start = five_year_start().isoformat()
        points = []
        for row in prices:
            try:
                close = _valid_close(row)
                if close is not None and row["day"] >= chart_start:
                    points.append((_stamp(row["day"]), close))
            except (TypeError, ValueError):
                continue
        executions = []
        for row in trades:
            try:
                price = float(row["price"])
                if math.isfinite(price) and price > 0 and row["trade_date"] >= chart_start:
                    executions.append((row, _stamp(row["trade_date"]), price))
            except (TypeError, ValueError):
                continue
        first_price_day = _first_valid_price_day(
            [row for row in prices if row["day"] >= chart_start])
        first_trade_day = min((row["trade_date"] for row in trades if row.get("trade_date")),
                              default=None)
        needs_history = not first_price_day or first_price_day > (
            five_year_start() + timedelta(days=7)).isoformat()
        self.load_button.setEnabled(bool(self._request_price_history and con_id and
                                         con_id not in self._history_exhausted and needs_history))

        chart_executions = []
        split_state = "ready"
        if points and executions:
            public_events, split_state = self._split_history.events(
                position, first_trade_day, self._render)
            if public_events is not None:
                try:
                    split_events = combine_split_history(ledger_split_events(trades), public_events)
                    for row, stamp, raw_price in executions:
                        adjusted_price, _factor = adjusted_execution_price(
                            raw_price, row["trade_date"], split_events)
                        chart_executions.append((row, stamp, adjusted_price))
                except ValueError:
                    split_state = "unavailable"

        note = ("Daily closes and buy/sell trades. P&L in USD." if points else
                "No saved prices for this stock. Load older prices to show the chart.")
        if con_id in self._history_exhausted:
            note += " IBKR has no earlier daily prices for this stock."
        if split_state == "fetching":
            note += " Checking trade markers…"
        elif split_state == "unavailable":
            note += " Split history unavailable; trade markers are hidden."
        if self._history_message:
            note += " " + self._history_message
        self.note.setText(note)
        if not points:
            finish_chart(self.chart_view, chart, self.theme, "No saved stock prices")
            return

        line = QLineSeries()
        line.setName("Daily close")
        line.setPen(self.chart_colors.pen("price", 2))
        for stamp, close in points:
            line.append(stamp, close)
        chart.addSeries(line)
        marker_series = []
        largest_notional = max((_trade_notional(row) for row, _, _ in chart_executions),
                               default=0.0)
        for side, color, marker in (("BUY", colors["positive"], QScatterSeries.MarkerShapeTriangle),
                                    ("SELL", colors["negative"], QScatterSeries.MarkerShapeRectangle)):
            entries = sorted((item for item in chart_executions if item[0]["side"] == side),
                             key=lambda item: _trade_notional(item[0]), reverse=True)
            if not entries:
                continue
            dots = QScatterSeries()
            dots.setName("Buys" if side == "BUY" else "Sells")
            dots.setColor(QColor(color))
            dots.setBorderColor(QColor(color))
            dots.setMarkerShape(marker)
            dots.setMarkerSize(12)
            details = {}
            for row, stamp, price in entries:
                index = dots.count()
                dots.append(stamp, price)
                notional = _trade_notional(row)
                dots.setPointConfiguration(
                    index, QScatterSeries.PointConfiguration.Size,
                    _marker_size(notional, largest_notional))
                label = (f"{row['trade_date']} {side} {row['quantity']:,.4f} @ "
                         f"{price:,.2f} {row['currency']} (split adjusted); "
                         f"recorded notional {notional:,.2f} {row['currency']}")
                details.setdefault((stamp, price), []).append(label)
            dots.hovered.connect(
                lambda point, state, labels=details: QToolTip.showText(
                    QCursor.pos(), "\n".join(labels.get((round(point.x()), point.y()), [])), self.chart_view)
                if state else QToolTip.hideText())
            chart.addSeries(dots)
            marker_series.append(dots)

        stamps = [stamp for stamp, _ in points] + [item[1] for item in chart_executions]
        values = [value for _, value in points] + [item[2] for item in chart_executions]
        first, last = min(stamps), max(stamps)
        if first == last:
            first -= 86400000
            last += 86400000
        x_axis = QDateTimeAxis()
        x_axis.setFormat("MMM d" if last - first <= 90 * 86400000 else "MMM yy")
        x_axis.setTickCount(6)
        x_axis.setRange(QDateTime.fromMSecsSinceEpoch(first), QDateTime.fromMSecsSinceEpoch(last))
        selected_decimals = self.axis_decimals_combo.currentData()
        y_axis = _price_axis(values, position["currency"] + " split adjusted", selected_decimals)
        chart.setLocale(QLocale("en_US"))
        chart.setLocalizeNumbers(True)
        for axis in (x_axis, y_axis):
            axis.setLabelsColor(QColor(colors["muted"]))
            axis.setGridLineColor(QColor(colors["line"]))
            axis.setLinePenColor(QColor(colors["line"]))
        chart.addAxis(x_axis, Qt.AlignBottom)
        chart.addAxis(y_axis, Qt.AlignLeft)
        for item in [line] + marker_series:
            item.attachAxis(x_axis)
            item.attachAxis(y_axis)
        finish_chart(self.chart_view, chart, self.theme)
        for button in (self.zoom_in_button, self.zoom_out_button, self.zoom_reset_button):
            button.setEnabled(True)
