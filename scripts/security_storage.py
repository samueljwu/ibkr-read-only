"""Opt-in SQLCipher reference adapter; the desktop Database still uses SQLite.

Never substitute stdlib sqlite3 if the pinned native engine is unavailable.
Keys are binary inputs, never command-line arguments or logged SQL.
"""

from importlib.metadata import version
from pathlib import Path
import secrets

BINDING_VERSION = "0.6.3"
CIPHER_VERSION = "4.12.0 community"


def new_key():
    return secrets.token_bytes(32)


def credential_target(channel, portfolio_id):
    from uuid import UUID
    if channel not in {"stable", "dev"}:
        raise ValueError("Unknown channel")
    identity = str(UUID(portfolio_id))
    return f"custom-ib-monitor:{channel}:database-key:{identity}:v1"


def connect(path, key, *, create=False, journal_mode="WAL", read_only=False):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("A random 32-byte database key is required")
    if journal_mode not in {"WAL", "DELETE"}:
        raise ValueError("Unsupported journal mode")
    if create and read_only:
        raise ValueError("Cannot create a read-only database")
    from sqlcipher3 import dbapi2 as cipher
    if version("sqlcipher3") != BINDING_VERSION:
        raise RuntimeError("Unreviewed SQLCipher binding version")
    path = Path(path).resolve()
    # rw prevents wrong paths from silently creating an empty portfolio.
    mode = "ro" if read_only else "rwc" if create else "rw"
    conn = cipher.connect(path.as_uri() + "?mode=" + mode,
                          uri=True, timeout=30)
    try:
        conn.execute(f'''PRAGMA key = "x'{key.hex()}'"''')
        row = conn.execute("PRAGMA cipher_version").fetchone()
        if not row or row[0] != CIPHER_VERSION:
            raise RuntimeError("Unreviewed native SQLCipher engine")
        conn.execute("PRAGMA cipher_compatibility=4")
        # Optional cipher_memory_security=ON crashes this Windows wheel in QA.
        # Keep the adapter experimental; no locked/wiped-memory claim is made.
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=0")
        # Force key verification BEFORE journal changes or any schema writes.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if read_only:
            conn.execute("PRAGMA query_only=ON")
        else:
            mode = conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0]
            if mode.upper() != journal_mode:
                raise RuntimeError("Requested journal mode unavailable")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = cipher.Row
        return conn
    except BaseException:
        conn.close()
        raise


def verify_integrity(conn):
    if conn.execute("PRAGMA cipher_integrity_check").fetchall():
        raise ValueError("Encrypted database authentication failed")
    if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("Database integrity failed")
