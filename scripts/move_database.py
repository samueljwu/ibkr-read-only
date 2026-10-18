"""Move a closed portfolio in a hidden worker, then report its new path."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from app.storage.database import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    args = parser.parse_args(argv)
    state = {"state": "failed"}
    code = 1
    try:
        db = Database(args.database)
        try:
            destination = db.configure_data_directory(args.directory)
        finally:
            db.conn.close()
        state = {"state": "completed", "database": str(destination)}
        code = 0
    except Exception:
        pass

    temporary = args.status_file.with_name(f".{args.status_file.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, args.status_file)
    except OSError:
        temporary.unlink(missing_ok=True)
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
