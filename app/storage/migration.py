"""Explicit preserved-original imports and encrypted portfolio publication.

Legacy WAL/hot-journal sources are refused: close the old application first so
it completes recovery/checkpointing. We never checkpoint or recover an original.
"""
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.platform.runtime import channel
from app.storage import database_keys
from app.storage.database import (Database, APPLICATION_ID, SCHEMA_VERSION, identity_path,
                                  read_identity, write_identity, validate_schema, validate_schema_objects)
from app.storage.encrypted_backup import export_database, _publish
from app.storage.paths import create_portfolio_directory, validate_database_path, select_data_directory
from app.storage.security_storage import connect, verify_integrity
from app.storage.storage_operations import SessionLease, maintenance, record_operation, retire


# Older Dev portfolios retained these exposure-cache fields after the feature
# was removed. Preserve that exact layout through migration and later encrypted
# copies/backups; do not add the fields to new portfolios or accept other extras.
# Values are PRAGMA table_xinfo: type, notnull, default, primary-key rank, hidden.
HISTORICAL_EXPOSURE_COLUMNS = {
    "market_cap_usd": ("REAL", 0, None, 0, 0),
    "cap_as_of": ("TEXT", 1, "''", 0, 0),
}


def validate_application(conn, *, legacy=False):
    """Validate a known schema before any initialization or adoption writes."""
    from sqlcipher3 import dbapi2 as cipher
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    schema = conn.execute("PRAGMA user_version").fetchone()[0]
    if (app_id, schema) != ((0, 0) if legacy else (APPLICATION_ID, SCHEMA_VERSION)):
        raise ValueError("Unsupported database application or schema version")
    validate_schema_objects(conn, allow_legacy=legacy)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise ValueError("Unsupported executable database schema")
    if any("CREATE VIRTUAL TABLE" in (r[0] or "").upper() for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='table'")):
        raise ValueError("Unsupported virtual database schema")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    required = {"portfolios": {"id", "name", "nav_usd"},
                "positions": {"id", "portfolio_id", "display_ticker", "ib_symbol", "security_type", "position_qty", "average_cost"},
                "trades": {"id", "portfolio_id", "trade_id", "trade_date", "side", "quantity", "price"},
                "settings": {"key", "value"}, "app_metadata": {"key", "value"}}
    if not set(required).issubset(tables):
        raise ValueError("Unrecognized portfolio schema")
    # Build the accepted column inventory from this version's trusted schema.
    # The template is memory-only and receives no user data or credentials.
    with closing(cipher.connect(":memory:")) as template:
        template.execute("PRAGMA temp_store=MEMORY")
        empty = object.__new__(Database)
        empty.conn = template
        empty._init()
        known = {r[0] for r in template.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables - known:
            raise ValueError("Unrecognized portfolio tables")
        if not legacy and tables != known:
            raise ValueError("Incomplete portfolio schema")
        for table in tables:
            # Include generated/hidden columns so they cannot evade the inventory.
            details = {r[1]: tuple(r[2:]) for r in conn.execute('PRAGMA table_xinfo("' + table + '")')}
            if any(definition[-1] != 0 for definition in details.values()):
                raise ValueError(f"Unrecognized portfolio columns in {table}")
            columns = set(details)
            accepted = {r[1] for r in template.execute('PRAGMA table_info("' + table + '")')}
            extra = columns - accepted
            if extra and (table != "exposure_facts" or extra != set(HISTORICAL_EXPOSURE_COLUMNS)
                          or any(details[name] != definition for name, definition in HISTORICAL_EXPOSURE_COLUMNS.items())):
                raise ValueError(f"Unrecognized portfolio columns in {table}")
            if not required.get(table, set()).issubset(columns) or (not legacy and columns - extra != accepted):
                raise ValueError(f"Unrecognized portfolio columns in {table}")
    if [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("Portfolio integrity check failed")
    # Validate existing brokerage provenance without changing settings.
    check = object.__new__(Database)
    check.conn = conn
    check.validate_account_change(check.setting("ib_account_id"))
    return {table: conn.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] for table in tables}


@contextmanager
def _legacy_source(source):
    """Deny concurrent Windows writers and refuse recovery-bearing originals."""
    source = Path(source).expanduser().absolute()
    if source.is_symlink() or not source.is_file() or source.resolve() != source:
        raise ValueError("A regular legacy database without path aliases is required")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Close the old application and complete its checkpoint/recovery before migrating; original sidecars were preserved")
    if os.name != "nt":
        raise RuntimeError("Preserved-original legacy migration requires Windows file-sharing guarantees")
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(source), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError("Close every application using the legacy portfolio before migrating")
    try:
        from sqlcipher3 import dbapi2 as cipher
        with closing(cipher.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = cipher.Row
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=0")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA cell_size_check=ON")
            conn.enable_load_extension(False)
            validate_application(conn, legacy=True)
            yield conn
    finally:
        api.CloseHandle(handle)


def install_from_authenticated_source(source_conn, destination=None, *, select=True, legacy=False):
    """Install a validated source under a newly reserved UUID and local key.

    Caller keeps source authenticated and quiescent for this entire operation.
    Incomplete/published but unselected artifacts retain a nonsecret receipt.
    """
    validate_application(source_conn, legacy=legacy)
    destination = validate_database_path(destination or create_portfolio_directory() / "portfolio.sqlite3")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or identity_path(destination).exists():
        raise FileExistsError("The destination portfolio already exists")
    operation = str(uuid4())
    identity = {"version": 1, "channel": channel(), "portfolio_uuid": str(uuid4())}
    details = {"kind": "legacy-migration" if legacy else "copy", "destination": str(destination),
               "portfolio_uuid": identity["portfolio_uuid"], "retained_plaintext": legacy}
    source_name = source_conn.execute("PRAGMA database_list").fetchone()[2]
    if source_name:
        details["source"] = str(Path(source_name))
    temporary = destination.with_name(".migration-" + uuid4().hex + ".pending")
    published = False
    with maintenance(destination):
        if destination.exists() or identity_path(destination).exists():
            raise FileExistsError("The destination portfolio already exists")
        try:
            write_identity(destination, identity)
            key = database_keys.create_key(channel(), identity["portfolio_uuid"])
            record_operation(destination.parent, operation, "reserved", **details)
            source_conn.execute("PRAGMA query_only=OFF")
            export_database(source_conn, temporary, key)
            with closing(connect(temporary, key, journal_mode="DELETE")) as candidate:
                counts = validate_application(candidate, legacy=legacy)
                # Preserve a verified encrypted snapshot before schema upgrades.
                pre_upgrade = destination.parent / ("pre-upgrade-" + operation + ".sqlite3")
                export_database(candidate, pre_upgrade, key)
                with candidate:
                    candidate.execute("INSERT OR REPLACE INTO app_metadata(key,value) VALUES ('portfolio_uuid',?)", (identity["portfolio_uuid"],))
                    candidate.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    candidate.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    candidate.execute("UPDATE settings SET value='' WHERE key IN ('data_directory','backup_directory')")
                    if not legacy or not select:
                        candidate.execute("UPDATE settings SET value='' WHERE key IN ('flex_query_id','analytics_flex_query_id','cash_flex_query_id')")
                        candidate.execute("UPDATE settings SET value='0' WHERE key='flex_auto_sync_enabled'")
                        candidate.execute("UPDATE settings SET value='Mock' WHERE key='provider'")
                upgrade = object.__new__(Database)
                upgrade.conn, upgrade.path = candidate, temporary
                upgrade.portfolio_uuid, upgrade._key = identity["portfolio_uuid"], key
                upgrade._pre_upgrade_snapshot = pre_upgrade
                upgrade._init()
                for table, count in counts.items():
                    if table != "app_metadata" and candidate.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0] != count:
                        raise ValueError("Migration row-count verification failed")
                validate_schema(candidate, identity["portfolio_uuid"])
                validate_application(candidate)
                verify_integrity(candidate)
            with temporary.open("r+b") as file:
                os.fsync(file.fileno())
            record_operation(destination.parent, operation, "verified", **details)
            _publish(temporary, destination)
            published = True
            record_operation(destination.parent, operation, "published", **details)
            if select:
                select_data_directory(destination.parent)
            record_operation(destination.parent, operation, "adopted" if select else "published", **details)
            return destination
        except BaseException:
            if not published:
                # Retain encrypted pre-upgrade artifacts together with custody.
                # A failed candidate cannot be selected or opened as a portfolio.
                record_operation(destination.parent, operation, "interrupted", **details)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + "-journal").unlink(missing_ok=True)


def migrate_legacy(source, destination=None, *, select=True):
    with _legacy_source(source) as conn:
        return install_from_authenticated_source(conn, destination, select=select, legacy=True)


def copy_portfolio(source, destination, *, source_channel=None):
    source = Path(source).expanduser().absolute()
    if source.is_symlink() or source != source.resolve():
        raise ValueError("Source database path aliases are unsupported")
    with source.open("rb") as file:
        plain = file.read(16) == b"SQLite format 3\x00"
    if plain:
        return migrate_legacy(source, destination, select=False)
    if source_channel is None:
        identity = read_identity(source)
    else:
        # Explicit cross-channel copies resolve exactly the supplied namespace.
        if source_channel not in {"dev", "stable"}:
            raise ValueError("Unknown source channel")
        sidecar = identity_path(source)
        if sidecar.is_symlink() or not sidecar.is_file() or sidecar.stat().st_size > 1024:
            raise ValueError("Unsupported source identity")
        identity = json.loads(sidecar.read_text(encoding="utf-8"))
        if set(identity) != {"version", "channel", "portfolio_uuid"} or identity["version"] != 1 or identity["channel"] != source_channel:
            raise ValueError("Unsupported source identity")
        if str(UUID(identity["portfolio_uuid"])) != identity["portfolio_uuid"]:
            raise ValueError("Unsupported source identity")
    key = database_keys.load_key(identity["channel"], identity["portfolio_uuid"])
    if key is None:
        raise ValueError("Source database key is missing")
    with SessionLease(source), closing(connect(source, key, read_only=True)) as conn:
        validate_schema(conn, identity["portfolio_uuid"])
        verify_integrity(conn)
        return install_from_authenticated_source(conn, destination, select=False)


def relocate_portfolio(db, directory):
    from app.storage.paths import validate_data_directory
    destination = validate_data_directory(directory or db.path.parent) / "portfolio.sqlite3"
    if destination == db.path:
        select_data_directory(destination.parent)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    operation = str(uuid4())
    details = {"kind": "relocation", "source": str(db.path), "destination": str(destination),
               "portfolio_uuid": db.portfolio_uuid}
    # Source then destination is the global maintenance acquisition order.
    # Fail-fast gate acquisition ensures opposite-direction attempts cannot deadlock.
    with maintenance(db.path, owner=db._lease), maintenance(destination):
        if destination.exists() or identity_path(destination).exists():
            raise FileExistsError("The destination portfolio already exists")
        record_operation(destination.parent, operation, "reserved", **details)
        write_identity(destination, read_identity(db.path))
        export_database(db.conn, destination, db._key)
        with closing(connect(destination, db._key, read_only=True)) as candidate:
            validate_schema(candidate, db.portfolio_uuid)
            validate_application(candidate)
            verify_integrity(candidate)
        record_operation(destination.parent, operation, "verified", **details)
        try:
            retire(db.path.parent, destination)
        except BaseException:
            if (db.path.parent / ".storage-retired.json").exists():
                # An interruption after atomic retirement must not leave the
                # old session able to commit data absent from the snapshot.
                db._relocation_retired = True
                db.conn.close()
            raise
        db._relocation_retired = True
        try:
            select_data_directory(destination.parent)
        except BaseException:
            retired = db.path.parent / ".storage-retired.json"
            try:
                retired.unlink()
            finally:
                db._relocation_retired = retired.exists()
                if db._relocation_retired:
                    db.conn.close()
            raise
        db._relocation_selected = True
        try:
            record_operation(destination.parent, operation, "adopted", **details)
        finally:
            # Selection already succeeded. Even a receipt-write failure must
            # release the old writable session under the retired identity.
            db.conn.close()
    return destination


def recover_operation(receipt):
    """Explicitly adopt a finalized artifact after reauthentication on restart.

    Pending/interrupted candidates are never promoted. For a relocation whose
    selection write was interrupted, retiring the source precedes adoption.
    """
    receipt = Path(receipt).resolve()
    operation = receipt.name.removeprefix(".storage-operation-").removesuffix(".json")
    if str(UUID(operation)) != operation:
        raise ValueError("Unsupported storage operation receipt name")
    if receipt.stat().st_size > 4096:
        raise ValueError("Unsupported storage operation receipt")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    allowed = {"version", "phase", "kind", "source", "destination", "portfolio_uuid", "retained_plaintext"}
    if not isinstance(data, dict) or set(data) - allowed or data.get("version") != 1:
        raise ValueError("Unsupported storage operation receipt")
    if data.get("phase") not in {"verified", "published", "adopted"}:
        raise ValueError("The operation has no finalized verified portfolio to recover")
    if data.get("kind") not in {"relocation", "copy", "legacy-migration"}:
        raise ValueError("Unsupported storage operation kind")
    destination = validate_database_path(data["destination"])
    if receipt.parent != destination.parent or not destination.is_file():
        raise ValueError("The finalized portfolio is unavailable")
    identity = read_identity(destination)
    if identity["portfolio_uuid"] != data["portfolio_uuid"]:
        raise ValueError("Operation identity differs from the finalized portfolio")
    key = database_keys.load_key(identity["channel"], identity["portfolio_uuid"])
    if key is None:
        raise ValueError("Database key is missing; use portable recovery")
    with ExitStack() as held:
        if data.get("kind") == "relocation":
            source = validate_database_path(data["source"])
            held.enter_context(maintenance(source))
            if source == destination or read_identity(source) != identity:
                raise ValueError("Relocation source identity differs from its destination")
            retired = source.parent / ".storage-retired.json"
            if not retired.is_file() or retired.stat().st_size > 4096:
                raise ValueError("Unretired relocation may be stale; continue with the original and move to a fresh destination")
            retired_data = json.loads(retired.read_text(encoding="utf-8"))
            if retired_data != {"version": 1, "destination": str(destination)}:
                raise ValueError("Retired relocation destination differs from this operation")
        held.enter_context(maintenance(destination))
        with closing(connect(destination, key, read_only=True)) as conn:
            validate_schema(conn, identity["portfolio_uuid"])
            validate_application(conn)
            verify_integrity(conn)
        select_data_directory(destination.parent)
    record_operation(destination.parent, operation, "adopted",
                     **{key: value for key, value in data.items() if key not in {"version", "phase"}})
    return destination
