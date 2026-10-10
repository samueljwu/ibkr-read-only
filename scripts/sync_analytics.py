"""Fetch completed IBKR account analytics without a running TWS session."""

import sys

from app.imports.analytics_flex import run_analytics_flex_sync
from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics


def main(database_path=None):
    db = Database(database_path)
    log = configure_diagnostics(db.path, "sync-analytics")
    try:
        db.set_metadata("ibkr_analytics_sync_status", "running")
        log.info("Analytics history sync started")
        result = run_analytics_flex_sync(db, db.setting("ib_account_id"), db.setting("analytics_flex_query_id"))
        db.set_metadata("ibkr_analytics_sync_status", "complete")
        db.set_metadata("ibkr_analytics_sync_error", "")
        log.info("Analytics history sync complete: %s", result)
        return 0
    except Exception as exc:
        db.set_metadata("ibkr_analytics_sync_status", "failed")
        db.set_metadata("ibkr_analytics_sync_error", str(exc)[:500])
        log.error("Analytics history sync failed: %s", type(exc).__name__)
        if sys.stderr is not None:
            print(f"IBKR Analytics history sync failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        db.conn.close()


if __name__ == "__main__":
    from app.updates.update_handoff import source_worker_entry
    raise SystemExit(source_worker_entry(lambda: main(sys.argv[1] if len(sys.argv) == 2 else None)))
