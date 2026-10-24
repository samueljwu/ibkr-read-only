"""Cash activity remains usable without statement balance checks."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication

from app.storage.database import Database
from app.views.cash_ui import CashMovementsTab


def _seed(db):
    movements = (
        ("usd-div", "2026-01-05", "Dividend", "USD", "10.25"),
        ("aud-interest", "2026-01-06", "Credit interest", "AUD", "2.50"),
        ("usd-deposit", "2026-01-06", "Deposit", "USD", "100.00"),
    )
    with db.conn:
        db.conn.executemany("""INSERT INTO cash_movements
            (portfolio_id,account_id,source_key,reported_day,period_from,period_to,
             kind,currency,amount,description,source_section,source_hash)
            VALUES (1,'DU123',?,?,'2026-01-05','2026-01-06',?,?,?,?,'Cash Transactions','synthetic')""",
            [(key, day, kind, currency, amount, key) for key, day, kind, currency, amount in movements])


def test_cash_view_shows_all_imported_currencies_and_date_range(tmp_path):
    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "cash.sqlite3")
    try:
        _seed(db)
        tab = CashMovementsTab(db, 1)
        assert tab.table.rowCount() == 3
        assert not hasattr(tab, "status")
        assert not hasattr(tab, "hide_zero_checkbox")
        assert not tab.table.isColumnHidden(3)
        assert tab.summary_values[("Dividends", "USD")].text() == "10.25"
        assert tab.summary_values[("Interest", "AUD")].text() == "2.50"
        tab.range_combo.setCurrentText("Custom")
        tab.start_date.setDate(QDate(2026, 1, 6))
        tab.end_date.setDate(QDate(2026, 1, 6))
        assert tab.table.rowCount() == 2
        tab.start_date.setDate(QDate(2026, 1, 7))
        assert tab.table.rowCount() == 0
        assert tab.empty_status.text() == "Choose a valid date range."
        tab.close()
    finally:
        db.conn.close()
