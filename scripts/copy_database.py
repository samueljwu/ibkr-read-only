"""Make a disposable SQLite copy for debugging without changing the source."""

import argparse
from contextlib import closing
from pathlib import Path
from secrets import token_hex
from sqlcipher3 import dbapi2 as sqlite3

from app.platform import secret_store
from app.storage.encryption import encrypted_copy, open_database


def copy_database(source, destination):
    source = Path(source).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("The source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    key = secret_store.load_database_key(source) or token_hex(32)
    try:
        encrypted_copy(source, destination, key)
        with closing(open_database(destination, key=key)) as copied:
            # A debug copy must not move itself or write backups to the
            # original database's configured folders.
            with copied:
                copied.execute("UPDATE settings SET value='' WHERE key IN "
                               "('data_directory', 'backup_directory')")
        secret_store.save_database_key(destination, key)
        secret_store.save_database_key(source, key)
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
