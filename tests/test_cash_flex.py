"""Synthetic Cash Flex transaction import and replay tests."""

from datetime import date
import xml.etree.ElementTree as ET

import pytest

from app.flex.cash_flex import parse_cash_flex, run_cash_flex_sync
from app.flex.flex_import import FlexValidationError
from app.storage.database import Database


DAY = "2026-01-06"
SPAN = [(DAY, DAY)]


def report(*, account="DU123", dividend="2", include_aud=False, empty=False):
    rows = "" if empty else (
        f'<CashTransaction accountId="{account}" currency="USD" reportDate="20260106" '
        f'type="Dividends" amount="{dividend}" conid="101" symbol="ABC" '
        'transactionID="dividend-1" />'
        + ('<CashTransaction currency="AUD" reportDate="20260106" '
           'type="Credit Interest" amount="3" />' if include_aud else ""))
    return (f'<FlexQueryResponse><FlexStatements><FlexStatement accountId="{account}" '
            f'fromDate="20260106" toDate="20260106"><CashTransactions>{rows}'
            '</CashTransactions></FlexStatement></FlexStatements></FlexQueryResponse>').encode()


def sync(db, xml, **options):
    return run_cash_flex_sync(db, "DU123", "cash-query", token="synthetic",
                              fetcher=lambda *a, **k: xml, pacing_seconds=0,
                              windows_override=SPAN, **options)


def test_cash_transactions_do_not_require_cash_report():
    rows = parse_cash_flex(report(include_aud=True), "DU123", DAY, DAY)
    assert [(row["currency"], row["amount"]) for row in rows] == [("USD", "2"), ("AUD", "3")]
    assert (rows[0]["con_id"], rows[0]["symbol"], rows[0]["income_type"]) == (
        101, "ABC", "Dividend")


def test_cash_report_balance_does_not_block_transactions():
    root = ET.fromstring(report())
    statement = root.find(".//FlexStatement")
    ET.SubElement(statement, "CashReport").append(ET.Element("CashReportCurrency", {
        "currency": "USD", "startingCash": "100", "endingCash": "999"}))
    assert len(parse_cash_flex(ET.tostring(root), "DU123", DAY, DAY)) == 1


@pytest.mark.parametrize("change", [
    lambda xml: xml.replace(b'accountId="DU123"', b'accountId="OTHER"'),
    lambda xml: xml.replace(b'currency="USD"', b'currency=""'),
    lambda xml: xml.replace(b'type="Dividends"', b'type="Unknown Cash Category"'),
    lambda xml: xml.replace(b'reportDate="20260106"', b'reportDate="20260107"'),
])
def test_invalid_cash_transaction_is_rejected(change):
    with pytest.raises(FlexValidationError):
        parse_cash_flex(change(report()), "DU123", DAY, DAY)


def test_cash_sync_tracks_empty_period_and_replays_corrections(tmp_path):
    db = Database(tmp_path / "cash.sqlite3")
    try:
        db.set_setting("ib_account_id", "DU123")
        assert sync(db, report(empty=True)) == {"windows": 1, "movements": 0}
        assert db.conn.execute("""SELECT status,source_count FROM broker_sync_windows
            WHERE source='flex_cash'""").fetchone()[:] == ("complete", 0)
        assert sync(db, report(dividend="2"))["movements"] == 1
        assert sync(db, report(dividend="3"), compare_existing=True)["updated_windows"] == 1
        assert db.cash_movement_rows(1)[0]["amount"] == "3"
        before = db.conn.total_changes
        assert sync(db, report(dividend="3"), compare_existing=True)["unchanged_windows"] == 1
        assert db.conn.total_changes == before
        assert run_cash_flex_sync(db, "DU123", "cash-query", today=date(2026, 1, 7),
                                  token="synthetic") == {"windows": 0, "movements": 0}
    finally:
        db.conn.close()


def test_cash_replacement_removes_omitted_currency_on_normal_sync(tmp_path):
    db = Database(tmp_path / "currencies.sqlite3")
    try:
        sync(db, report(include_aud=True))
        sync(db, report())
        assert [(row["currency"], row["amount"]) for row in db.cash_movement_rows(1)] == [
            ("USD", "2")]
    finally:
        db.conn.close()


def test_cash_recheck_keeps_prior_rows_on_invalid_report(tmp_path):
    db = Database(tmp_path / "correction.sqlite3")
    try:
        sync(db, report())
        before = [dict(row) for row in db.cash_movement_rows(1)]
        with pytest.raises(FlexValidationError):
            sync(db, report().replace(b'type="Dividends"', b'type="Unknown Cash Category"'),
                 compare_existing=True)
        assert db.cash_movement_rows(1) == before
    finally:
        db.conn.close()


def test_legacy_cash_period_is_migrated_before_dropping_bridge_table(tmp_path):
    path = tmp_path / "legacy-cash.sqlite3"
    db = Database(path)
    try:
        assert not db.conn.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='cash_reconciliations'""").fetchone()
        with db.conn:
            db.conn.execute("""CREATE TABLE cash_reconciliations (
                account_id TEXT, period_from TEXT, period_to TEXT, currency TEXT)""")
            db.conn.execute("""INSERT INTO cash_reconciliations VALUES
                ('DU123','2026-01-01','2026-01-06','USD')""")
            db.conn.execute("DELETE FROM settings WHERE key='cash_windows_migrated'")
    finally:
        db.conn.close()
    reopened = Database(path)
    try:
        assert tuple(reopened.conn.execute("""SELECT account_id,from_day,to_day,status
            FROM broker_sync_windows WHERE source='flex_cash'""").fetchone()) == (
                "DU123", "2026-01-01", "2026-01-06", "complete")
        assert not reopened.conn.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='cash_reconciliations'""").fetchone()
    finally:
        reopened.conn.close()


def test_previously_migrated_bridge_table_is_dropped(tmp_path):
    path = tmp_path / "migrated-cash.sqlite3"
    db = Database(path)
    try:
        with db.conn:
            db.conn.execute("CREATE TABLE cash_reconciliations (account_id TEXT)")
            db.conn.execute("INSERT INTO cash_reconciliations VALUES ('DU123')")
    finally:
        db.conn.close()
    reopened = Database(path)
    try:
        assert not reopened.conn.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='cash_reconciliations'""").fetchone()
    finally:
        reopened.conn.close()
