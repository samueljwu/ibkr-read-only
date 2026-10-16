"""Synthetic examples for cumulative instrument results and income attribution."""

from datetime import date, timedelta
import os

import pytest

from app.finance.position_returns import position_return_rows
from app.finance.services import PortfolioService
from app.flex.cash_flex import parse_cash_flex, run_cash_flex_sync
from app.market.market_data import MockMarketDataProvider
from app.storage.database import Database

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _income(db, key, day, kind, amount, *, con_id=None, symbol="", fx="1",
            currency="USD", income_type=None, transaction_id="", description="Synthetic"):
    with db.conn:
        db.conn.execute("""INSERT INTO cash_movements
            (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
             kind,currency,amount,description,source_section,source_hash,
             con_id,symbol,transaction_id,fx_to_usd,income_type)
            VALUES (1,'SYNTHETIC',?,?,?,? ,?, ?,?,?,'Cash Transactions',
                    'synthetic',?,?,?, ?,?)""",
            (key, day, day, day, kind, currency, str(amount), description, con_id, symbol,
             transaction_id, fx, income_type or kind))


def _cover_cash(db, first, last):
    db.set_setting("ib_account_id", "SYNTHETIC")
    with db.conn:
        db.conn.execute("""INSERT INTO broker_sync_windows
            (account_id,source,from_day,to_day,status) VALUES
            ('SYNTHETIC','flex_cash',?,?,'complete')""", (first, last))


def test_closed_position_includes_income_tax_and_purchase_cost(tmp_path):
    db = Database(tmp_path / "returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.cache_contract(pid, 101)
        buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                            "quantity": 100, "price": 10, "commission_usd": 1})
        sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-05", "side": "SELL",
                             "quantity": 100, "price": 12, "commission_usd": 1})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 100)])
        _income(db, "div", "2026-01-06", "Dividend", 20, con_id=101,
                transaction_id="income-1")
        _income(db, "tax", "2026-01-06", "Withholding tax", -3,
                transaction_id="income-1")
        _cover_cash(db, "2026-01-02", "2026-01-06")
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        rows, unattributed = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        assert not unattributed
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "Closed"
        assert row["realized"] == pytest.approx(198)
        assert row["unrealized"] == 0
        assert row["income"] == pytest.approx(17)
        assert row["total"] == pytest.approx(215)
        assert row["percent"] == pytest.approx(215 / 1001 * 100)
        assert not row["issues"]
    finally:
        db.conn.close()


def test_open_and_unattributed_income_leave_total_unavailable(tmp_path):
    db = Database(tmp_path / "open-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
        db.cache_contract(pid, 202)
        start = (date.today() - timedelta(days=5)).isoformat()
        through = (date.today() - timedelta(days=1)).isoformat()
        db.add_trade({"position_id": pid, "trade_date": start, "side": "BUY",
                      "quantity": 10, "price": 10, "commission_usd": 0})
        _cover_cash(db, start, through)
        service = PortfolioService(db, MockMarketDataProvider())
        valuations = [{"id": pid, "position_qty": 10, "last_px": 12,
                       "fx_to_usd": 1, "reconciliation": "OK", "price_source": "MOCK"}]
        lots = service.lots_rows(valuations)
        rows, _ = position_return_rows(db, 1, valuations, lots)
        assert rows[0]["unrealized"] == pytest.approx(20)
        assert rows[0]["total"] == pytest.approx(20)
        _income(db, "orphan", through, "Dividend", 5)
        rows, unattributed = position_return_rows(db, 1, valuations, lots)
        assert len(unattributed) == 1
        assert rows[0]["unrealized"] == pytest.approx(20)
        assert rows[0]["total"] is None
        assert "Unattributed income" in rows[0]["issues"]
    finally:
        db.conn.close()


def test_return_total_requires_contiguous_cash_window_coverage(tmp_path):
    db = Database(tmp_path / "cash-gap-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "GAP", "ib_symbol": "GAP"})
        buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-02",
                            "side": "BUY", "quantity": 1, "price": 10})
        sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-05",
                             "side": "SELL", "quantity": 1, "price": 12})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 1)])
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        rows, _ = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        assert rows[0]["realized"] == pytest.approx(2)
        assert rows[0]["total"] is None
        assert "Cash history incomplete" in rows[0]["issues"]
        _cover_cash(db, "2026-01-02", "2026-01-03")
        with db.conn:
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status) VALUES
                ('SYNTHETIC','flex_cash','2026-01-05','2026-01-05','complete')""")
        rows, _ = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        assert rows[0]["total"] is None
        with db.conn:
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status) VALUES
                ('SYNTHETIC','flex_cash','2026-01-04','2026-01-04','complete')""")
        rows, _ = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        assert rows[0]["total"] == pytest.approx(2)
        assert not rows[0]["issues"]
    finally:
        db.conn.close()


def test_partial_sale_open_lot_and_foreign_bond_income(tmp_path):
    db = Database(tmp_path / "partial-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "BOND", "ib_symbol": "BOND"})
        db.cache_contract(pid, 303)
        start = (date.today() - timedelta(days=5)).isoformat()
        through = (date.today() - timedelta(days=1)).isoformat()
        buy = db.add_trade({"position_id": pid, "trade_date": start, "side": "BUY",
                            "quantity": 10, "price": 10, "commission_usd": 1})
        sell = db.add_trade({"position_id": pid, "trade_date": through, "side": "SELL",
                             "quantity": 4, "price": 15, "commission_usd": 1})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 4)])
        _income(db, "coupon", through, "Credit interest", 5, con_id=303,
                currency="EUR", fx="1.2", income_type="Bond interest")
        _income(db, "coupon-tax", through, "Withholding tax", -1, con_id=303,
                currency="EUR", fx="1.2")
        _cover_cash(db, start, through)
        valuations = [{"id": pid, "position_qty": 6, "last_px": 12,
                       "fx_to_usd": 1, "reconciliation": "OK", "price_source": "MOCK"}]
        rows, _ = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        row = rows[0]
        assert row["realized"] == pytest.approx(18.6)
        assert row["unrealized"] == pytest.approx(11.4)
        assert row["income"] == pytest.approx(4.8)
        assert row["total"] == pytest.approx(34.8)
        assert row["percent"] == pytest.approx(34.8 / 101 * 100)
    finally:
        db.conn.close()


def test_bond_clean_price_interest_transfers_and_withholding_are_separate(tmp_path):
    db = Database(tmp_path / "bond-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "BOND", "ib_symbol": "BOND",
                               "security_type": "BOND", "multiplier": 0.01})
        db.cache_contract(pid, 404)
        buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                            "quantity": 1000, "price": 95, "accrued_interest_usd": -30})
        sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-08", "side": "SELL",
                             "quantity": 1000, "price": 97, "accrued_interest_usd": 35})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 1000)])
        _income(db, "coupon", "2026-01-08", "Credit interest", 20, con_id=404,
                income_type="Bond interest")
        _income(db, "tax", "2026-01-08", "Withholding tax", -4, con_id=404)
        # IBKR can also list settlement accrued interest in Cash Transactions.
        _income(db, "buy-accrued", "2026-01-02", "Debit interest", -30,
                con_id=404, income_type="Bond interest", description="Accrued Interest Paid BOND")
        _income(db, "sell-accrued", "2026-01-08", "Credit interest", 35,
                con_id=404, income_type="Bond interest", description="Accrued Interest Received BOND")
        _cover_cash(db, "2026-01-02", "2026-01-08")
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        row = position_return_rows(db, 1, valuations, service.lots_rows(valuations))[0][0]
        assert row["realized"] == pytest.approx(20)
        assert row["income"] == pytest.approx(-30 + 35 + 20 - 4)
        assert row["total"] == pytest.approx(41)
        assert row["percent"] == pytest.approx(41 / 980 * 100)
        assert len(row["income_details"]) == 4
        assert not row["issues"]
    finally:
        db.conn.close()


def test_open_bond_requires_current_accrued_receivable_for_full_return(tmp_path):
    db = Database(tmp_path / "open-bond-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "BOND", "ib_symbol": "BOND",
                               "security_type": "BOND", "multiplier": 0.01})
        db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                      "quantity": 1000, "price": 95, "accrued_interest_usd": -30})
        _cover_cash(db, "2026-01-02", (date.today() - timedelta(days=1)).isoformat())
        valuations = [{"id": pid, "position_qty": 1000, "last_px": 95,
                       "fx_to_usd": 1, "reconciliation": "OK", "price_source": "MOCK"}]
        service = PortfolioService(db, MockMarketDataProvider())
        lots = service.lots_rows(valuations)
        row = position_return_rows(db, 1, valuations, lots)[0][0]
        assert row["unrealized"] == 0
        assert row["income"] == -30
        assert row["total"] is None
        assert "Bond accrued interest unavailable" in row["issues"]
        valuations[0]["bond_accrued_interest_usd"] = 35
        row = position_return_rows(db, 1, valuations, lots)[0][0]
        assert row["income"] == 5
        assert row["total"] == 5
        assert row["percent"] == pytest.approx(5 / 980 * 100)
    finally:
        db.conn.close()


def test_missing_withholding_fx_blocks_total(tmp_path):
    db = Database(tmp_path / "missing-tax-fx.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.cache_contract(pid, 505)
        buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-02",
                            "side": "BUY", "quantity": 1, "price": 10})
        sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-08",
                             "side": "SELL", "quantity": 1, "price": 12})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 1)])
        _income(db, "tax", "2026-01-08", "Withholding tax", -1,
                con_id=505, currency="EUR", fx="")
        _cover_cash(db, "2026-01-02", "2026-01-08")
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        row = position_return_rows(db, 1, valuations, service.lots_rows(valuations))[0][0]
        assert row["realized"] == 2
        assert row["income"] is None
        assert row["total"] is None
        assert "Income FX missing" in row["issues"]
    finally:
        db.conn.close()


def test_closed_short_has_dollar_result_without_return_on_cost(tmp_path):
    db = Database(tmp_path / "short-returns.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "OPT", "ib_symbol": "OPT",
                               "security_type": "OPT", "multiplier": 100})
        db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "SELL",
                      "quantity": 1, "price": 13.67, "multiplier": 100})
        db.add_trade({"position_id": pid, "trade_date": "2026-01-05", "side": "BUY",
                      "quantity": 1, "price": 0, "multiplier": 100})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.rebuild_auto([pid])
        _cover_cash(db, "2026-01-02", "2026-01-05")
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        rows, _ = position_return_rows(db, 1, valuations, service.lots_rows(valuations))
        assert rows[0]["total"] == pytest.approx(1367)
        assert rows[0]["percent"] is None
    finally:
        db.conn.close()


def test_cash_parser_retains_instrument_and_fx_fields():
    xml = b"""<FlexQueryResponse><FlexStatements>
      <FlexStatement accountId="SYNTHETIC" fromDate="20260106" toDate="20260106">
        <CashTransactions><CashTransaction accountId="SYNTHETIC" currency="USD"
          dateTime="20260106;090000" type="Dividends" amount="2"
          conid="101" symbol="ABC" fxRateToBase="1" transactionID="tx-1" />
        </CashTransactions>
      </FlexStatement></FlexStatements></FlexQueryResponse>"""
    movement = parse_cash_flex(xml, "SYNTHETIC", "2026-01-06", "2026-01-06")[0]
    assert (movement["con_id"], movement["symbol"], movement["transaction_id"],
            movement["fx_to_usd"], movement["income_type"]) == (
                101, "ABC", "tx-1", "1", "Dividend")


def test_cash_reimport_adds_attribution_even_when_amount_is_unchanged(tmp_path):
    db = Database(tmp_path / "cash-attribution.sqlite3")
    try:
        template = """<FlexQueryResponse><FlexStatements>
          <FlexStatement accountId="SYNTHETIC" fromDate="20260106" toDate="20260106">
            <CashTransactions><CashTransaction accountId="SYNTHETIC" currency="USD"
              dateTime="20260106;090000" type="Dividends" amount="2"
              transactionID="tx-1" {fields} /></CashTransactions>
          </FlexStatement></FlexStatements></FlexQueryResponse>"""
        report = lambda fields: template.format(fields=fields).encode()
        kwargs = {"windows_override": [("2026-01-06", "2026-01-06")],
                  "token": "synthetic-token", "backup": False, "pacing_seconds": 0}
        run_cash_flex_sync(db, "SYNTHETIC", "query", fetcher=lambda *a, **k: report(""), **kwargs)
        result = run_cash_flex_sync(
            db, "SYNTHETIC", "query", compare_existing=True,
            fetcher=lambda *a, **k: report('conid="101" symbol="ABC" fxRateToBase="1"'),
            **kwargs)
        assert result["updated_windows"] == 1
        movement = db.cash_movement_rows(1)[0]
        assert movement["con_id"] == 101
        assert movement["symbol"] == "ABC"
    finally:
        db.conn.close()


def test_return_panel_lists_closed_instrument_and_income_detail(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from app.views.returns_ui import ReturnPanel

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "return-panel.sqlite3")
    panel = None
    try:
        pid = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.cache_contract(pid, 101)
        buy = db.add_trade({"position_id": pid, "trade_date": "2026-01-02", "side": "BUY",
                            "quantity": 1, "price": 10})
        sell = db.add_trade({"position_id": pid, "trade_date": "2026-01-05", "side": "SELL",
                             "quantity": 1, "price": 12})
        service = PortfolioService(db, MockMarketDataProvider())
        service.matching.match(sell, [(buy, 1)])
        _income(db, "income", "2026-01-05", "Dividend", 2, con_id=101)
        _cover_cash(db, "2026-01-02", "2026-01-05")
        valuations = [{"id": pid, "position_qty": 0, "reconciliation": "OK"}]
        panel = ReturnPanel(db, service, 1)
        panel.refresh(valuations, service.lots_rows(valuations))
        assert panel.table.rowCount() == 1
        assert panel.table.item(0, 5).text() == "$4.00"
        assert panel.details_table.rowCount() == 2
        entries = {panel.details_table.item(index, 0).text(): index
                   for index in range(panel.details_table.rowCount())}
        assert set(entries) == {"Lot", "Income"}
        assert panel.details_table.item(entries["Lot"], 3).text() == "ABC"
        assert panel.details_table.item(entries["Lot"], 7).text() == "$2.00"
        assert panel.details_table.item(entries["Income"], 9).text() == "$2.00"
        assert panel.details_table.item(entries["Income"], 10).text() == "2.00"
        assert panel.details_table.item(0, 0).text() == "Income"
        panel.layout_combo.setCurrentText("Side by side")
        assert panel.splitter.orientation() == Qt.Horizontal
        assert panel.view_state()["layout"] == "Side by side"
        panel.show.setCurrentText("Currently held")
        assert panel.table.rowCount() == 0
        assert panel.details_table.rowCount() == 0
        panel.show.setCurrentText("Closed")
        panel.search.setText("ABC")
        assert panel.table.rowCount() == 1
        panel.layout_combo.setCurrentText("Top and bottom")
        assert panel.splitter.orientation() == Qt.Vertical
        panel.apply_view_state({"layout": "Side by side"})
        assert panel.splitter.orientation() == Qt.Horizontal
        reopened = ReturnPanel(db, service, 1)
        try:
            assert reopened.layout_combo.currentText() == "Side by side"
            assert reopened.splitter.orientation() == Qt.Horizontal
        finally:
            reopened.close()
        _income(db, "income-missing-fx", "2026-01-05", "Dividend", 3,
                con_id=101, fx="", currency="EUR")
        panel.refresh(valuations, service.lots_rows(valuations))
        assert panel.table.item(0, 5).text() == "—"
        assert "Income FX missing" in panel.table.item(0, 7).text()
        assert "total return is unavailable" in panel.note.text()
    finally:
        if panel is not None:
            panel.close()
        db.conn.close()


def test_old_analytics_view_selection_keeps_exposure_after_return_insert(tmp_path):
    from PySide6.QtWidgets import QApplication
    from app.views.analytics_ui import AnalyticsTab

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "analytics-view.sqlite3")
    panel = None
    try:
        panel = AnalyticsTab(db, PortfolioService(db, MockMarketDataProvider()), 1)
        state = panel.view_state()
        state["version"] = 1
        state["section"] = 2
        state.pop("return")
        assert panel.apply_view_state(state)
        assert panel.sections.currentWidget() is panel.exposure_page
    finally:
        if panel is not None:
            panel.close()
        db.conn.close()
