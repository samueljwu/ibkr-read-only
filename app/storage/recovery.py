"""Profile-independent encrypted recovery and profile-bound local restore."""

from contextlib import closing
from pathlib import Path

from app.platform.runtime import channel
from app.storage import database_keys
from app.storage.database import read_identity, validate_schema, identity_path
from app.storage.encrypted_backup import export_database, _validate_size
from app.storage.migration import install_from_authenticated_source, validate_application
from app.storage.paths import create_portfolio_directory, data_root
from app.storage.recovery_code import decode_recovery_key
from app.storage.security_storage import connect, verify_integrity
from app.storage.storage_operations import SessionLease


def _backup_path(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Choose a regular encrypted backup file")
    _validate_size(path)
    return path


def _account(source):
    row = source.execute("SELECT value FROM settings WHERE key='ib_account_id'").fetchone()
    return row[0] if row else ""


def inspect_portable(backup, code):
    with closing(connect(_backup_path(backup), decode_recovery_key(code), read_only=True)) as source:
        verify_integrity(source)
        validate_application(source)
        return _account(source)


def inspect_local(backup):
    backup = _backup_path(backup)
    identity = read_identity(backup)
    key = database_keys.load_key(identity["channel"], identity["portfolio_uuid"])
    if key is None:
        raise ValueError("This local backup needs its original Windows profile key; use a portable backup and recovery code")
    with closing(connect(backup, key, read_only=True)) as source:
        validate_schema(source, identity["portfolio_uuid"])
        validate_application(source)
        verify_integrity(source)
        return _account(source)


def restore_portable(backup, code, destination=None, *, select=True, expected_account=None):
    key = decode_recovery_key(code)
    with closing(connect(_backup_path(backup), key, read_only=True)) as source:
        verify_integrity(source)
        validate_application(source)
        if expected_account is not None and _account(source) != expected_account:
            raise ValueError("The backup account changed; choose the backup again")
        return install_from_authenticated_source(source, destination, select=select)


def restore_local(backup, destination=None, *, select=True, expected_account=None):
    backup = _backup_path(backup)
    identity = read_identity(backup)
    key = database_keys.load_key(identity["channel"], identity["portfolio_uuid"])
    if key is None:
        raise ValueError("This local backup needs its original Windows profile key; use a portable backup and recovery code")
    with closing(connect(backup, key, read_only=True)) as source:
        verify_integrity(source)
        validate_schema(source, identity["portfolio_uuid"])
        validate_application(source)
        if expected_account is not None and _account(source) != expected_account:
            raise ValueError("The backup account changed; choose the backup again")
        return install_from_authenticated_source(source, destination, select=select)


def export_portable(db, destination, code):
    """Publish a committed snapshot, then test recovery into a separate portfolio.

    The caller must confirm custody before invoking this function. A failed test
    never reports success, and any finalized export remains available for retry.
    """
    key = decode_recovery_key(code)
    if db.conn.in_transaction:
        raise ValueError("Finish the current transaction before exporting")
    with SessionLease(db.path), closing(connect(db.path, db._key, read_only=True)) as source:
        validate_schema(source, db.portfolio_uuid)
        validate_application(source)
        source.execute("PRAGMA query_only=OFF")
        result = export_database(source, destination, key)
    directory = create_portfolio_directory()
    test_path = directory / "portfolio.sqlite3"
    restored = None
    try:
        restored = restore_portable(result, code, test_path, select=False)
        identity = read_identity(restored)
        test_key = database_keys.load_key(channel(), identity["portfolio_uuid"])
        if test_key is None or test_key == db._key or test_key == key:
            raise ValueError("Recovery did not establish independent local key custody")
        with closing(connect(restored, test_key, read_only=True)) as verified:
            validate_schema(verified, identity["portfolio_uuid"])
            verify_integrity(verified)
        return result
    finally:
        # This directory was reserved exclusively above and never selected.
        # Retain failed operations for diagnosis; remove only a successful test.
        if restored is not None:
            _discard_test_portfolio(directory, restored)


def _discard_test_portfolio(directory, path):
    directory = Path(directory).resolve()
    root = data_root().resolve()
    if directory == root or not directory.is_relative_to(root) or path.parent.resolve() != directory:
        raise ValueError("Recovery test cleanup escaped its reserved directory")
    identity = read_identity(path)
    # Refuse unexpected contents rather than deleting user-provided files.
    files = list(directory.iterdir())
    def owned(item):
        return (item.name in {path.name, identity_path(path).name, ".storage-admission.lock"}
                or item.name.startswith(("pre-upgrade-", ".storage-operation-", ".storage-session-")))
    if any(item.is_symlink() or not item.is_file() or not owned(item) for item in files):
        raise ValueError("Recovery test cleanup encountered unexpected files")
    database_keys.delete_key(identity["channel"], identity["portfolio_uuid"])
    for item in files:
        item.unlink()
    directory.rmdir()
