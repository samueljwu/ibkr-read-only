from pathlib import Path

import pytest

import main
from app.storage.database import Database
from scripts.copy_database import copy_database


def test_main_passes_explicit_database_to_window(monkeypatch, tmp_path):
    calls = []

    class App:
        def __init__(self, args):
            pass

        def setApplicationVersion(self, value):
            pass

        def setApplicationName(self, value):
            pass

        def setApplicationDisplayName(self, value):
            pass

        def exec(self):
            return 0

    class Window:
        def __init__(self, database_path):
            calls.append(database_path)

        def show(self):
            pass

    monkeypatch.setattr(main, "QApplication", App)
    monkeypatch.setattr(main, "MainWindow", Window)
    chosen = tmp_path / "fresh run" / "portfolio.sqlite3"
    assert main.main(["--database", str(chosen)]) == 0
    assert calls == [chosen]


def test_debug_copy_preserves_data_but_isolates_directories(tmp_path):
    db = Database(tmp_path / "original" / "portfolio.sqlite3")
    try:
        db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        db.set_setting("data_directory", str(db.path.parent))
        db.set_setting("backup_directory", str(tmp_path / "original-backups"))
        destination = tmp_path / "debug copy" / "portfolio.sqlite3"
        copy_database(db.path, destination)
        with pytest.raises(FileExistsError):
            copy_database(db.path, destination)
        copied = Database(destination)
        try:
            assert copied.rows("positions", 1)[0]["ib_symbol"] == "ABC"
            assert copied.setting("data_directory") == ""
            assert copied.setting("backup_directory") == ""
            assert copied.backup().parent == destination.parent / "backups"
            copied.add_position({"display_ticker": "NEW", "ib_symbol": "NEW"})
            assert len(db.rows("positions", 1)) == 1
            assert db.setting("backup_directory") == str(tmp_path / "original-backups")
        finally:
            copied.conn.close()
    finally:
        db.conn.close()
