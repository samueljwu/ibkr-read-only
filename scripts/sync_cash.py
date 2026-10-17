"""Fetch and validate completed IBKR cash activity through Flex."""

import sys

from app.flex.cash_flex import run_cash_flex_sync
from app.flex.flex_import import safe_flex_error
from app.storage.database import Database
from app.platform.diagnostics import configure_diagnostics


def main(database_path=None):
    db = Database(database_path)
    log = configure_diagnostics(db.path, "sync-cash")
    try:
        db.set_metadata("ibkr_cash_sync_status", "running")
        log.info("Cash history sync started")
        result = run_cash_flex_sync(db, db.setting("ib_account_id"), db.setting("cash_flex_query_id"))
        db.set_metadata("ibkr_cash_sync_status", "complete")
        db.set_metadata("ibkr_cash_sync_error", "")
        log.info("Cash history sync complete: %s", result)
        return 0
    except Exception as exc:
        message = safe_flex_error(exc)
        db.set_metadata("ibkr_cash_sync_status", "failed")
        db.set_metadata("ibkr_cash_sync_error", message)
        log.error("Cash history sync failed: %s: %s", type(exc).__name__, message)
        if sys.stderr is not None:
            print(f"IBKR Cash history sync failed: {message}", file=sys.stderr)
        return 1
    finally:
        db.conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) == 2 else None))
