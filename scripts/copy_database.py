"""Make a disposable SQLite copy for debugging without changing the source."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def copy_database(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("The source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the path exclusively; never overwrite a previous copy or source.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copied:
                original.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Copied database failed its integrity check")
                # A debug copy must not move itself or write backups to the
                # original database's configured folders.
                with copied:
                    copied.execute("UPDATE settings SET value='' WHERE key IN "
                                   "('data_directory', 'backup_directory')")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(copy_database(args.source, args.destination))
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Copy failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
