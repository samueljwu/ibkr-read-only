from types import SimpleNamespace
from datetime import date, datetime

import pytest

from app.storage.database import Database
from app.finance.exposure import (add_custom_set, custom_labels, delete_custom_set,
                          exposure_snapshot, grouped_exposure, rename_custom_set,
                          save_broker_fact, set_custom_label)


def _row(con_id, symbol, kind, qty, value, multiplier=1):
    return {"id": con_id, "con_id": con_id, "display_ticker": symbol,
            "security_type": kind, "position_qty": qty, "value": value,
            "multiplier": multiplier, "currency": "USD", "price_source": "LAST"}


def test_stock_and_option_exposure_with_signed_delta_and_custom_underlying(tmp_path):
    db = Database(tmp_path / "exposure.sqlite3")
    db.set_portfolio_nav(1, 100000)
    rows = [_row(301, "ABC", "STK", -20, -3000),
            _row(401, "ABC CALL", "OPT", 2, 1000, 100),
            _row(402, "ABC PUT", "OPT", -1, -300, 100)]
    service = SimpleNamespace(portfolio_rows=lambda: rows)
    for option_id in (401, 402):
        assert save_broker_fact(db, 1, ("exposure_detail", option_id,
                                       {"under_con_id": 301, "sector": "Technology"}))
        assert save_broker_fact(db, 1, ("exposure_underlying_price", option_id,
                                       150, "USD", "Live"))
    save_broker_fact(db, 1, ("exposure_detail", 301,
                             {"sector": "Technology", "stock_type": "COMMON"}))
    save_broker_fact(db, 1, ("exposure_delta", 401, .6, "Live"))
    save_broker_fact(db, 1, ("exposure_delta", 402, -.4, "Live"))
    add_custom_set(db, 1, "Thesis")
    set_custom_label(db, 1, "Thesis", 301, "AI")
    snapshot = exposure_snapshot(db, service, 1, custom_set="Thesis")
    assert snapshot["totals"]["value"] == pytest.approx(-2300)
    assert snapshot["totals"]["adjusted"] == pytest.approx(21000)
    assert snapshot["totals"]["adjusted_gross"] == pytest.approx(27000)
    assert [item["equivalent_shares"] for item in snapshot["positions"]] == [None, 120, 40]
    assert {item["custom"] for item in snapshot["positions"]} == {"AI"}
    assert grouped_exposure(snapshot["positions"], "sector", "adjusted") == [
        ("Technology", 21000, 27000, 3)]


def test_missing_delta_keeps_adjusted_total_unavailable(tmp_path):
    db = Database(tmp_path / "missing.sqlite3")
    rows = [_row(401, "CALL", "OPT", 1, 50, 100)]
    service = SimpleNamespace(portfolio_rows=lambda: rows)
    save_broker_fact(db, 1, ("exposure_underlying_price", 401, 150, "USD", "Live"))
    snapshot = exposure_snapshot(db, service, 1)
    assert snapshot["totals"]["value"] == 50
    assert snapshot["totals"]["adjusted"] is None
    assert "delta" in snapshot["issues"][0]
    assert grouped_exposure(snapshot["positions"], "sector", "adjusted") == [
        ("Unclassified", None, None, 1)]
    assert not save_broker_fact(db, 1, ("exposure_delta", 401, 1.2, "Live"))


def test_options_share_one_fx_lookup_per_currency_on_exposure_refresh(tmp_path, monkeypatch):
    db = Database(tmp_path / "exposure-fx-cache.sqlite3")
    today = date.today().isoformat()
    db.set_fx_rate("EUR", today, 1.1)
    rows = [_row(401, "CALL A", "OPT", 1, 50, 100),
            _row(402, "CALL B", "OPT", 2, 70, 100)]
    for row in rows:
        row["currency"] = "EUR"
        save_broker_fact(db, 1, ("exposure_delta", row["con_id"], .5, "Live"))
        save_broker_fact(db, 1, ("exposure_underlying_price", row["con_id"],
                                 100, "EUR", "Live"))
    calls = []
    original = db.fx_rate

    def counted_fx(currency, day):
        calls.append((currency, day))
        return original(currency, day)

    monkeypatch.setattr(db, "fx_rate", counted_fx)
    snapshot = exposure_snapshot(db, SimpleNamespace(portfolio_rows=lambda: rows), 1)
    assert [row["adjusted"] for row in snapshot["positions"]] == pytest.approx([5500, 11000])
    assert calls == [("EUR", today)]
    db.conn.close()


def test_option_can_use_recent_underlying_holding_quote(tmp_path):
    db = Database(tmp_path / "underlying-quote.sqlite3")
    stock = _row(301, "QQQ", "STK", 1, 150)
    stock.update(last_px=150, last_updated=datetime.now().isoformat(timespec="seconds"),
                 price_source="LAST")
    option = _row(401, "QQQ CALL", "OPT", 2, 500, 100)
    service = SimpleNamespace(portfolio_rows=lambda: [stock, option])
    save_broker_fact(db, 1, ("exposure_detail", 401, {"under_con_id": 301}))
    save_broker_fact(db, 1, ("exposure_delta", 401, .5, "Live"))
    adjusted = exposure_snapshot(db, service, 1)["positions"][1]
    assert adjusted["adjusted"] == pytest.approx(15000)
    assert adjusted["underlying_mode"] == "Holding quote: LAST"
    stock["last_updated"] = "2020-01-01"
    assert exposure_snapshot(db, service, 1)["positions"][1]["adjusted"] is None








def test_ibkr_exposure_callbacks_keep_model_delta_and_latest_underlying_mark():
    from app.market.market_data import IBKRMarketDataProvider

    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id="DU123")
    client = provider.client
    client.contracts[1001] = SimpleNamespace(secType="OPT", conId=401)
    client.tickOptionComputation(1001, 13, 0, .2, -.4, 3, 0, 0, 0, 0, 150)
    client.tickOptionComputation(1001, 10, 0, .2, -.9, 3, 0, 0, 0, 0, 150)
    client.exposure_underlying_ticks[1002] = (301, 401, "USD")
    client.tickPrice(1002, 4, 150, None)
    client.tickPrice(1002, 9, 145, None)
    events = [provider.events.get_nowait() for _ in range(provider.events.qsize())]
    assert ("exposure_delta", 401, -.4, "Live") in events
    assert ("exposure_underlying_price", 401, 150.0, "USD", "Live") in events
    assert ("exposure_underlying_price", 401, 145.0, "USD", "Live") not in events


def test_ibkr_calculates_delta_when_model_missing_and_prices_are_available(monkeypatch):
    from app.market.market_data import IBKRMarketDataProvider

    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id="DU123")
    client = provider.client
    option = SimpleNamespace(secType="OPT", conId=401, currency="USD")
    stock = SimpleNamespace(secType="STK", conId=301, currency="USD")
    client.contracts[1001] = option
    client.contracts[1002] = stock
    client.exposure_underlying_parents[301] = {401}
    requests = []
    monkeypatch.setattr(client, "calculateImpliedVolatility",
                        lambda *args: requests.append(("iv", args)))
    monkeypatch.setattr(client, "calculateOptionPrice",
                        lambda *args: requests.append(("price", args)))
    client.tickPrice(1001, 1, 2.0, None)
    client.tickPrice(1001, 2, 4.0, None)
    client.tickPrice(1002, 4, 150.0, None)
    assert requests[0][0] == "iv"
    assert requests[0][1][2:4] == (3.0, 150.0)
    iv_id = requests[0][1][0]
    client.tickOptionComputation(iv_id, 53, 0, .25, float("nan"), 3, 0, 0, 0, 0, 150)
    assert requests[1][0] == "price"
    price_id = requests[1][1][0]
    client.tickOptionComputation(price_id, 53, 0, .25, .45, 3, 0, 0, 0, 0, 150)
    events = [provider.events.get_nowait() for _ in range(provider.events.qsize())]
    assert ("exposure_delta", 401, .45, "IBKR calculated") in events
    assert ("exposure_underlying_price", 401, 150.0, "USD", "Live") in events




def test_custom_set_editor_saves_underlying_assignments(tmp_path):
    from PySide6.QtWidgets import QApplication

    from app.views.exposure_custom_ui import CustomSetDialog
    from app.views.exposure_ui import ExposurePanel

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "custom.sqlite3")
    rows = [_row(301, "QQQ", "STK", 1, 100),
            _row(401, "QQQ CALL", "OPT", 1, 20, 100)]
    save_broker_fact(db, 1, ("exposure_detail", 401, {"under_con_id": 301}))
    panel = ExposurePanel(db, SimpleNamespace(portfolio_rows=lambda: rows), 1)
    add_custom_set(db, 1, "Theme")
    editor = CustomSetDialog(panel, "Theme")
    assert editor.table.rowCount() == 1
    editor.table.cellWidget(0, 1).setCurrentText("Core")
    editor._save()
    assert custom_labels(db, 1, "Theme") == {301: "Core"}
    assert {row["custom"] for row in exposure_snapshot(
        db, panel.service, 1, custom_set="Theme")["positions"]} == {"Core"}
    assert rename_custom_set(db, 1, "Theme", "Strategy") == "Strategy"
    assert custom_labels(db, 1, "Strategy") == {301: "Core"}
    delete_custom_set(db, 1, "Strategy")
    assert custom_labels(db, 1, "Strategy") == {}
    editor.close()
    panel.close()


def test_new_custom_set_opens_assignment_editor(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QInputDialog

    from app.views.exposure_custom_ui import CustomSetDialog
    from app.views.exposure_ui import ExposurePanel

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "new-set.sqlite3")
    service = SimpleNamespace(portfolio_rows=lambda: [_row(301, "QQQ", "STK", 1, 100)])
    panel = ExposurePanel(db, service, 1)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Strategy", True))

    def assign_in_editor(editor):
        assert editor.current_set == "Strategy"
        editor.table.cellWidget(0, 1).setCurrentText("Index")
        editor._save_and_close()
        return 1

    monkeypatch.setattr(CustomSetDialog, "exec", assign_in_editor)
    panel._new_set()
    assert panel.group_combo.currentText() == "Custom"
    assert panel.custom_set_combo.currentText() == "Strategy"
    assert custom_labels(db, 1, "Strategy") == {301: "Index"}
    panel.close()


def test_exposure_panel_drilldown_and_custom_controls(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.views.analytics_ui import AnalyticsTab

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "panel.sqlite3")
    db.set_portfolio_nav(1, 1000)
    rows = [_row(101, "ONE", "STK", 1, 120), _row(102, "TWO", "STK", 1, 80)]
    service = SimpleNamespace(portfolio_rows=lambda: rows,
                              matching=SimpleNamespace(trade_rows=lambda: []))
    save_broker_fact(db, 1, ("exposure_detail", 101, {"sector": "Technology"}))
    save_broker_fact(db, 1, ("exposure_detail", 102, {"sector": "Healthcare"}))
    panel = AnalyticsTab(db, service, 1)
    panel.sections.setCurrentWidget(panel.exposure_page)
    panel.refresh()
    assert panel.sections.tabText(3) == "Exposure"
    assert panel.range_combo.isHidden()
    assert "$200.00" in panel.exposure_page.net_summary.text()
    market_slices = panel.exposure_page.chart_view.chart().series()[0].slices()
    assert {slice_.label() for slice_ in market_slices} == {"Technology", "Healthcare"}
    selected_category = market_slices[0].label()
    market_slices[0].clicked.emit()
    assert panel.exposure_page.positions.rowCount() == 1
    panel.exposure_page.groups.sortItems(0, Qt.AscendingOrder)
    panel.exposure_page.render()
    selected_row = panel.exposure_page.groups.selectedItems()[0].row()
    assert panel.exposure_page.groups.item(selected_row, 0).data(Qt.UserRole) == selected_category
    panel.exposure_page._select_category(selected_category)
    assert panel.exposure_page.groups.rowCount() == 2
    assert panel.exposure_page.groups.horizontalHeaderItem(2).text() == "Mix %"
    assert {panel.exposure_page.groups.item(index, 2).text()
            for index in range(2)} == {"+60.0%", "+40.0%"}
    panel.exposure_page.groups.selectRow(0)
    assert panel.exposure_page.positions.rowCount() == 1
    add_custom_set(db, 1, "Theme")
    set_custom_label(db, 1, "Theme", 101, "Core")
    panel.exposure_page._load_custom_sets("Theme")
    panel.exposure_page.group_combo.setCurrentText("Custom")
    panel.exposure_page._group_changed()
    assert {panel.exposure_page.groups.item(index, 0).data(Qt.UserRole)
            for index in range(panel.exposure_page.groups.rowCount())} == {"Core", "Unclassified"}
    assert panel.exposure_page.custom_set_combo.currentText() == "Theme"
    panel.resize(650, 550)
    panel.show()
    QApplication.processEvents()
    assert panel.exposure_page.splitter.orientation() == Qt.Vertical
    panel.sections.setCurrentWidget(panel.pnl_page)
    assert not panel.range_combo.isHidden()
    panel.close()




def test_custom_set_selection_saved_and_legacy_view_restored(tmp_path):
    from PySide6.QtWidgets import QApplication

    from app.views.exposure_ui import ExposurePanel

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "custom-views.sqlite3")
    add_custom_set(db, 1, "Core")
    add_custom_set(db, 1, "Theme")
    service = SimpleNamespace(portfolio_rows=lambda: [_row(301, "ABC", "STK", 1, 100)])
    panel = ExposurePanel(db, service, 1)
    restored = ExposurePanel(db, service, 1)
    try:
        panel.group_combo.setCurrentText("Custom")
        panel.custom_set_combo.setCurrentText("Theme")
        state = panel.view_state()
        assert state["group"] == "Custom" and state["custom_set"] == "Theme"
        restored.apply_view_state(state)
        assert restored.group_combo.currentText() == "Custom"
        assert restored.custom_set_combo.currentText() == "Theme"
        restored.apply_view_state({"group": "Custom: Core"})
        assert restored.custom_set_combo.currentText() == "Core"
        restored.apply_view_state({"group": "Market cap", "category": "Large"})
        assert restored.group_combo.currentText() == "Sector"
        assert restored.category_filter is None
    finally:
        panel.close()
        restored.close()




def test_custom_view_guides_first_set_and_editor_switches_active_set(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QInputDialog

    from app.views.exposure_custom_ui import CustomSetDialog
    from app.views.exposure_ui import ExposurePanel

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "custom-flow.sqlite3")
    panel = ExposurePanel(db, SimpleNamespace(portfolio_rows=lambda: [
        _row(301, "ABC", "STK", 1, 100)]), 1)
    try:
        panel.group_combo.setCurrentText("Custom")
        assert panel.assign_button.text() == "Create set"
        assert panel.groups.rowCount() == 0
        assert "Create a set" in panel.chart_view.chart().title()
        add_custom_set(db, 1, "First")
        panel._load_custom_sets("First")
        panel._group_changed()
        editor = CustomSetDialog(panel, "First")
        try:
            monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Second", True))
            editor._new()
            assert editor.current_set == "Second"
            assert panel.custom_set_combo.currentText() == "Second"
            assert panel.assign_button.text() == "Manage categories"
        finally:
            editor.close()
    finally:
        panel.close()
