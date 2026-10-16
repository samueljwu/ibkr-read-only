"""The portfolio, backups and migration must preserve data without plaintext copies."""

from contextlib import closing
import os
import sqlite3

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from app.storage.database import Database
import app.storage.encryption as encryption
from app.storage.encryption import (InvalidDatabaseKey, MissingDatabaseKey,
                                    is_plaintext, migrate_in_place)


def test_new_database_and_backup_are_encrypted(tmp_path, synthetic_database_keys):
    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    key = db.recovery_code
    assert key and len(key) == 64
    try:
        db.add_position({"display_ticker": "SYNTHETIC", "ib_symbol": "SYNTHETIC"})
        backup = db.backup()
    finally:
        db.conn.close()
    assert not is_plaintext(path)
    assert not is_plaintext(backup)
    assert b"SYNTHETIC" not in path.read_bytes()
    assert b"SYNTHETIC" not in backup.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        with closing(sqlite3.connect(path)) as ordinary:
            ordinary.execute("SELECT * FROM positions").fetchall()
    synthetic_database_keys.clear()
    with pytest.raises(MissingDatabaseKey):
        Database(path)
    with pytest.raises(InvalidDatabaseKey):
        Database(path, recovery_key="0" * 64)
    restored = Database(backup, recovery_key=key)
    try:
        assert restored.rows("positions", 1)[0]["ib_symbol"] == "SYNTHETIC"
    finally:
        restored.conn.close()


def test_plaintext_migration_preserves_source_and_encrypted_backup(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE synthetic_source (id INTEGER)")
    legacy = Database(path)
    legacy.add_position({"display_ticker": "SYNTHETIC", "ib_symbol": "SYNTHETIC"})
    legacy.conn.close()
    assert is_plaintext(path)
    db = Database(path)
    backup = db.backup()
    db.conn.close()
    assert not is_plaintext(backup)
    retained = migrate_in_place(path)
    assert is_plaintext(retained)
    assert not is_plaintext(path)
    reopened = Database(path)
    try:
        assert reopened.rows("positions", 1)[0]["ib_symbol"] == "SYNTHETIC"
    finally:
        reopened.conn.close()


def test_gui_migration_uses_worker_and_reopens_portfolio(tmp_path, monkeypatch):
    import app.ui as ui
    from scripts import encrypt_database

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "portfolio.sqlite3"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE synthetic_source (id INTEGER)")
    window = ui.MainWindow(database_path=path)
    monkeypatch.setattr(window, "show_database_recovery_code", lambda: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)

    class Finished:
        def __init__(self, returncode):
            self.returncode = returncode

        def poll(self):
            return self.returncode

    def run_worker(command, **_kwargs):
        args = command[3:] if command[1:3] == ["-m", "scripts.encrypt_database"] else command[2:]
        return Finished(encrypt_database.main(args))

    monkeypatch.setattr(ui.subprocess, "Popen", run_worker)
    try:
        window.encrypt_database()
        window._poll_encryption()
        assert window.db.encrypted
        assert not is_plaintext(path)
        assert list(tmp_path.glob("portfolio-plaintext-*.sqlite3"))
    finally:
        window.close()
    assert app is not None


def test_interrupted_switch_keeps_plaintext_portfolio(tmp_path, monkeypatch):
    path = tmp_path / "portfolio.sqlite3"
    with closing(sqlite3.connect(path)) as source:
        source.execute("CREATE TABLE synthetic_source (id INTEGER)")
    db = Database(path)
    db.backup()
    db.conn.close()
    replace = encryption.os.replace

    def interrupted(source, destination):
        if destination == path:
            raise OSError("synthetic interrupted switch")
        return replace(source, destination)

    monkeypatch.setattr(encryption.os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted switch"):
        migrate_in_place(path)
    assert is_plaintext(path)
    assert list(tmp_path.glob("portfolio-plaintext-*.sqlite3"))
