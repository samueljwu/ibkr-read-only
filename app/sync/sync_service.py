"""Read-only Flex retrieval followed by replay-safe local database reconciliation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import msvcrt
from pathlib import Path
import time

from app.broker.broker_sync import BrokerIngestor, parse_flex_root, parse_flex_xml
from app.platform.diagnostics import configure_diagnostics
from app.flex.flex_import import FlexServiceError, FlexValidationError, flex_day, flex_today, safe_flex_error, fetch_flex
from app.platform.secret_store import load_flex_token
from app.finance.services import MatchingService


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
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if handle:
            handle.close()
        raise RuntimeError("Another IBKR Flex sync is running") from exc
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def planned_windows(db, account_id: str, today: date) -> list[tuple[str, str]]:
    through = today - timedelta(days=1)
    row = db.conn.execute("""SELECT MAX(to_day) FROM broker_sync_windows
        WHERE account_id=? AND source='flex' AND status='complete'""", (account_id,)).fetchone()
    last = date.fromisoformat(row[0]) if row and row[0] else None
    first = last - timedelta(days=7) if last else through - timedelta(days=29)
    if first > through:
        return []
    windows = []
    while first <= through:
        end = min(first + timedelta(days=13), through)
        windows.append((first.isoformat(), end.isoformat()))
        first = end + timedelta(days=1)
    return windows


def _validate_report_window(xml: bytes, account_id: str, first: str, last: str):
    root = parse_flex_root(xml)
    statements = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "FlexStatement"]
    if len(statements) != 1 or statements[0].get("accountId") != account_id:
        raise FlexValidationError("Flex report account mismatch")
    statement = statements[0]
    if (flex_day(statement.get("fromDate")), flex_day(statement.get("toDate"))) != (first, last):
        raise FlexValidationError("Flex report date range mismatch")
    sections = [node for node in statement if node.tag.rsplit("}", 1)[-1] == "Trades"]
    if len(sections) != 1:
        raise FlexValidationError("Flex query needs a Trades section at Executions detail")
    for node in sections[0]:
        if node.tag.rsplit("}", 1)[-1] != "Trade":
            continue
        detail = (node.get("levelOfDetail") or node.get("levelOfDetailCode") or "").upper()
        if detail not in {"EXECUTION", "EXECUTIONS"}:
            raise FlexValidationError("Flex query needs Trades at Executions detail; remove summary or closed lot rows")
    records, events = parse_flex_xml(xml, account_id, root=root)
    if len(records) != sum(node.tag.rsplit("}", 1)[-1] == "Trade" for node in sections[0]):
        raise FlexValidationError("Flex execution count mismatch; check the Trades section")
    if any(not first <= record["trade_date"] <= last for record in records):
        raise FlexValidationError("Flex execution outside requested date range")
    return records, events


def trade_sync_paused(db) -> bool:
    """True when IBKR has restricted the Flex token until an explicit retry.

    A restriction must survive restarts, so it is read back from the recorded
    error code; older databases only carry the message text.
    """
    code = db.metadata("ibkr_flex_sync_error_code")
    if code:
        return code == "1025"
    return (db.metadata("ibkr_flex_sync_status") == "failed"
            and "1025" in db.metadata("ibkr_flex_sync_error"))


def run_flex_sync(db, account_id: str, query_id: str, *, today: date | None = None,
                  token: str | None = None, fetcher=fetch_flex, pacing_seconds: float = 15,
                  windows_override: list[tuple[str, str]] | None = None,
                  on_window_start=None, on_window_complete=None,
                  on_window_unavailable=None, compare_existing=False,
                  on_window_result=None, on_window_unavailable_detail=None) -> dict:
    """Catch up missed days with overlap; never invoke any trading endpoint."""
    if not account_id or not query_id:
        raise ValueError("IBKR account and Flex query ID are required")
    db.validate_account_ownership(account_id)
    today = today or flex_today()
    with single_writer_lock(db.path):
        log = configure_diagnostics(db.path, "sync-trades")
        run_id = None

        def start_run():
            nonlocal run_id
            with db.conn:
                run_id = db.conn.execute("""INSERT INTO broker_sync_runs(account_id,source)
                    VALUES (?, 'flex')""", (account_id,)).lastrowid
            db.set_metadata("ibkr_flex_sync_run_id", str(run_id))
            db.set_metadata("ibkr_flex_sync_status", "running")
            db.set_metadata("ibkr_flex_sync_started", datetime.now().isoformat(timespec="seconds"))

        if not compare_existing:
            start_run()
        secret = token
        stage = "loading Flex token"
        current_window = None
        aggregate = {"source_count": 0, "inserted": 0, "updated": 0,
                     "duplicates": 0, "legacy_inserted": 0,
                     "corporate_inserted": 0, "review": 0, "windows": 0}
        changed_position_ids = set()
        unchanged = updated = 0
        try:
            secret = secret or load_flex_token(account_id)
            if not secret:
                raise ValueError("IBKR Flex token is missing; renew it in Client Portal")
            windows = windows_override if windows_override is not None else planned_windows(db, account_id, today)
            log.info("Run %s started; %s date windows planned through %s", run_id, len(windows), today - timedelta(days=1))
            if windows and not compare_existing:
                stage = "creating database backup"
                db.backup()
            last_request = 0.0
            ingestor = BrokerIngestor(db, account_id, allow_flex_corrections=compare_existing)
            for first, last in windows:
                current_window = (first, last)
                if on_window_start:
                    on_window_start(first, last)
                stage = "requesting Flex report"
                log.info("Run %s requesting window %s to %s", run_id, first, last)
                delay = pacing_seconds - (time.monotonic() - last_request)
                if last_request and delay > 0:
                    time.sleep(delay)
                last_request = time.monotonic()
                try:
                    xml = fetcher(query_id, secret, from_day=first, to_day=last)
                except FlexServiceError as exc:
                    if exc.code != 1003 or on_window_unavailable is None:
                        raise
                    safe_stage = exc.stage if exc.stage in ("SendRequest", "GetStatement") else "Flex service"
                    log.info("Run %s window %s to %s unavailable: Flex code 1003 stage=%s",
                             run_id, first, last, safe_stage)
                    if on_window_unavailable_detail is not None:
                        on_window_unavailable_detail(first, last, exc.code, exc.stage)
                    on_window_unavailable(first, last)
                    continue
                stage = "validating Flex report"
                records, events = _validate_report_window(xml, account_id, first, last)
                if compare_existing:
                    same, existing_review = ingestor.flex_report_matches(records, events, (first, last))
                    if same:
                        if not db.conn.execute("""SELECT 1 FROM broker_sync_windows WHERE account_id=?
                                AND source='flex' AND from_day=? AND to_day=?""",
                                (account_id, first, last)).fetchone():
                            with db.conn:
                                db.conn.execute("""INSERT INTO broker_sync_windows
                                    (account_id,source,from_day,to_day,status,source_count,imported_count,issue_count)
                                    VALUES (?,'flex',?,?,?,?,?,?)""",
                                    (account_id, first, last, "review" if existing_review else "complete",
                                     len(records), len(records), existing_review))
                        unchanged += 1
                        aggregate["source_count"] += len(records)
                        aggregate["duplicates"] += len(records)
                        aggregate["review"] += existing_review
                        aggregate["windows"] += 1
                        if on_window_result:
                            on_window_result(first, last, "needs_review" if existing_review else "unchanged")
                        if on_window_complete:
                            on_window_complete(first, last)
                        continue
                    if run_id is None:
                        db.backup()
                        start_run()
                stage = "importing executions"
                result = ingestor.ingest_many(records, "flex", window=(first, last), events=events)
                changed_position_ids |= result.changed_position_ids
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
                updated += bool(result.inserted or result.updated or result.legacy_inserted or result.corporate_inserted)
                if on_window_result:
                    on_window_result(first, last, "needs_review" if result.review else
                                     "updated" if result.changed_position_ids else "unchanged")
                if on_window_complete:
                    on_window_complete(first, last)
            if changed_position_ids:
                # Match once for the whole import: re-matching every window's
                # positions from scratch is quadratic in the number of windows.
                stage = "rebuilding trade matches"
                MatchingService(db).rebuild_auto(changed_position_ids)
                changed_position_ids = set()
            if compare_existing:
                aggregate.update(unchanged_windows=unchanged, updated_windows=updated)
            if run_id is None:
                return aggregate
            with db.conn:
                db.conn.execute("""UPDATE broker_sync_runs SET finished_at=CURRENT_TIMESTAMP,
                    status=?,from_day=?,to_day=?,source_count=?,inserted_count=?,review_count=?
                    WHERE id=?""",
                    ("review" if aggregate["review"] else "complete",
                     windows[0][0] if windows else None, windows[-1][1] if windows else None,
                     aggregate["source_count"], aggregate["inserted"], aggregate["review"], run_id))
            db.set_metadata("ibkr_flex_sync_status", "review" if aggregate["review"] else "complete")
            db.set_metadata("ibkr_flex_sync_error", "")
            db.set_metadata("ibkr_flex_sync_error_code", "")
            db.set_metadata("ibkr_flex_sync_stage", "")
            db.set_metadata("ibkr_flex_sync_completed", datetime.now().isoformat(timespec="seconds"))
            log.info("Run %s %s: %s", run_id, "review" if aggregate["review"] else "complete", aggregate)
            return aggregate
        except Exception as exc:
            if changed_position_ids:
                # Windows already imported must still be matched even though the
                # run failed: a later run sees them as duplicates and would not.
                try:
                    MatchingService(db).rebuild_auto(changed_position_ids)
                except Exception:
                    log.error("Run %s could not rebuild matches after a failed window", run_id)
            message = safe_flex_error(exc)
            window_text = f"{current_window[0]} to {current_window[1]}" if current_window else "before first window"
            log.error("Run %s failed during %s (%s): %s: %s", run_id, stage, window_text,
                      type(exc).__name__, message)
            if run_id is not None:
                with db.conn:
                    db.conn.execute("""UPDATE broker_sync_runs SET finished_at=CURRENT_TIMESTAMP,
                        status='failed',from_day=?,to_day=?,error=? WHERE id=?""",
                        (current_window[0] if current_window else None,
                         current_window[1] if current_window else None, message[:1000], run_id))
                db.set_metadata("ibkr_flex_sync_status", "failed")
                db.set_metadata("ibkr_flex_sync_error", message[:1000])
                db.set_metadata("ibkr_flex_sync_error_code",
                                str(exc.code) if isinstance(exc, FlexServiceError) and type(exc.code) is int and 1000 <= exc.code <= 1999 else "")
                db.set_metadata("ibkr_flex_sync_stage", stage)
                db.set_metadata("ibkr_flex_sync_failed_at", datetime.now().isoformat(timespec="seconds"))
            raise
