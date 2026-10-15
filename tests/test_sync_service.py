from datetime import date

import pytest

from app.storage.database import Database
from app.flex.flex_import import FlexServiceError
from app.sync.sync_service import run_flex_sync, single_writer_lock


ACCOUNT = "U0000002"


def _report(first, last, trade_days=()):
    trades = "".join(
        f'''<Trade levelOfDetail="EXECUTION" accountId="{ACCOUNT}" tradeID="T{day}"
        ibExecID="EX{day}.01" tradeDate="{day}" dateTime="{day};101500"
        buySell="BUY" quantity="5" tradePrice="10" conid="123" symbol="ABC"
        currency="USD" assetCategory="STK" multiplier="1" ibCommission="-0.5"/>'''
        for day in trade_days if first.replace("-", "") <= day <= last.replace("-", ""))
    return (f'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="{ACCOUNT}"
        baseCurrency="USD" fromDate="{first.replace('-', '')}" toDate="{last.replace('-', '')}">
        <Trades>{trades}</Trades></FlexStatement></FlexStatements></FlexQueryResponse>''').encode()


def test_startup_replay_and_missed_days_sync_without_duplicate_trades(tmp_path):
    db = Database(tmp_path / "daily.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    calls = []

    def fetcher(_query, _token, *, from_day, to_day):
        calls.append((from_day, to_day))
        return _report(from_day, to_day, ("20260916", "20261015"))

    try:
        first = run_flex_sync(db, ACCOUNT, "123", today=date(2026, 10, 6),
                              token="dummy", fetcher=fetcher, pacing_seconds=0)
        assert first["inserted"] == 1 and first["windows"] == 3
        replay = run_flex_sync(db, ACCOUNT, "123", today=date(2026, 10, 6),
                               token="dummy", fetcher=fetcher, pacing_seconds=0)
        assert replay["inserted"] == 0 and replay["duplicates"] == 0
        missed = run_flex_sync(db, ACCOUNT, "123", today=date(2026, 11, 1),
                               token="dummy", fetcher=fetcher, pacing_seconds=0)
        assert missed["inserted"] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM broker_executions").fetchone()[0] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM broker_sync_runs WHERE status='complete'").fetchone()[0] == 3
        assert max(end for _, end in calls) == "2026-10-31"
    finally:
        db.conn.close()


def test_failed_window_still_matches_what_was_already_imported(tmp_path):
    db = Database(tmp_path / "partial.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    calls = {"n": 0}

    def trades(first, last, rows):
        trades_xml = "".join(
            f'''<Trade levelOfDetail="EXECUTION" accountId="{ACCOUNT}" tradeID="{tag}"
            ibExecID="EX{tag}.01" tradeDate="{day}" dateTime="{day};101500" buySell="{side}"
            quantity="5" tradePrice="{price}" conid="777" symbol="ABC" currency="USD"
            assetCategory="STK" multiplier="1" ibCommission="-0.5"/>'''
            for tag, day, side, price in rows if first.replace("-", "") <= day <= last.replace("-", ""))
        return (f'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="{ACCOUNT}"
            baseCurrency="USD" fromDate="{first.replace('-', '')}" toDate="{last.replace('-', '')}">
            <Trades>{trades_xml}</Trades></FlexStatement></FlexStatements></FlexQueryResponse>''').encode()

    def fetcher(_query, _token, *, from_day, to_day):
        calls["n"] += 1
        if calls["n"] == 2:
            raise FlexServiceError(1012, "Token has expired.", "SendRequest")
        return trades(from_day, to_day, [("B1", "20260916", "BUY", 10),
                                        ("S1", "20260916", "SELL", 12)])

    try:
        with pytest.raises(FlexServiceError):
            run_flex_sync(db, ACCOUNT, "123", today=date(2026, 10, 6), token="dummy",
                          fetcher=fetcher, pacing_seconds=0)
        # The first window's buy and sell were matched before the run failed; a
        # later run would skip them as duplicates and never match them.
        assert db.conn.execute("SELECT COUNT(*) FROM trade_matches").fetchone()[0] == 1
    finally:
        db.conn.close()


def test_failed_report_records_actionable_status_and_lock_blocks_second_writer(tmp_path):
    db = Database(tmp_path / "failed.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    try:
        with single_writer_lock(db.path):
            with pytest.raises(RuntimeError, match="Another IBKR Flex sync"):
                with single_writer_lock(db.path):
                    pass

        def broken(_query, _token, *, from_day, to_day):
            return _report(from_day, to_day).replace(b"U0000002", b"OTHER")

        with pytest.raises(ValueError, match="account mismatch"):
            run_flex_sync(db, ACCOUNT, "123", today=date(2026, 10, 6),
                          token="dummy", fetcher=broken, pacing_seconds=0)
        assert db.metadata("ibkr_flex_sync_status") == "failed"
        assert db.conn.execute("SELECT status FROM broker_sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "failed"
    finally:
        db.conn.close()


def test_failed_window_records_stage_and_redacts_token_from_log(tmp_path):
    db = Database(tmp_path / "redacted.sqlite3")
    secret = "private-flex-token-example"

    def broken(_query, _token, *, from_day, to_day):
        raise ValueError(f"Request rejected with {secret}")

    try:
        with pytest.raises(ValueError):
            run_flex_sync(db, ACCOUNT, "123", today=date(2026, 10, 6),
                          token=secret, fetcher=broken, pacing_seconds=0)
        row = db.conn.execute("SELECT * FROM broker_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "failed"
        assert (row["from_day"], row["to_day"]) == ("2026-09-06", "2026-09-19")
        assert db.metadata("ibkr_flex_sync_stage") == "requesting Flex report"
        log = (tmp_path / "logs" / "sync-trades.log").read_text(encoding="utf-8")
        assert "requesting Flex report" in log
        assert "Flex import failed; check the query sections" in log
        assert secret not in log + row["error"] + db.metadata("ibkr_flex_sync_error")
    finally:
        db.conn.close()
