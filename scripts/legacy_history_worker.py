"""Hidden worker for validated pre-Flex CSV history imports."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import traceback

from app.platform.diagnostics import configure_diagnostics
from app.platform.history_status import read_status, write_status
from app.storage.database import Database
from app.sync.legacy_history import LegacyHistoryError, import_legacy_history, remove_legacy_history


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--reports", type=Path, nargs=2)
    operation.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    status = read_status(args.status_file)
    status.update(source="legacy", action="remove" if args.remove else "import",
                  state="running", pid=os.getpid(),
                  started_sources=["legacy"], active_source="legacy", message="")
    try:
        write_status(args.status_file, status)
    except OSError:
        return 1
    db = None
    code = 1
    try:
        db = Database(args.database)
        log = configure_diagnostics(db.path, "history-import")
        account = db.setting("ib_account_id")
        if not account:
            raise LegacyHistoryError("Apply the IBKR account in Settings first.")
        if args.remove:
            log.info("Older CSV removal started")
            result = remove_legacy_history(db, account)
            status.update(state="completed", legacy_result=result,
                          message="Saved older CSV history removed. Original files remain on disk.")
            log.info("Older CSV removal completed: months=%s statements=%s openings=%s",
                     result["months"], result["statements"], result["opening_positions"])
        else:
            log.info("Older CSV import started")
            result = import_legacy_history(db, account, *args.reports)
            status.update(state="completed", legacy_result=result,
                          message="Monthly history, closing stock quantities, and reported return saved.")
            log.info("Older CSV import completed: months=%s new_months=%s evidence_rows=%s",
                     result["months"], result["new_months"], result["evidence_rows"])
        code = 0
    except LegacyHistoryError as exc:
        status.update(state="needs_review", message=str(exc))
        if db is not None:
            configure_diagnostics(db.path, "history-import").warning(
                "Older CSV operation needs review: reason=%s", type(exc).__name__)
    except Exception as exc:
        status.update(state="failed", message=(
            "Could not remove saved older CSV history; check the local data folder."
            if args.remove else
            "Older CSV import failed; check the selected files and local data folder."))
        if db is not None:
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            configure_diagnostics(db.path, "history-import").error(
                "Older CSV operation failed: error=%s location=%s:%s",
                type(exc).__name__, Path(frame.filename).name, frame.lineno)
    finally:
        if db is not None:
            db.conn.close()
        status.update(pid=None, active_source=None,
                      finished_at=datetime.now().isoformat(timespec="seconds"))
        try:
            write_status(args.status_file, status)
        except OSError:
            return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
