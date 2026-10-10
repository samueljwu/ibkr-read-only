"""Read-only Flex retrieval followed by replay-safe local database reconciliation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from app.broker.broker_sync import BrokerIngestor, parse_flex_xml
from app.platform.diagnostics import configure_diagnostics
from app.imports.flex_import import DEFAULT_REQUEST_PACING_SECONDS, FlexRequestPacer, FlexServiceError, fetch_flex
from app.storage.secret_store import load_flex_token
from app.finance.services import MatchingService
from app.sync.history_coverage import catchup_windows, remember_account_open_day
from app.broker.broker_lots import retain_lots, restore_saved_lot_links, reconcile_closed_lots, reconcile_transferred_lots, check_open_lots
from app.broker.broker_fees import reconcile_fee_adjustments


def trade_sync_paused(db) -> bool:
    """Honor the broker restriction, including errors saved by older versions."""
    return (db.metadata('ibkr_flex_paused') or '') not in ('', '1003') or db.metadata("ibkr_flex_sync_status") == "failed" and (
        db.metadata("ibkr_flex_sync_error_code") == "1025"
        or db.metadata("ibkr_flex_sync_error").startswith("Flex error 1025:"))


@contextmanager
def single_writer_lock(database_path: Path, lock_name="ibkr-sync.lock"):
    lock_path = database_path.parent / lock_name
    handle = None
    try:
        handle = lock_path.open("a+b")
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if handle:
            handle.close()
        raise RuntimeError("Another IBKR Flex sync is running") from exc
    try:
        yield
    finally:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def planned_windows(db, account_id: str, today: date) -> list[tuple[str, str]]:
    return catchup_windows(db, account_id, 'trades', today, 28)


def _validate_report_window(xml: bytes, account_id: str, first: str, last: str):
    root = ET.fromstring(xml)
    statements = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "FlexStatement"]
    if len(statements) != 1 or statements[0].get("accountId") != account_id:
        raise ValueError("Flex report account mismatch")
    if (statements[0].get("fromDate"), statements[0].get("toDate")) != (
        first.replace("-", ""), last.replace("-", "")
    ):
        raise ValueError("Flex report date range mismatch")
    records, events = parse_flex_xml(xml, account_id)
    count = sum(node.tag.rsplit("}", 1)[-1] == "Trade"
                and (node.get("levelOfDetail") or "").upper() == "EXECUTION"
                for node in root.iter())
    if count != len(records):
        raise ValueError("Flex execution count mismatch")
    if any(not first <= record["trade_date"] <= last for record in records):
        raise ValueError("Flex execution outside requested date range")
    return records, events


def run_flex_sync(db, account_id: str, query_id: str, *, today: date | None = None,
                  token: str | None = None, fetcher=fetch_flex,
                  pacing_seconds: float = DEFAULT_REQUEST_PACING_SECONDS,
                  windows_override: list[tuple[str, str]] | None = None,
                  on_window_start=None, on_window_complete=None,
                  on_window_unavailable=None, request_pacer=None,
                  stop_on_unavailable=False, backup=True) -> dict:
    """Catch up missed days with overlap; never invoke any trading endpoint."""
    if not account_id or not query_id:
        raise ValueError("IBKR account and Flex query ID are required")
    db.validate_account_ownership(account_id)
    today = today or date.today()
    with single_writer_lock(db.path):
        log = configure_diagnostics(db.path, "sync-trades")
        with db.conn:
            run_id = db.conn.execute("""INSERT INTO broker_sync_runs(account_id,source)
                VALUES (?, 'flex')""", (account_id,)).lastrowid
        db.set_metadata("ibkr_flex_sync_run_id", str(run_id))
        db.set_metadata("ibkr_flex_sync_status", "running")
        db.set_metadata("ibkr_flex_sync_started", datetime.now().isoformat(timespec="seconds"))
        secret = token
        stage = "loading Flex token"
        current_window = None
        aggregate = {"source_count": 0, "inserted": 0, "updated": 0,
                     "duplicates": 0, "legacy_inserted": 0,
                     "corporate_inserted": 0, "review": 0, "windows": 0}
        try:
            secret = secret or load_flex_token(account_id)
            if not secret:
                raise ValueError("IBKR Flex token is missing; renew it in Client Portal")
            windows = windows_override if windows_override is not None else planned_windows(db, account_id, today)
            log.info("Run %s started; %s date windows planned through %s", run_id, len(windows), today - timedelta(days=1))
            if windows and backup:
                stage = "creating database backup"
                db.backup()
            pacer = request_pacer or FlexRequestPacer(pacing_seconds)
            pacer.configure(db, account_id)
            unavailable = 0
            def unavailable_window(first, last):
                nonlocal unavailable
                unavailable += 1
                if on_window_unavailable:
                    on_window_unavailable(first, last)

            stage = 'requesting Flex report'
            def starting_window(first, last):
                nonlocal current_window, stage
                current_window = (first, last)
                stage = 'requesting Flex report'
                if on_window_start:
                    on_window_start(first, last)
            for first, last, xml in pacer.reports(fetcher, query_id, secret, windows,
                    starting_window, unavailable_window if on_window_unavailable else None):
                current_window = (first, last)
                log.info("Run %s requesting window %s to %s", run_id, first, last)
                stage = "validating Flex report"
                try:
                    records, events = _validate_report_window(xml, account_id, first, last)
                except (ValueError, ET.ParseError):
                    pacer.invalidate()
                    raise
                stage = "importing executions"
                remember_account_open_day(db, account_id, xml)
                with db.conn:
                    result = BrokerIngestor(db, account_id).ingest_many(
                        records, "flex", window=(first, last), events=events, transaction=False)
                    retain_lots(db, account_id, xml)
                    restore_saved_lot_links(db, account_id, query_id)
                    result.changed_position_ids.update(reconcile_transferred_lots(db, account_id))
                    reconcile_fee_adjustments(db, account_id)
                    result.changed_position_ids.update(reconcile_closed_lots(db, account_id))
                    if result.changed_position_ids:
                        stage = "rebuilding trade matches"
                        MatchingService(db).rebuild_auto(result.changed_position_ids, transaction=False)
                    check_open_lots(db, account_id)
                    result.review += db.conn.execute('''SELECT COUNT(*) FROM broker_source_records
                        WHERE account_id=? AND status='review'
                        AND (source IN ('flex_closed_lot','flex_fee')
                             OR (source='flex_open_lot' AND issue LIKE 'Broker%'))''', (account_id,)).fetchone()[0]
                log.info("Run %s window %s to %s complete: source=%s inserted=%s updated=%s duplicates=%s review=%s",
                         run_id, first, last, len(records), result.inserted, result.updated,
                         result.duplicates, result.review)
                aggregate["source_count"] += len(records)
                aggregate["inserted"] += result.inserted
                aggregate["updated"] += result.updated
                aggregate["duplicates"] += result.duplicates
                aggregate["legacy_inserted"] += result.legacy_inserted
                aggregate["corporate_inserted"] += result.corporate_inserted
                aggregate["review"] += result.review
                aggregate["windows"] += 1
                if on_window_complete:
                    on_window_complete(first, last)
                stage = 'requesting Flex report'
            pending = db.conn.execute('''SELECT COUNT(*) FROM broker_source_records
                WHERE account_id=? AND status='pending' AND (source='flex_closed_lot'
                OR (source='flex_open_lot' AND issue LIKE 'Waiting for earlier%'))''', (account_id,)).fetchone()[0]
            final_status = 'partial' if unavailable or pending or pacer.user_paused else 'review' if aggregate['review'] else 'complete'
            with db.conn:
                db.conn.execute("""UPDATE broker_sync_runs SET finished_at=CURRENT_TIMESTAMP,
                    status=?,from_day=?,to_day=?,source_count=?,inserted_count=?,review_count=?
                    WHERE id=?""",
                    (final_status,
                     windows[0][0] if windows else None, windows[-1][1] if windows else None,
                     aggregate["source_count"], aggregate["inserted"], aggregate["review"], run_id))
            db.set_metadata("ibkr_flex_sync_status", final_status)
            db.set_metadata("ibkr_flex_sync_error", "")
            db.set_metadata("ibkr_flex_sync_error_code", "")
            if not unavailable:
                db.set_metadata('ibkr_flex_paused', '')
            db.set_metadata("ibkr_flex_sync_stage", "")
            db.set_metadata("ibkr_flex_sync_completed", datetime.now().isoformat(timespec="seconds"))
            log.info("Run %s %s: %s", run_id, final_status, aggregate)
            if unavailable:
                aggregate["unavailable_windows"] = unavailable
            return aggregate
        except Exception as exc:
            message = str(exc).replace(secret, "[redacted]") if secret else str(exc)
            window_text = f"{current_window[0]} to {current_window[1]}" if current_window else "before first window"
            log.error("Run %s failed during %s (%s): %s: %s", run_id, stage, window_text,
                      type(exc).__name__, message)
            with db.conn:
                db.conn.execute("""UPDATE broker_sync_runs SET finished_at=CURRENT_TIMESTAMP,
                    status='failed',from_day=?,to_day=?,error=? WHERE id=?""",
                    (current_window[0] if current_window else None,
                     current_window[1] if current_window else None, message[:1000], run_id))
            db.set_metadata("ibkr_flex_sync_status", "failed")
            db.set_metadata("ibkr_flex_sync_error", message[:1000])
            db.set_metadata("ibkr_flex_sync_error_code",
                            str(exc.code) if isinstance(exc, FlexServiceError) and exc.code else "")
            db.set_metadata("ibkr_flex_sync_stage", stage)
            db.set_metadata("ibkr_flex_sync_failed_at", datetime.now().isoformat(timespec="seconds"))
            raise
