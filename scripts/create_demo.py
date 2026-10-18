"""Build a synthetic, plaintext portfolio and capture the real desktop UI."""

import argparse
from contextlib import closing
import json
import logging
import math
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "demo" / "portfolio.sqlite3"
DEFAULT_SCREENSHOTS = ROOT / "docs" / "images"
DEMO_NAV_USD = 1_500_000
DEMO_DEPOSIT_USD = 100_000


def business_days(end, count):
    days = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def create_database(path, today):
    from app.finance.services import MatchingService
    from app.market.market_data import MockMarketDataProvider
    from app.storage.database import Database

    path.parent.mkdir(parents=True, exist_ok=True)
    # A pre-existing SQLite header makes this an intentionally unencrypted demo.
    with sqlite3.connect(path) as plain:
        plain.execute("CREATE TABLE _demo_init (id INTEGER)")
        plain.execute("DROP TABLE _demo_init")
    db = Database(path)
    try:
        db.conn.execute("UPDATE portfolios SET name='Magnificent Seven Demo' WHERE id=1")
        db.set_portfolio_nav(1, DEMO_NAV_USD)
        db.set_setting("provider", "Mock")
        db.set_setting("ui_theme", "dark")
        db.set_setting("ib_account_id", "DEMO")
        db.set_setting("analytics_pnl_splitter_orientation", "horizontal")
        db.set_setting("analytics_capital_splitter_orientation", "horizontal")
        db.set_setting("analytics_levels_splitter_1_orientation", "horizontal")
        db.set_metadata("synthetic_demo", "1")
        db.set_setting("calcs_targets_1", json.dumps({
            "mode": "individual", "cash_target": 0.15,
            "targets": {"1": 0.10, "2": 0.17, "3": 0.05, "4": 0.09,
                        "5": 0.09, "6": 0.27, "7": 0.08},
        }))

        holdings = (
            ("AAPL", 800, 135.00, "Technology"),
            ("MSFT", 1400, 162.00, "Technology"),
            ("GOOGL", 900, 64.00, "Communication"),
            ("AMZN", 700, 160.00, "Consumer cyclical"),
            ("NVDA", 650, 158.00, "Technology"),
            ("META", 3500, 146.00, "Communication"),
            ("TSLA", 450, 170.00, "Consumer cyclical"),
        )
        sale_quantities = {"NVDA": 100, "META": 200}
        days = business_days(today - timedelta(days=1), 125)
        matcher = MatchingService(db)
        for index, (ticker, quantity, cost, sector) in enumerate(holdings, 1):
            con_id = 900000 + index
            position_id = db.add_position({
                "display_ticker": ticker, "ib_symbol": ticker,
                "notes": "Synthetic demo instrument",
            })
            db.cache_contract(position_id, con_id)
            first_qty = quantity + sale_quantities.get(ticker, 0)
            first_buy = db.add_trade({
                "position_id": position_id, "trade_id": f"DEMO-{ticker}-BUY-1",
                "trade_date": days[15 + index * 3].isoformat(),
                "side": "BUY", "quantity": first_qty, "price": cost,
                "commission_usd": 1.25, "notes": "Synthetic demo trade",
            })
            if ticker in sale_quantities:
                sale = db.add_trade({
                    "position_id": position_id, "trade_id": f"DEMO-{ticker}-SELL-1",
                    "trade_date": days[85 + index].isoformat(),
                    "side": "SELL", "quantity": sale_quantities[ticker],
                    "price": round(cost * (1.08 if ticker == "META" else 0.96), 2),
                    "commission_usd": 1.25, "notes": "Synthetic demo trade",
                })
                matcher.match(sale, [(first_buy, sale_quantities[ticker])])
            with db.conn:
                db.conn.execute("""INSERT INTO exposure_facts
                    (portfolio_id,con_id,sector,category,stock_type,details_as_of)
                    VALUES (1,?,?,?,?,?)""",
                    (con_id, sector, "Stock", "Common", datetime.now().isoformat(timespec="seconds")))

        positions = db.rows("positions", 1)
        market = MockMarketDataProvider()
        market.refresh(positions)
        quantities = {ticker: quantity for ticker, quantity, _cost, _sector in holdings}
        holdings_value = 0.0
        daily_pnl = 0.0
        for position in positions:
            quote = market.quote(position["id"])
            quantity = quantities[position["display_ticker"]]
            holdings_value += quantity * quote["ticks"]["last"]
            daily_pnl += quantity * (quote["ticks"]["last"] - quote["ticks"]["close"])
            db.save_history(position["con_id"], [
                {**bar, "close": round(bar["close"] + 1.2 * math.sin(index * 0.17), 2)}
                for index, bar in enumerate(quote["history"])
            ])
        if daily_pnl <= 0:
            raise RuntimeError("Demo holdings must have positive daily P&L")
        cash_balance = round(DEMO_NAV_USD - holdings_value, 2)
        last_day = days[-1]
        db.set_account_value(1, "TotalCashValue", cash_balance, "USD", last_day.isoformat())
        end_wave = 4700 * math.sin((len(days) - 1) * 0.83) + 2500 * math.sin((len(days) - 1) * 1.91)
        for index, day in enumerate(days):
            progress = index / (len(days) - 1)
            wave = 4700 * math.sin(index * 0.83) + 2500 * math.sin(index * 1.91)
            deposited = day >= days[4]
            nav = 1_250_000 + 150_000 * progress + wave - end_wave * progress
            cash = cash_balance + 30_000 * (1 - progress) - (0 if deposited else DEMO_DEPOSIT_USD)
            if deposited:
                nav += DEMO_DEPOSIT_USD
            db.save_account_history(1, day.isoformat(), round(nav, 2), round(cash, 2),
                                    source="flex", flow_covered=True)
        db.save_external_flow(1, "DEMO-DEPOSIT", days[4].isoformat(), DEMO_DEPOSIT_USD, "deposit")

        cash_rows = (
            (days[-8], "Dividend", "420.00", "AAPL synthetic dividend", "AAPL"),
            (days[-17], "Interest", "183.50", "Synthetic cash interest", ""),
            (days[-33], "Dividend", "310.00", "MSFT synthetic dividend", "MSFT"),
            (days[-48], "Withholding tax", "-62.40", "Synthetic withholding tax", "MSFT"),
            (days[-69], "Dividend", "278.00", "META synthetic dividend", "META"),
            (days[4], "Deposit", "100000.00", "Synthetic opening deposit", ""),
        )
        with db.conn:
            db.conn.executemany("""INSERT INTO cash_movements
                (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
                 kind,currency,amount,description,source_section,symbol,source_hash)
                VALUES (1,'DEMO',?,?,?,?,?,'USD',?,?,?,?,'synthetic')""",
                [(f"DEMO-CASH-{index}", day.isoformat(), day.isoformat(), day.isoformat(),
                  kind, amount, description, "CashTransactions", symbol)
                 for index, (day, kind, amount, description, symbol) in enumerate(cash_rows, 1)])
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status,source_count,imported_count)
                VALUES ('DEMO','flex_cash',?,?, 'complete',?,?)""",
                (days[0].isoformat(), (today - timedelta(days=1)).isoformat(),
                 len(cash_rows), len(cash_rows)))
        db.conn.commit()
        if db.conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Demo database failed integrity check")
    finally:
        db.conn.close()


def capture(path, screenshot_dir):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    # Keep screenshot generation from creating a log folder beside the demo.
    ui.configure_diagnostics = lambda *args: logging.getLogger("demo-capture")
    app = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 9))
    symbols_path = Path("C:/Windows/Fonts/seguisym.ttf")
    if symbols_path.is_file():
        QFontDatabase.addApplicationFont(str(symbols_path))
    window = ui.MainWindow(database_path=path)
    try:
        window.analytics_tab.levels_page._split_history.events = lambda *_: (None, "unavailable")
        window.resize(1400, 700)
        window.show()
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        tabs = (
            ("overview", window.views["Portfolio"].parentWidget()),
            ("trades", window.views["Trades"].parentWidget()),
            ("lots", window.views["Lots"].parentWidget()),
            ("cash", window.cash_tab),
            ("analytics", window.analytics_tab),
            ("rebalance", window.calcs_tab),
        )
        for name, tab in tabs:
            window.resize(1400, 900 if name == "analytics" else 700)
            window.tabs.setCurrentWidget(tab)
            app.processEvents()
            app.processEvents()
            screenshot = screenshot_dir / f"demo-{name}.png"
            image = window.grab()
            if image.isNull() or not image.save(str(screenshot)):
                raise RuntimeError(f"Could not save screenshot: {screenshot}")
        window.resize(1400, 900)
        window.tabs.setCurrentWidget(window.analytics_tab)
        analytics = window.analytics_tab
        sections = (
            ("returns", analytics.return_page),
            ("capital", analytics.capital_page),
            ("exposure", analytics.exposure_page),
            ("monthly", analytics.monthly_page),
            ("levels", analytics.levels_page),
        )
        for name, page in sections:
            width, height = (1000, 650) if name == "monthly" else (1400, 900)
            window.resize(width, height)
            analytics.sections.setCurrentWidget(page)
            if name == "capital":
                analytics.range_combo.setCurrentText("All")
                analytics.capital_combo.setCurrentText("Buy / sell activity")
            analytics.refresh()
            if name == "levels":
                stock_index = analytics.levels_page.stock_combo.findText("META")
                if stock_index < 0:
                    raise RuntimeError("Synthetic META stock is missing from buy / sell levels")
                analytics.levels_page.stock_combo.setCurrentIndex(stock_index)
            app.processEvents()
            app.processEvents()
            screenshot = screenshot_dir / f"demo-analytics-{name}.png"
            image = window.grab()
            if image.isNull() or not image.save(str(screenshot)):
                raise RuntimeError(f"Could not save screenshot: {screenshot}")
        analytics.sections.setCurrentWidget(analytics.pnl_page)
        analytics.pnl_view_combo.setCurrentText("Calendar")
        analytics.refresh()
        app.processEvents()
        app.processEvents()
        screenshot = screenshot_dir / "demo-analytics-calendar.png"
        image = window.grab()
        if image.isNull() or not image.save(str(screenshot)):
            raise RuntimeError(f"Could not save screenshot: {screenshot}")
        analytics.range_combo.setCurrentText("3 months")
        window.tabs.setCurrentWidget(window.views["Portfolio"].parentWidget())
        app.processEvents()
    finally:
        window.close()
        window.db.conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--screenshots", type=Path, default=DEFAULT_SCREENSHOTS)
    parser.add_argument("--replace", action="store_true", help="replace an existing demo database")
    args = parser.parse_args()
    if args.database.exists():
        if not args.replace:
            parser.error(f"{args.database} exists; pass --replace to regenerate it")
        try:
            with closing(sqlite3.connect(args.database)) as existing:
                marker = existing.execute("""SELECT value FROM app_metadata
                    WHERE key='synthetic_demo'""").fetchone()
        except sqlite3.DatabaseError:
            marker = None
        if marker != ("1",):
            parser.error(f"{args.database} is not a marked synthetic demo database")
        args.database.unlink()
    create_database(args.database, date.today())
    capture(args.database, args.screenshots)
    print(f"Demo database: {args.database}")
    print(f"Screenshots: {args.screenshots}")


if __name__ == "__main__":
    main()
