"""Offline package verification, refused unless this channel's storage is empty."""

from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import hashlib
import sqlite3
from contextlib import closing
from unittest.mock import patch

from app.storage.database import Database
from app.resources import asset_path
from app.storage.paths import data_root
from app.platform.runtime import app_name, build_info, window_title
from app.storage.secret_store import credential_target
from app.sync.workers import worker_launch


def assert_encrypted_file(path, sentinel):
    content = Path(path).read_bytes()
    assert not content.startswith(b"SQLite format 3\0")
    assert sentinel.encode() not in content
    with closing(sqlite3.connect(str(path))) as plain:
        try:
            plain.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError:
            return
    raise AssertionError("Standard SQLite accepted an encrypted database")


def native_configuration(connection):
    import sqlcipher3._sqlite3 as engine
    from importlib.metadata import version
    expected = {"binding": "0.6.3", "cipher_version": "4.12.0 community",
                "cipher_provider": "openssl", "cipher_provider_version": "OpenSSL 3.6.0 1 Oct 2025",
                "sqlite_version": "3.51.1", "journal_mode": "delete", "temp_store": 2,
                "trusted_schema": 0, "cell_size_check": 1, "cipher_status": 1}
    actual = {name: connection.execute("PRAGMA " + name).fetchone()[0]
              for name in expected if name not in {"binding", "sqlite_version"}}
    actual.update(binding=version("sqlcipher3"), sqlite_version=connection.execute("SELECT sqlite_version()").fetchone()[0])
    actual["cipher_status"] = int(actual["cipher_status"])
    assert actual == expected, actual
    actual["threadsafe"] = next(row[0] for row in connection.execute("PRAGMA compile_options")
                                if row[0].startswith("THREADSAFE="))
    assert actual["threadsafe"] == "THREADSAFE=1"
    actual["extension_sha256"] = hashlib.sha256(Path(engine.__file__).read_bytes()).hexdigest()
    assert actual["extension_sha256"] == "de34b44f0d16078064d050c68268852946fdabd5c58aadb4c561aa365379233f"
    return actual


def verify_offline_worker_handlers(path):
    """Run real packaged entry points with local fabricated import handlers.

    This deliberately reports handler persistence separately from subprocess
    failure-path checks; it never represents acceptance of a live IBKR report.
    """
    from scripts import sync_trades, sync_analytics, sync_cash, history_worker
    results = {}
    for name, module, handler in (("trades", sync_trades, "run_flex_sync"),
                                  ("analytics", sync_analytics, "run_analytics_flex_sync"),
                                  ("cash", sync_cash, "run_cash_flex_sync")):
        def fabricated(db, *_args, name=name):
            db.set_metadata("package_qa_" + name, "synthetic committed import")
            return {"synthetic": True}
        with patch.object(module, handler, fabricated):
            assert module.main(path) == 0
        results[name] = "real entry point, fabricated offline handler"
    def fabricated_history(argv, **_kwargs):
        database = Path(argv[argv.index("--database") + 1])
        db = Database(database)
        try:
            db.set_metadata("package_qa_history", "synthetic committed import")
        finally:
            db.conn.close()
        return 0
    status = path.parent / "package-qa-history.json"
    with patch.object(history_worker, "import_history", fabricated_history):
        assert history_worker.main(["--status-file", str(status), "--database", str(path)]) == 0
    results["history"] = "real entry point, fabricated offline handler"
    db = Database(path)
    try:
        for name in results:
            assert db.metadata("package_qa_" + name) == "synthetic committed import"
    finally:
        db.conn.close()
    return results


def create_released_v1_fixture(path, sentinel):
    """Create only synthetic plaintext data with the released v1.0.0 layout.

    The schema initializer is unchanged from v1.0.0. A source regression compares
    this fixture's complete schema with that tag's actual initializer, so this
    frozen probe cannot silently substitute a newer layout for the old release.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        db = object.__new__(Database)
        db.conn, db.path = conn, path
        db._init()
        db.set_setting("ib_account_id", "SYNTHETIC")
        db.set_setting("package_qa_sentinel", sentinel)
        db.set_setting("refresh_seconds", "47")
        db.set_metadata("synthetic_history_coverage", "2026-10-01:2026-10-02")
        position = db.add_position({"display_ticker": "SYNTH", "ib_symbol": "SYNTH",
                                    "position_qty": 7, "average_cost": 123.45})
        with conn:
            conn.execute("""INSERT INTO trades(id,position_id,trade_id,trade_date,display_ticker,
                         ib_symbol,side,quantity,price,commission_usd,tax_lc,tax_usd)
                         VALUES (1,?,'synthetic-trade','2026-10-01','SYNTH','SYNTH','BUY',7,123.45,1.25,0.5,0.5)""", (position,))
            conn.execute("INSERT INTO historical_prices VALUES (401,'2026-10-01',123.45,100)")
            conn.execute("INSERT INTO exposure_facts(portfolio_id,con_id,sector) VALUES (1,401,'Synthetic')")
            conn.execute("INSERT INTO account_history VALUES (1,'2026-10-01',10000,1000,25,'flex',1)")
            conn.execute("INSERT INTO account_values VALUES (1,'NetLiquidation',10000,'USD','2026-10-01')")
            conn.execute("""INSERT INTO cash_movements VALUES
                         (1,'SYNTHETIC','synthetic-cash','2026-10-01','2026-10-01','2026-10-02',
                          'deposit','USD','100','Synthetic deposit','Cash','synthetic-hash')""")
            conn.execute("""INSERT INTO trade_fee_adjustments VALUES
                         (1,'SYNTHETIC','synthetic-refund',1,'2026-10-01','0.25','Synthetic refund','synthetic-hash')""")
    return path


def verify_legacy_migration(root, sentinel):
    """Run preserved-original upgrade through the actual frozen storage engine."""
    from app.storage.database import LegacyDatabaseError
    from app.storage.migration import migrate_legacy
    from app.storage.paths import local_config_path
    source = create_released_v1_fixture(root / "synthetic-v1" / "portfolio.sqlite3", sentinel)
    original = source.read_bytes()
    selection = local_config_path().read_bytes() if local_config_path().exists() else None
    with closing(sqlite3.connect(source)) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        expected = {table: conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
                    for table in tables}
    try:
        opened = Database(source)
    except LegacyDatabaseError:
        pass
    else:
        opened.conn.close()
        raise AssertionError("Legacy database was admitted without explicit migration")
    assert source.read_bytes() == original
    assert (local_config_path().read_bytes() if local_config_path().exists() else None) == selection
    destination = root / "synthetic-v1-upgraded" / "portfolio.sqlite3"
    migrate_legacy(source, destination, select=False)
    db = Database(destination)
    try:
        for table, rows in expected.items():
            actual = [tuple(row) for row in db.conn.execute('SELECT * FROM "' + table + '" ORDER BY rowid')]
            if table == "app_metadata":
                actual = [row for row in actual if row[0] != "portfolio_uuid"]
            assert actual == rows, table
        assert db.rows("trades")[0]["commission_usd"] == 1.0
    finally:
        db.conn.close()
    assert_encrypted_file(destination, sentinel)
    assert source.read_bytes() == original
    assert (local_config_path().read_bytes() if local_config_path().exists() else None) == selection
    return {"schema": "released v1.0.0, matched by source regression", "tables_preserved": len(expected),
            "original_sha256": hashlib.sha256(original).hexdigest(), "original_bytes_unchanged": True,
            "selection_unchanged": True, "encrypted_upgrade": True}


def verify_package(args):
    if len(args) != 1:
        return 2
    output = Path(args[0]).resolve()
    report = dict(build=build_info(), passed=False, checks=[])
    root = data_root()
    # No use of existing personal data, settings, or credentials during QA.
    if root.exists() and any(root.iterdir()):
        raise ValueError("Package verification requires an empty disposable user data root")
    output.parent.mkdir(parents=True, exist_ok=True)
    from app.storage import database_keys
    from app.platform.runtime import channel
    from app.storage.security_storage import new_key, connect
    from app.storage.recovery_code import encode_recovery_key
    from app.storage.recovery import export_portable, restore_portable, restore_local
    from app.storage.paths import create_portfolio_directory
    created = []
    save_key = database_keys.save_key
    def save_synthetic(target_channel, identity, key):
        save_key(target_channel, identity, key)
        created.append((target_channel, identity))
    database_keys.save_key = save_synthetic
    sentinel = "PACKAGE-QA-SYNTHETIC-PORTFOLIO-SENTINEL"
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from app.ui.window import MainWindow, COLUMNS
        from app.imports.excel_io import export_workbook
        from app.sync.history_status import read_status

        qt = QApplication.instance() or QApplication([])
        qt.setApplicationName(app_name())
        qt.setWindowIcon(QIcon(str(asset_path("doge.ico"))))
        assert not qt.windowIcon().isNull()
        window = MainWindow()
        try:
            assert window.db.rows("positions", 1) == []
            assert window.db.rows("trades", 1) == []
            assert window.db.setting("ib_account_id") == ""
            assert window.db.setting("flex_query_id") == ""
            assert window.windowTitle() == window_title("Main")
            assert window.db.setting("provider") == "Mock"
            report["checks"].append("empty offline GUI, identity and icon")
            window.show()
            qt.processEvents()
            assert window.grab().save(str(output.with_suffix(".png")))
            path = window.db.path
            window.db.set_setting("refresh_seconds", "47")
            window.db.set_setting("package_qa_sentinel", sentinel)
            original_identity = window.db.portfolio_uuid
            report["native_encryption"] = native_configuration(window.db.conn)
            window.db.conn.execute("BEGIN IMMEDIATE")
            window.db.conn.execute("UPDATE settings SET value='synthetic changed value' WHERE key='package_qa_sentinel'")
            journal = Path(str(path) + "-journal")
            assert journal.is_file() and sentinel.encode() not in journal.read_bytes()
            window.db.conn.rollback()
            backup = window.db.backup()
            assert backup.is_file()
            assert_encrypted_file(path, sentinel)
            assert_encrypted_file(backup, sentinel)
            portable = path.parent / "synthetic-portable.pmbackup"
            recovery_code = encode_recovery_key(new_key())
            export_portable(window.db, portable, recovery_code)
            assert_encrypted_file(portable, sentinel)
            wrong_code = encode_recovery_key(new_key())
            try:
                restore_portable(portable, wrong_code, path.parent / "wrong-key.sqlite3", select=False)
            except Exception:
                pass
            else:
                raise AssertionError("Portable backup accepted the wrong recovery key")
            assert not (path.parent / "wrong-key.sqlite3").exists()
            from app.storage import encrypted_backup
            interrupted = path.parent / "interrupted.pmbackup"
            with patch.object(encrypted_backup, "_publish", side_effect=InterruptedError("synthetic interrupted export")):
                try:
                    export_portable(window.db, interrupted, recovery_code)
                except InterruptedError:
                    pass
                else:
                    raise AssertionError("Interrupted export reported success")
            assert not interrupted.exists()
            export_workbook(path.parent / "synthetic-export.xlsx", [], [], [], COLUMNS)
            report["checks"].append("encrypted database, rollback journal, normal and portable backups; workbook export")
            report["checks"].append("wrong portable key fails; interrupted export never publishes a final backup")
        finally:
            window.close()
            qt.processEvents()
        reopened = Database()
        try:
            assert reopened.path == path
            assert reopened.setting("refresh_seconds") == "47"
            assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            reopened.conn.close()
        report["checks"].append("restart preserves database and settings")
        report["legacy_upgrade"] = verify_legacy_migration(root, sentinel)
        report["checks"].append("v1.0.0 synthetic plaintext upgrade preserves logical rows and original bytes; implicit admission refused")
        local_restored = restore_local(backup, create_portfolio_directory() / "portfolio.sqlite3", select=False)
        db = Database(local_restored)
        try:
            assert db.setting("package_qa_sentinel") == sentinel
        finally:
            db.conn.close()
        report["checks"].append("normal encrypted backup restores under independent local key")
        results = {}
        for module in ("scripts.sync_trades", "scripts.sync_analytics", "scripts.sync_cash"):
            command, cwd = worker_launch(module, [path])
            result = subprocess.run(command, cwd=cwd, timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert result.returncode == 1, (module, result.returncode)
            results[module] = result.returncode
        status = path.parent / "history-import-status.json"
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        command, cwd = worker_launch("scripts.history_worker", ["--status-file", status,
                                   "--source", "all", "--from-date", yesterday,
                                   "--to-date", yesterday, "--database", path])
        result = subprocess.run(command, cwd=cwd, timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 2
        state = read_status(status)
        assert state["state"] == "failed" and state["pid"] is None
        assert "Set the IBKR account" in state["message"]
        results["scripts.history_worker"] = result.returncode
        db = Database(path)
        try:
            for name in ("cash", "analytics"):
                assert db.metadata("ibkr_" + name + "_sync_status") == "failed"
        finally:
            db.conn.close()
        report["offline_worker_handlers"] = verify_offline_worker_handlers(path)
        report["checks"].append("subprocess workers persist failures in encrypted database; real entry points commit fabricated offline handlers")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            with closing(connect(path, new_key(), read_only=True)):
                raise AssertionError("Wrong key accepted")
        except Exception as exc:
            if isinstance(exc, AssertionError):
                raise
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        database_keys.delete_key(channel(), original_identity)
        try:
            unexpected = Database(path)
        except ValueError:
            pass
        else:
            unexpected.conn.close()
            raise AssertionError("Missing key accepted")
        recovered = restore_portable(portable, recovery_code, create_portfolio_directory() / "portfolio.sqlite3", select=False)
        db = Database(recovered)
        try:
            assert db.setting("package_qa_sentinel") == sentinel
            assert db.portfolio_uuid != original_identity
        finally:
            db.conn.close()
        report["checks"].append("wrong and missing local keys fail; portable restore succeeds without original profile key")
        report["portable_recovery_scope"] = "fresh destination and absent original credential on this Windows installation; different computer acceptance remains separate"
        report.update(database=str(path), credentials_target=credential_target("SYNTHETIC"),
                      workers=results, passed=True)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        database_keys.save_key = save_key
        cleanup_errors = []
        for target_channel, identity in reversed(created):
            try:
                database_keys.delete_key(target_channel, identity)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        report["synthetic_key_cleanup"] = {"created": len(created), "errors": cleanup_errors}
        if cleanup_errors:
            report["passed"] = False
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
