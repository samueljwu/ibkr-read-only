"""Encrypt one closed portfolio in the existing hidden worker process pattern."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from app.storage.encryption import migrate_in_place


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    args = parser.parse_args(argv)
    state = {"state": "failed", "message": "Encryption failed; the original database was preserved."}
    try:
        old_copy = migrate_in_place(args.database)
        state = {"state": "completed", "plaintext_copy": str(old_copy)}
        code = 0
    except Exception:
        code = 1
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
