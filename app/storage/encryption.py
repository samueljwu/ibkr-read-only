"""SQLCipher connections and verified encrypted copies."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import re
from secrets import token_hex
from uuid import uuid4

from sqlcipher3 import dbapi2 as sqlite3

from app.platform import secret_store


SQLITE_HEADER = b"SQLite format 3\x00"


class MissingDatabaseKey(ValueError):
    """An encrypted database needs its recovery code on this Windows profile."""


class InvalidDatabaseKey(ValueError):
    """The supplied or stored recovery code could not open the database."""


def is_plaintext(path) -> bool:
    with Path(path).open("rb") as source:
        return source.read(16) == SQLITE_HEADER


def _valid_key(key: str) -> str:
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
        raise ValueError("Database recovery code must be 64 hexadecimal characters")
    return key.lower()


def _apply_key(connection, key: str):
    # The validated hex is a SQLCipher raw key, not a passphrase or a SQL value.
    connection.execute(f"PRAGMA key = \"x'{_valid_key(key)}'\"")
    if not connection.execute("PRAGMA cipher_version").fetchone():
        raise RuntimeError("The SQLite library does not include SQLCipher")
    connection.execute("PRAGMA temp_store=MEMORY")


def open_database(path, *, key=None, readonly=False, timeout=30):
    """Open a portfolio without ever treating a missing key as a new database."""
    path = Path(path)
    exists = path.is_file()
    supplied_key = key is not None
    if readonly and not exists:
        raise FileNotFoundError(path)
    plain = exists and is_plaintext(path)
    if plain and key is not None:
        raise ValueError("A plaintext database cannot be opened with a recovery code")
    if not plain:
        key = _valid_key(key) if key is not None else secret_store.load_database_key(path)
        if key is None:
            if exists:
                raise MissingDatabaseKey(f"Recovery code required for {path}")
            key = token_hex(32)
        else:
            key = _valid_key(key)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    location = path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
    connection = sqlite3.connect(location, uri=readonly, timeout=timeout)
    try:
        if not plain:
            _apply_key(connection, key)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if not plain and not exists and not readonly:
            # Force the encrypted file to be created before storing its key.
            connection.execute("CREATE TABLE IF NOT EXISTS _cipher_init (id INTEGER)")
            connection.execute("DROP TABLE _cipher_init")
            connection.commit()
            if not supplied_key:
                secret_store.save_database_key(path, key)
        return connection
    except sqlite3.DatabaseError as exc:
        connection.close()
        if not exists and not supplied_key:
            path.unlink(missing_ok=True)
        if not plain:
            raise InvalidDatabaseKey("Database recovery code is incorrect or the file is damaged") from exc
        raise
    except Exception:
        connection.close()
        if not exists and not supplied_key:
            path.unlink(missing_ok=True)
        raise


def encrypt_plaintext(source, destination, key: str):
    """Export a quiescent plaintext database to a new encrypted file."""
    source, destination = Path(source), Path(destination)
    if not is_plaintext(source):
        raise ValueError("The source is not a plaintext SQLite database")
    if destination.exists():
        raise FileExistsError(destination)
    key = _valid_key(key)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("PRAGMA temp_store=MEMORY")
        original.execute("SELECT count(*) FROM sqlite_master").fetchone()
        try:
            original.execute(f"ATTACH DATABASE ? AS encrypted KEY \"x'{key}'\"", (str(destination),))
            original.execute("SELECT sqlcipher_export('encrypted')").fetchone()
            version = original.execute("PRAGMA main.user_version").fetchone()[0]
            original.execute(f"PRAGMA encrypted.user_version={version}")
            original.execute("DETACH DATABASE encrypted")
            with closing(open_database(destination, key=key, readonly=True)) as encrypted:
                if encrypted.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Encrypted database failed integrity check")
        except Exception:
            destination.unlink(missing_ok=True)
            raise


def encrypted_copy(source, destination, key: str):
    """Create an encrypted backup or move without a plaintext intermediate."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = destination.with_name(f".{destination.name}-{uuid4().hex}.tmp")
    key = _valid_key(key)
    try:
        if is_plaintext(source):
            encrypt_plaintext(source, temporary, key)
        else:
            with closing(open_database(temporary, key=key)) as copied:
                with closing(open_database(source, key=key, readonly=True)) as original:
                    original.backup(copied)
        with closing(open_database(temporary, key=key, readonly=True)) as verified:
            if verified.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Encrypted copy failed integrity check")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def migrate_in_place(path):
    """Replace a plaintext portfolio after verifying its encrypted copy."""
    path = Path(path)
    if not is_plaintext(path):
        raise ValueError("Portfolio is already encrypted or is not a readable SQLite database")
    key = secret_store.load_database_key(path)
    if key is None:
        raise MissingDatabaseKey("Save a recovery code before starting migration")
    key = _valid_key(key)
    temporary = path.with_name(f".{path.stem}-{uuid4().hex}.sqlite3")
    retained = path.with_name(f"{path.stem}-plaintext-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.sqlite3")
    try:
        encrypt_plaintext(path, temporary, key)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(open_database(temporary, key=key, readonly=True)) as encrypted:
                tables = [row[0] for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                if {row[0] for row in encrypted.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} != set(tables):
                    raise RuntimeError("Encrypted copy has different tables")
                for name in tables:
                    quoted = '"' + name.replace('"', '""') + '"'
                    if source.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0] != encrypted.execute(
                            f"SELECT count(*) FROM {quoted}").fetchone()[0]:
                        raise RuntimeError("Encrypted copy has different row counts")
                if encrypted.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Encrypted copy failed foreign key check")
        # Preserve a consistent plaintext copy before the single atomic switch.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(retained)) as preserved:
                source.backup(preserved)
                if preserved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Plaintext recovery copy failed integrity check")
        os.replace(temporary, path)
        return retained
    finally:
        temporary.unlink(missing_ok=True)
