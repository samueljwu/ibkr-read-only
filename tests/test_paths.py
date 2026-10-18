"""Packaging paths must not put a new user's financial data beside code."""

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.platform import paths
from app.storage.database import Database
from app.ui import MainWindow


def _isolate_app_data(monkeypatch, base):
    location = base / "IBKRDashboard"
    monkeypatch.setattr(paths, "user_data_dir", lambda: location)


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert paths.user_data_dir() == Path.home() / "Library" / "Application Support" / "IBKRDashboard"


def test_windows_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.user_data_dir() == tmp_path / "IBKRDashboard"


def test_new_install_uses_per_user_directory(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    root = tmp_path / "IBKRDashboard" / "dev" / "data"
    assert paths.default_data_dir() == root
    assert paths.local_config_path() == root / "local_config.json"
    db = Database()
    try:
        assert db.path.parent.parent == root
        assert re.fullmatch(r"\d{8}_[0-9a-f]{12}", db.path.parent.name)
        assert json.loads(paths.local_config_path().read_text())["data_directory"] == str(db.path.parent)
    finally:
        db.conn.close()
    reopened = Database()
    try:
        assert reopened.path == db.path
    finally:
        reopened.conn.close()


def test_previous_app_folder_is_not_selected(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    old = tmp_path / "CustomIBMonitor" / "prod" / "data" / "portfolio.sqlite3"
    old_db = Database(old)
    old_db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    old_db.conn.close()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)

    db = Database()
    try:
        assert db.path.parent.parent == paths.default_data_dir()
        assert db.rows("positions", 1) == []
        assert old.is_file()
    finally:
        db.conn.close()


def test_missing_active_selection_still_fails_without_creating_database(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    config = paths.local_config_path()
    config.parent.mkdir(parents=True)
    missing = config.parent / "missing" / "portfolio.sqlite3"
    config.write_text(json.dumps({"data_directory": str(missing.parent)}))

    with pytest.raises(FileNotFoundError, match="Configured portfolio database is missing"):
        Database()
    assert not missing.exists()


def test_frozen_sync_uses_executable_and_user_data(tmp_path, monkeypatch):
    class Window:
        pass

    window = Window()
    window.db = type("Store", (), {"path": tmp_path / "portfolio.sqlite3"})()
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    command, workdir = MainWindow._sync_launch(window, "scripts.sync_trades", "--sync-trades")
    assert command == [paths.sys.executable, "--sync-trades", str(window.db.path)]
    assert workdir == tmp_path


def test_verified_database_move_preserves_data_and_selects_copy(tmp_path, monkeypatch):
    # Windows' test sandbox permits file creation in tmp_path but blocks renames there.
    with TemporaryDirectory(dir=Path.cwd()) as scratch:
        root = Path(scratch)
        config = root / "config.json"
        monkeypatch.setattr("app.storage.database.local_config_path", lambda: config)
        db = Database(root / "old" / "portfolio.sqlite3")
        position_id = db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        destination = root / "new"
        db.configure_data_directory(destination)
        db.conn.close()
        reopened = Database()
        try:
            assert reopened.path.parent.parent == destination
            assert re.fullmatch(r"\d{8}_[0-9a-f]{12}", reopened.path.parent.name)
            assert reopened.rows("positions", 1)[0]["id"] == position_id
        finally:
            reopened.conn.close()


def test_populated_database_rejects_account_change(tmp_path):
    db = Database(tmp_path / "account.sqlite3")
    try:
        db.set_setting("ib_account_id", "U1")
        db.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
        import pytest
        with pytest.raises(ValueError, match="separate database"):
            db.validate_account_change("U2")
        db.validate_account_change("U1")
    finally:
        db.conn.close()


def test_account_ownership_rejects_conflicting_broker_provenance(tmp_path):
    db = Database(tmp_path / "provenance.sqlite3")
    try:
        with db.conn:
            db.conn.execute("""INSERT INTO broker_sync_windows
                (account_id,source,from_day,to_day,status) VALUES ('U1','flex','2026-01-01','2026-01-02','complete')""")
        import pytest
        with pytest.raises(ValueError, match="another IBKR account"):
            db.validate_account_ownership("U2")
    finally:
        db.conn.close()
