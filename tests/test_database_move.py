"""Moving or replacing a portfolio file while using the dashboard."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

import app.ui as ui
from app.storage.database import Database
from app.storage.encryption import open_database
from scripts import move_database


class Finished:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


@pytest.mark.parametrize("encrypted", (False, True))
def test_apply_settings_uses_database_replaced_at_same_path(tmp_path, monkeypatch, encrypted):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / "portfolio.sqlite3"
    key = "ab" * 32 if encrypted else None
    if not encrypted:
        with closing(sqlite3.connect(source)) as plain:
            plain.execute("PRAGMA user_version=1")
    with closing(Database(source, recovery_key=key).conn):
        pass
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window = ui.MainWindow(database_path=source)
    try:
        old_connection = window.db.conn
        replacement = tmp_path / "replacement.sqlite3"
        shutil.copyfile(source, replacement)
        with closing(open_database(replacement, key=key, create=False)) as updated:
            with updated:
                updated.execute("UPDATE settings SET value='45' WHERE key='refresh_seconds'")
        try:
            os.replace(replacement, source)
        except PermissionError:
            if os.name != "nt":
                raise
            pytest.skip("Windows reports a sharing violation for the open database file")

        assert window.db.setting("refresh_seconds") == "30"
        window.settings_fields["ib_port"].setText("4001")
        apply_button = next(button for button in window.settings_dialog.findChildren(QPushButton)
                            if button.text() == "Apply Settings")
        apply_button.click()

        assert not warnings
        assert window.db.path == source
        assert window.db.conn is not old_connection
        assert window.settings_fields["refresh_seconds"].text() == "45"
        with closing(open_database(source, key=key, readonly=True)) as verified:
            assert dict(verified.execute(
                "SELECT key,value FROM settings WHERE key IN ('ib_port','refresh_seconds')"
            )) == {"ib_port": "4001", "refresh_seconds": "45"}
    finally:
        window.close()
        for handler in window.log.handlers[:]:
            window.log.removeHandler(handler)
            handler.close()
    assert app is not None


@pytest.mark.parametrize("copy_fails", (False, True))
def test_gui_move_uses_worker_and_keeps_a_working_portfolio(monkeypatch, copy_fails):
    app = QApplication.instance() or QApplication([])
    with TemporaryDirectory(dir=Path.cwd()) as scratch:
        root = Path(scratch)
        config = root / "config.json"
        monkeypatch.setattr("app.storage.database.local_config_path", lambda: config)
        source = root / "old" / "portfolio.sqlite3"
        seed = Database(source)
        position_id = seed.add_position({"display_ticker": "SYNTHETIC", "ib_symbol": "SYNTHETIC"})
        seed.conn.close()
        destination_root = root / "new"
        destination_root.mkdir()
        window = ui.MainWindow(database_path=source)
        window.open_settings()
        window.advanced_settings.show()
        app.processEvents()
        assert window.database_location.width() < window.settings_dialog.width()
        assert window.database_location.height() >= window.database_location.heightForWidth(
            window.database_location.width())
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(destination_root))
        answers = iter((QMessageBox.Yes, QMessageBox.No))
        monkeypatch.setattr(QMessageBox, "question", lambda *args: next(answers))
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
        if copy_fails:
            def fail_copy(_db, _directory):
                raise OSError("Synthetic copy failure")

            monkeypatch.setattr(Database, "configure_data_directory", fail_copy)

        def run_worker(command, **_kwargs):
            args = command[3:] if command[1:3] == ["-m", "scripts.move_database"] else command[2:]
            return Finished(move_database.main(args))

        monkeypatch.setattr(ui.subprocess, "Popen", run_worker)
        try:
            window.move_database()
            window._poll_database_move()
            if copy_fails:
                assert window.db.path == source
            else:
                assert window.db.path.parent.parent == destination_root
            assert window.db.rows("positions", 1)[0]["id"] == position_id
            assert source.is_file()
            if copy_fails:
                assert not config.exists()
            else:
                assert json.loads(config.read_text())["data_directory"] == str(window.db.path.parent)
            assert window.database_location.text() == str(window.db.path)
        finally:
            window.close()
            for handler in window.log.handlers[:]:
                window.log.removeHandler(handler)
                handler.close()
    assert app is not None
