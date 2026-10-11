"""Fetch and validate completed IBKR cash activity through Flex."""

import sys

from app.imports.cash_flex import run_cash_flex_sync
from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics


def main(database_path=None):
    try:
        db = Database(database_path)
    except Exception:
        if sys.stderr is not None:
            print("Portfolio storage is unavailable. Open the app to resolve migration, recovery, or active-session errors.", file=sys.stderr)
        return 1
    try:
        log = configure_diagnostics(db.path, "sync-cash")
        db.set_metadata("ibkr_cash_sync_status", "running")
        log.info("Cash history sync started")
        result = run_cash_flex_sync(db, db.setting("ib_account_id"), db.setting("cash_flex_query_id"))
        unavailable = (result or {}).get('unavailable_windows', 0)
        state = 'partial' if unavailable else 'complete'
        db.set_metadata("ibkr_cash_sync_status", state)
        db.set_metadata("ibkr_cash_sync_error", f'{unavailable} requested Cash report windows were unavailable; saved periods are retained.'
                        if unavailable else "")
        log.info("Cash history sync %s: %s", state, result)
        return 0
    except Exception as exc:
        db.set_metadata("ibkr_cash_sync_status", "failed")
        db.set_metadata("ibkr_cash_sync_error", str(exc)[:500])
        if "log" in locals():
            log.error("Cash history sync failed: %s", type(exc).__name__)
        if sys.stderr is not None:
            print(f"IBKR Cash history sync failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        db.conn.close()


if __name__ == "__main__":
    from app.updates.update_handoff import source_worker_entry
    raise SystemExit(source_worker_entry(lambda: main(sys.argv[1] if len(sys.argv) == 2 else None)))
