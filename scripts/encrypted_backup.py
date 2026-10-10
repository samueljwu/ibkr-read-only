"""Opt-in portable SQLCipher backups. No profile credentials or plaintext staging.

The caller retains the independent recovery key offline. This reference API is
not connected to the desktop Backup button or automatic portfolio selection.
"""
import os
from pathlib import Path
import tempfile

from scripts.security_storage import connect, verify_integrity

MAX_BACKUP_BYTES = 512 * 1024 * 1024
PAGE_BYTES = 4096  # Pinned SQLCipher 4 default format.


def _validate_size(path):
    size = path.stat().st_size
    if size < PAGE_BYTES or size % PAGE_BYTES:
        raise ValueError("Backup is empty or truncated")
    if size > MAX_BACKUP_BYTES:
        raise ValueError("Backup exceeds the supported size limit")


def _publish(temporary, destination):
    # Windows rename refuses an existing destination. POSIX rename overwrites,
    # so use an atomic hardlink there and fail closed if unsupported.
    if os.name == "nt":
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)


def export_database(conn, destination, recovery_key):
    """Export one consistent committed snapshot under a separate random key."""
    if not isinstance(recovery_key, bytes) or len(recovery_key) != 32:
        raise ValueError("A random 32-byte recovery key is required")
    if conn.in_transaction:
        raise ValueError("Finish the source transaction before exporting")
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    handle, name = tempfile.mkstemp(prefix=".encrypted-backup-", suffix=".pending",
                                     dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    attached = False
    try:
        conn.execute(f'''ATTACH DATABASE ? AS recovery KEY "x'{recovery_key.hex()}'"''',
                     (str(temporary),))
        attached = True
        conn.execute("PRAGMA recovery.journal_mode=DELETE")
        conn.execute("PRAGMA recovery.synchronous=FULL")
        conn.execute("BEGIN")
        # First metadata read establishes the same source snapshot as export.
        user_version = int(conn.execute("PRAGMA main.user_version").fetchone()[0])
        application_id = int(conn.execute("PRAGMA main.application_id").fetchone()[0])
        conn.execute("SELECT sqlcipher_export('recovery')").fetchone()
        conn.execute(f"PRAGMA recovery.user_version={user_version}")
        conn.execute(f"PRAGMA recovery.application_id={application_id}")
        conn.commit()
        conn.execute("DETACH DATABASE recovery")
        attached = False
        _validate_size(temporary)
        check = connect(temporary, recovery_key, read_only=True)
        try:
            verify_integrity(check)
        finally:
            check.close()
        with temporary.open("r+b") as file:
            os.fsync(file.fileno())
        _publish(temporary, destination)
        return destination
    finally:
        if attached:
            conn.rollback()
            conn.execute("DETACH DATABASE recovery")
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-journal").unlink(missing_ok=True)


def restore_database(backup, recovery_key, destination, local_key):
    """Validate a backup read-only, then create a fresh encrypted local copy.

    The caller must validate the application schema/account and persist local
    key custody before adopting the returned path. This API never adopts it.
    """
    backup = Path(backup)
    if backup.is_symlink() or not backup.is_file():
        raise ValueError("A regular encrypted backup file is required")
    _validate_size(backup)
    source = connect(backup, recovery_key, read_only=True)
    try:
        verify_integrity(source)
        # A read-only source may attach a writable destination. query_only is
        # connection-wide, so disable it after authentication; mode=ro still
        # prevents writes to the backup itself.
        source.execute("PRAGMA query_only=OFF")
        return export_database(source, destination, local_key)
    finally:
        source.close()
