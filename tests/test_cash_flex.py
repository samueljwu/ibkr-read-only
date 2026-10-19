"""Synthetic Cash Flex transaction import and replay tests."""

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






@pytest.mark.parametrize("change", [
    lambda xml: xml.replace(b'accountId="DU123"', b'accountId="OTHER"'),
    lambda xml: xml.replace(b'currency="USD"', b'currency=""'),
    lambda xml: xml.replace(b'type="Dividends"', b'type="Unknown Cash Category"'),
    lambda xml: xml.replace(b'reportDate="20260106"', b'reportDate="20260107"'),
])
def test_invalid_cash_transaction_is_rejected(change):
    with pytest.raises(FlexValidationError):
        parse_cash_flex(change(report()), "DU123", DAY, DAY)






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
