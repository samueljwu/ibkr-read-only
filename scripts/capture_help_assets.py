"""Capture guide images from real Qt controls with synthetic portfolio data."""

import logging
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_SCALE_FACTOR"] = "2"

from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QSplitter, QWidget

import app.ui as ui
from app.platform import secret_store
from app.views.lot_matching_ui import LotMatchDialog
from app.views.typography import install_app_font


ASSETS = Path(__file__).resolve().parents[1] / "app" / "views" / "help_assets"
ACTIVE_SCREENSHOTS = frozenset({
    "start_flex", "rebalance_table", "rebalance_cases", "views_view_menu",
})


def save_crop(widget, name, theme, left, top, width, height):
    if name not in ACTIVE_SCREENSHOTS:
        return
    image = widget.grab(QRect(left, top, width, height))
    path = ASSETS / f"{name}_{theme}@2x.png"
    if image.isNull() or not image.save(str(path)):
        raise RuntimeError(f"Could not capture {path}")
    print(path.name, image.width(), image.height())


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    keys = {}
    identity = lambda path: str(Path(path).resolve()).casefold()
    secret_store.load_database_key = lambda path: keys.get(identity(path))
    secret_store.save_database_key = lambda path, key: keys.__setitem__(identity(path), key)
    secret_store.load_flex_token = lambda account: None
    ui.configure_diagnostics = lambda *args: logging.getLogger("help-capture")
    app = QApplication([])
    install_app_font(app)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        window = ui.MainWindow(database_path=Path(directory) / "synthetic.sqlite3")
        window.resize(1100, 760)
        position_id = window.db.add_position({"display_ticker": "DEMO"})
        for day, side, quantity, price in (("2026-01-02", "BUY", 6, 10),
                                           ("2026-01-03", "BUY", 4, 9),
                                           ("2026-01-05", "SELL", 10, 12)):
            window.db.add_trade({"position_id": position_id, "trade_date": day,
                                 "side": side, "quantity": quantity, "price": price,
                                 "commission_usd": 0})
        today = date.today()
        held_id = window.db.add_position({"display_ticker": "HOLD"})
        window.db.cache_contract(held_id, 101)
        window.db.save_history(101, [
            {"day": (today - timedelta(days=offset)).isoformat(),
             "close": 145 + (10 - offset) * 0.7, "volume": 1000}
            for offset in range(10, 0, -1)])
        window.db.add_trade({"position_id": held_id,
                             "trade_date": (today - timedelta(days=4)).isoformat(),
                             "side": "BUY", "quantity": 8, "price": 25,
                             "commission_usd": 0})
        gain_id = window.db.add_position({"display_ticker": "GAIN"})
        gain_buy = window.db.add_trade({"position_id": gain_id,
                                        "trade_date": (today - timedelta(days=10)).isoformat(),
                                        "side": "BUY", "quantity": 5, "price": 20,
                                        "commission_usd": 0})
        gain_sell = window.db.add_trade({"position_id": gain_id,
                                         "trade_date": (today - timedelta(days=8)).isoformat(),
                                         "side": "SELL", "quantity": 5, "price": 25,
                                         "commission_usd": 0})
        window.service.matching.match(gain_sell, [(gain_buy, 5)])
        for offset, nav in ((5, 1000), (4, 1012), (3, 1008), (2, 1023), (1, 1030)):
            window.db.save_account_history(1, (today - timedelta(days=offset)).isoformat(),
                                           nav, 250, flow_covered=True)
        cash_day = (today - timedelta(days=2)).isoformat()
        with window.db.conn:
            window.db.conn.execute("""INSERT INTO cash_movements
                (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
                 kind,currency,amount,description,source_section,source_hash)
                VALUES (1,'DEMO','sample-dividend',?,?,?,?,?,?,?,?,?)""",
                (cash_day, cash_day, cash_day, "Dividend", "USD", "12.00",
                 "Synthetic dividend", "CashTransactions", "synthetic"))
        window.market.refresh(window.db.rows("positions"))
        window._refresh_models()
        window.show()
        window.db.set_setting("ib_account_id", "DEMO")
        window.db.set_setting("analytics_flex_query_id", "234567")
        window.db.set_metadata("ibkr_analytics_sync_status", "complete")
        window.db.set_metadata("ibkr_analytics_sync_completed", today.isoformat())
        window.settings_fields["ib_host"].setText("127.0.0.1")
        window.settings_fields["ib_port"].setText("7496")
        window.settings_fields["ib_account_id"].setText("DEMO")
        window.settings_fields["flex_query_id"].setText("123456")
        window.settings_fields["analytics_flex_query_id"].setText("234567")
        window.settings_fields["cash_flex_query_id"].setText("345678")
        window.tabs.setCurrentWidget(window.views["Lots"].parentWidget())
        window.views["Lots"].selectRow(0)
        window.open_settings()
        window.open_history_import()
        app.processEvents()
        settings_page = window.settings_dialog.findChild(QWidget, "settingsPage")
        sale_id = next(t["id"] for t in window.db.rows("trades") if t["side"] == "SELL")
        buy_id = next(t["id"] for t in window.db.rows("trades") if t["side"] == "BUY")
        dialog = LotMatchDialog(window.service, position_id, "DEMO", window,
                                sale_id=sale_id)
        dialog.resize(800, 650)
        for column, width in ((0, 130), (1, 68), (2, 62), (3, 260)):
            dialog.sale_table.setColumnWidth(column, width)
        for column, width in ((0, 135), (1, 70), (2, 78), (3, 72), (4, 150)):
            dialog.table.setColumnWidth(column, width)
        for theme in ("light", "dark"):
            dialog._reset_sale()
            window._apply_theme(theme)
            dialog.set_theme(theme)
            window.settings_dialog.show()
            app.processEvents()
            save_crop(settings_page, "start_connection", theme, 0, 0, 598, 115)
            save_crop(settings_page, "start_flex", theme, 0, 112, 598, 155)
            save_crop(settings_page, "start_apply", theme, 0, 267, 598, 140)
            window.settings_dialog.hide()
            sync_left = window.sync_button.mapTo(window, window.sync_button.rect().topLeft()).x()
            sync_top = window.sync_button.mapTo(window, window.sync_button.rect().topLeft()).y()
            save_crop(window, "start_sync", theme,
                      max(0, sync_left - 100), max(0, sync_top - 22), 260, 90)
            window.history_import_dialog.show()
            app.processEvents()
            import_form = window.history_import_dialog.flex_card
            save_crop(import_form, "start_import", theme, 0, 0,
                      import_form.width(), import_form.height())
            older_card = window.history_import_dialog.older_card
            save_crop(older_card, "setup_older_csv", theme, 0, 0,
                      older_card.width(), older_card.height())
            window.history_import_dialog.hide()
            window.tabs.setCurrentWidget(window.views["Lots"].parentWidget())
            app.processEvents()
            lots_page = window.views["Lots"].parentWidget()
            save_crop(lots_page, "matched_lots", theme, 0, 0, 560, 185)
            dialog.show()
            dialog.findChild(QSplitter).setSizes([175, 420])
            app.processEvents()
            save_crop(dialog.sale_table, "matched_sale", theme, 0, 0, 530, 115)
            save_crop(dialog.table, "matched_purchase", theme, 0, 0, 530, 120)
            if not dialog.move_sale(sale_id, buy_id, 0):
                raise RuntimeError("Synthetic lot match was rejected")
            app.processEvents()
            save_crop(dialog.table, "matched_after", theme, 0, 0, 530, 120)
            dialog.hide()
            window.matches_menu.show()
            app.processEvents()
            save_crop(window.matches_menu, "matched_menu", theme,
                      0, 0, window.matches_menu.width(), window.matches_menu.height())
            window.matches_menu.hide()

            window.tabs.setCurrentWidget(window.views["Portfolio"].parentWidget())
            app.processEvents()
            save_crop(window.views["Portfolio"].parentWidget(), "portfolio_overview", theme,
                      0, 0, 610, 150)
            window.tabs.setCurrentWidget(window.views["Trades"].parentWidget())
            app.processEvents()
            save_crop(window.views["Trades"].parentWidget(), "portfolio_trades", theme,
                      0, 0, 610, 180)
            window.tabs.setCurrentWidget(window.cash_tab)
            window.cash_tab.refresh()
            app.processEvents()
            save_crop(window.cash_tab.toolbar, "portfolio_cash_range", theme, 0, 0, 610, 52)
            save_crop(window.cash_tab.transactions_pane, "portfolio_cash", theme,
                      0, 0, 610, 100)

            window.tabs.setCurrentWidget(window.analytics_tab)
            window.analytics_tab.range_combo.setCurrentText("5 days")
            window.analytics_tab.refresh()
            for page, name, height in (
                    (window.analytics_tab.pnl_page, "analytics_pnl", 175),
                    (window.analytics_tab.capital_page, "analytics_capital", 190),
                    (window.analytics_tab.return_page, "analytics_returns", 210),
                    (window.analytics_tab.exposure_page, "analytics_exposure", 190),
                    (window.analytics_tab.monthly_page, "analytics_monthly", 180),
                    (window.analytics_tab.levels_page, "analytics_levels", 160)):
                window.analytics_tab.sections.setCurrentWidget(page)
                app.processEvents()
                save_crop(page, name, theme, 0, 0, 610, height)

            window.tabs.setCurrentWidget(window.calcs_tab)
            window.calcs_tab.refresh()
            window.calcs_tab.view_choice.setCurrentText("All")
            app.processEvents()
            save_crop(window.calcs_tab, "rebalance_setup", theme, 0, 0, 610, 185)
            save_crop(window.calcs_tab.view_choice, "rebalance_show", theme, 0, 0, 240, 30)
            save_crop(window.calcs_tab.table, "rebalance_table", theme, 0, 0, 610, 90)
            window.calcs_tab.view_choice.setCurrentText("Upside / downside")
            app.processEvents()
            save_crop(window.calcs_tab.table, "rebalance_cases", theme, 0, 0, 610, 90)

            window.tabs.setCurrentWidget(window.views["Portfolio"].parentWidget())
            app.processEvents()
            header_menu = window._build_header_menu("Portfolio", 4)
            header_menu.show()
            app.processEvents()
            save_crop(header_menu, "views_columns", theme,
                      0, 0, header_menu.width(), header_menu.height())
            header_menu.hide()
            for title, name in (("View", "views_view_menu"),
                                ("File", "views_file_menu")):
                action = next(action for action in window.menuBar().actions()
                              if action.text() == title)
                QTest.mouseClick(window.menuBar(), Qt.LeftButton,
                                 pos=window.menuBar().actionGeometry(action).center())
                app.processEvents()
                popups = [widget for widget in app.topLevelWidgets()
                          if isinstance(widget, QMenu) and widget.isVisible()]
                if not popups:
                    raise RuntimeError(f"{title} menu did not open")
                menu = popups[-1]
                save_crop(menu, name, theme, 0, 0, menu.width(), menu.height())
                menu.hide()
        dialog.close()
        window.close()
        window.db.conn.close()


if __name__ == "__main__":
    main()
