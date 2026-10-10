"""Make a disposable SQLite copy for debugging without changing the source."""

import argparse
from pathlib import Path
from app.storage.paths import create_portfolio_directory, validate_database_path
from app.storage.migration import copy_portfolio
from sqlcipher3.dbapi2 import Error as DatabaseError


def copy_database(source, destination, source_channel=None):
    # Explicit read-only import may copy legacy/stable data into the active
    # channel. The destination must always stay inside the active channel.
    source = Path(source).expanduser().resolve()
    destination = validate_database_path(destination)
    if not source.is_file():
        raise FileNotFoundError("The source database does not exist")
    return copy_portfolio(source, destination, source_channel=source_channel)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-channel", choices=("dev", "stable"),
                        help="Explicit custody channel for an encrypted cross-channel source")
    parser.add_argument("--destination", type=Path,
                        help="SQLite path within this channel's data root; omitted creates a new portfolio ID")
    args = parser.parse_args(argv)
    try:
        destination = args.destination
        if destination is None:
            source = args.source.expanduser().resolve()
            if not source.is_file():
                raise FileNotFoundError("The source database does not exist")
            destination = create_portfolio_directory() / "portfolio.sqlite3"
        print(copy_database(args.source, destination, args.source_channel))
    except (OSError, RuntimeError, ValueError, DatabaseError) as exc:
        parser.exit(1, f"Copy failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
