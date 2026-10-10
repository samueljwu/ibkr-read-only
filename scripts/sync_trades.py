"""Fetch account reports and update local trades without making IBKR orders."""

from __future__ import annotations

import sys

from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics
from app.sync.sync_service import run_flex_sync


def main(database_path=None) -> int:
    try:
        db = Database(database_path)
    except Exception:
        if sys.stderr is not None:
            print("Portfolio storage is unavailable. Open the app to resolve migration, recovery, or active-session errors.", file=sys.stderr)
        return 1
    try:
        log = configure_diagnostics(db.path, "sync-trades")
        run_flex_sync(db, db.setting("ib_account_id"), db.setting("flex_query_id"))
        return 0
    except Exception as exc:
        reason = db.metadata("ibkr_flex_sync_error") or type(exc).__name__
        if "log" in locals():
            log.error("Trade sync stopped: %s: %s", type(exc).__name__, reason)
        if sys.stderr is not None:
            print(f"IBKR trade sync failed: {reason}", file=sys.stderr)
        return 1
    finally:
        db.conn.close()


if __name__ == "__main__":
    from app.updates.update_handoff import source_worker_entry
    raise SystemExit(source_worker_entry(lambda: main(sys.argv[1] if len(sys.argv) == 2 else None)))
