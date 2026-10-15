"""Packaging paths must not put a new user's financial data beside code."""

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.platform import paths
from app.storage.database import Database
from app.ui import MainWindow


def test_new_install_uses_per_user_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths, "legacy_source_dir", lambda: tmp_path / "source")
    monkeypatch.setattr("app.storage.database.legacy_source_dir", lambda: tmp_path / "source")
    root = tmp_path / "CustomIBMonitor" / "dev" / "data"
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


def test_existing_source_database_is_copied_to_dev_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    monkeypatch.setattr(paths, "legacy_source_dir", lambda: tmp_path / "source")
    monkeypatch.setattr("app.storage.database.legacy_source_dir", lambda: tmp_path / "source")
    source = tmp_path / "profile" / "CustomIBMonitor" / "portfolio.sqlite3"
    old = Database(source)
    old.add_position({"display_ticker": "ABC", "ib_symbol": "ABC"})
    old.conn.close()
    db = Database()
    try:
        assert db.path.parent.parent == paths.default_data_dir()
        assert db.path != source
        assert db.rows("positions", 1)[0]["ib_symbol"] == "ABC"
        assert source.is_file()
    finally:
        db.conn.close()


def test_encrypted_legacy_database_is_left_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("app.storage.database.legacy_source_dir", lambda: tmp_path / "source")
    source = tmp_path / "CustomIBMonitor" / "portfolio.sqlite3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"encrypted portfolio fixture")
    with pytest.raises(ValueError, match="readable SQLite export"):
        Database()
    assert source.read_bytes() == b"encrypted portfolio fixture"
    assert not paths.local_config_path().exists()


def test_audit_uses_selected_portfolio_database(tmp_path, monkeypatch):
    from scripts import audit_activity_cash

    config = tmp_path / "local_config.json"
    folder = tmp_path / "20261015_123456789abc"
    folder.mkdir()
    database = folder / "portfolio.sqlite3"
    database.touch()
    config.write_text(json.dumps({"data_directory": str(folder)}))
    monkeypatch.setattr(audit_activity_cash, "local_config_path", lambda: config)
    assert audit_activity_cash.selected_database() == database


def test_frozen_install_never_uses_source_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    monkeypatch.setattr(paths, "legacy_source_dir", lambda: tmp_path / "source")
    monkeypatch.setattr("app.storage.database.legacy_source_dir", lambda: tmp_path / "source")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    stable = tmp_path / "profile" / "CustomIBMonitor" / "stable" / "data"
    source = stable / "20261010_123456789abc" / "portfolio.sqlite3"
    old = Database(source)
    old.add_position({"display_ticker": "XYZ", "ib_symbol": "XYZ"})
    old.conn.close()
    (stable / "local_config.json").write_text(json.dumps({"data_directory": str(source.parent)}))
    root = tmp_path / "profile" / "CustomIBMonitor" / "prod" / "data"
    assert paths.default_data_dir() == root
    assert paths.local_config_path() == root / "local_config.json"
    db = Database()
    try:
        assert db.path.parent.parent == root
        assert db.rows("positions", 1)[0]["ib_symbol"] == "XYZ"
        assert source.is_file()
    finally:
        db.conn.close()


def test_encrypted_stable_profile_does_not_select_empty_prod(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    stable = tmp_path / "CustomIBMonitor" / "stable" / "data"
    old_folder = stable / "20261010_123456789abc"
    old_folder.mkdir(parents=True)
    source = old_folder / "portfolio.sqlite3"
    source.write_bytes(b"encrypted portfolio fixture")
    (stable / "local_config.json").write_text(json.dumps({"data_directory": str(old_folder)}))
    with pytest.raises(ValueError, match="readable SQLite export"):
        Database()
    assert source.read_bytes() == b"encrypted portfolio fixture"
    assert not paths.local_config_path().exists()


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
