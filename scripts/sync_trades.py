"""Fetch account reports and update local trades without making IBKR orders."""

from __future__ import annotations

import sys

from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics
from app.sync.sync_service import run_flex_sync


def main(database_path=None) -> int:
    db = Database(database_path)
    log = configure_diagnostics(db.path, "sync-trades")
    try:
        run_flex_sync(db, db.setting("ib_account_id"), db.setting("flex_query_id"))
        return 0
    except Exception as exc:
        reason = db.metadata("ibkr_flex_sync_error") or type(exc).__name__
        log.error("Trade sync stopped: %s: %s", type(exc).__name__, reason)
        if sys.stderr is not None:
            print(f"IBKR trade sync failed: {reason}", file=sys.stderr)
        return 1
    finally:
        db.conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else None))
