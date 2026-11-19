"""Packaging paths must not put a new user's financial data beside code."""

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.platform import paths
from app.storage.database import Database, MissingSelectedDatabase
from app.ui import MainWindow


def _isolate_app_data(monkeypatch, base):
    location = base / "IBKRDashboard"
    monkeypatch.setattr(paths, "user_data_dir", lambda: location)










def test_missing_active_selection_still_fails_without_creating_database(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    config = paths.local_config_path()
    config.parent.mkdir(parents=True)
    missing = config.parent / "missing" / "portfolio.sqlite3"
    config.write_text(json.dumps({"data_directory": str(missing.parent)}))

    with pytest.raises(MissingSelectedDatabase, match="Configured portfolio database is missing"):
        Database()
    assert not missing.exists()

    replacement = config.parent / "replacement" / "portfolio.sqlite3"
    created = Database(replacement)
    created.conn.close()
    Database.select_database(replacement)
    reopened = Database()
    try:
        assert reopened.path == replacement
        assert not missing.exists()
    finally:
        reopened.conn.close()


def test_app_opens_database_selection_when_saved_database_is_missing(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main as entry
    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    config = paths.local_config_path()
    config.parent.mkdir(parents=True)
    missing = config.parent / "missing" / "portfolio.sqlite3"
    config.write_text(json.dumps({"data_directory": str(missing.parent)}))
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    app = QApplication.instance() or QApplication([])
    seen = []

    def inspect():
        windows = [widget for widget in QApplication.topLevelWidgets()
                   if isinstance(widget, DatabaseSelectionWindow) and widget.isVisible()]
        seen.extend(windows)
        for widget in windows:
            widget.close()
        app.quit()

    QTimer.singleShot(0, inspect)
    assert entry.main([]) == 0
    assert len(seen) == 1
    assert not missing.exists()
    assert json.loads(config.read_text())["data_directory"] == str(missing.parent)


def test_cancelling_recovery_prompt_can_create_new_portfolio(tmp_path, monkeypatch,
                                                            synthetic_database_keys):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QInputDialog, QPushButton
    import main as entry

    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    locked = tmp_path / "locked" / "portfolio.sqlite3"
    db = Database(locked)
    db.conn.close()
    Database.select_database(locked)
    original = locked.read_bytes()
    synthetic_database_keys.clear()
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    monkeypatch.setattr(entry, "_show_recovery_code", lambda *args: None)
    app = QApplication.instance() or QApplication([])
    observed = []
    prompt_parents = []

    original_get_text = QInputDialog.getText

    def cancel_prompt(parent, *args):
        prompt_parents.append(parent)
        assert isinstance(parent, DatabaseSelectionWindow) and parent.isVisible()
        QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
        result = original_get_text(parent, *args)
        QTimer.singleShot(0, inspect)
        return result

    monkeypatch.setattr(entry.QInputDialog, "getText", cancel_prompt)

    def inspect():
        try:
            chooser = next(widget for widget in app.topLevelWidgets()
                           if isinstance(widget, DatabaseSelectionWindow) and widget.isVisible())
            assert "create a new portfolio" in chooser.explanation.text()
            button = next(button for button in chooser.findChildren(QPushButton)
                          if button.text() == "Create new portfolio")
            button.click()
            observed.extend(widget.db.path for widget in app.topLevelWidgets()
                            if isinstance(widget, MainWindow) and widget.isVisible())
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow):
                    widget.close()
        finally:
            app.quit()

    assert entry.main([]) == 0
    assert len(prompt_parents) == 1
    assert len(observed) == 1
    assert observed[0] != locked
    assert observed[0].is_file()
    assert locked.read_bytes() == original
    assert Database.saved_databases() == [observed[0], locked]


def test_recovery_prompt_opens_database_and_closes_chooser(tmp_path, monkeypatch,
                                                          synthetic_database_keys):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main as entry

    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    locked = tmp_path / "locked" / "portfolio.sqlite3"
    db = Database(locked)
    code = db.recovery_code
    db.conn.close()
    Database.select_database(locked)
    synthetic_database_keys.clear()
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    app = QApplication.instance() or QApplication([])
    observed = []

    def provide_code(parent, *args):
        assert isinstance(parent, DatabaseSelectionWindow) and parent.isVisible()
        return code, True

    monkeypatch.setattr(entry.QInputDialog, "getText", provide_code)

    def inspect():
        try:
            observed.extend(widget.db.path for widget in app.topLevelWidgets()
                            if isinstance(widget, MainWindow) and widget.isVisible())
            assert not any(isinstance(widget, DatabaseSelectionWindow) and widget.isVisible()
                           for widget in app.topLevelWidgets())
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow):
                    widget.close()
        finally:
            app.quit()

    QTimer.singleShot(0, inspect)
    assert entry.main([]) == 0
    assert observed == [locked]


def test_keychain_denial_still_opens_portfolio_chooser(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton
    import main as entry

    from app.platform import secret_store
    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    locked = tmp_path / "locked" / "portfolio.sqlite3"
    db = Database(locked)
    db.conn.close()
    Database.select_database(locked)
    original = locked.read_bytes()
    load_key = secret_store.load_database_key

    def denied(path):
        if Path(path) == locked:
            raise RuntimeError("synthetic Keychain access denied")
        return load_key(path)

    monkeypatch.setattr(secret_store, "load_database_key", denied)
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    monkeypatch.setattr(entry, "_show_recovery_code", lambda *args: None)
    app = QApplication.instance() or QApplication([])
    warnings = []
    monkeypatch.setattr(entry.QMessageBox, "warning",
                        lambda _parent, title, message: warnings.append((title, message)))
    observed = []

    def inspect():
        try:
            chooser = next(widget for widget in app.topLevelWidgets()
                           if isinstance(widget, DatabaseSelectionWindow) and widget.isVisible())
            button = next(button for button in chooser.findChildren(QPushButton)
                          if button.text() == "Create new portfolio")
            button.click()
            observed.extend(widget.db.path for widget in app.topLevelWidgets()
                            if isinstance(widget, MainWindow) and widget.isVisible())
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow):
                    widget.close()
        finally:
            app.quit()

    QTimer.singleShot(0, inspect)
    assert entry.main([]) == 0
    assert len(observed) == 1 and observed[0] != locked
    assert warnings and warnings[0][0] == "Open portfolio"
    assert locked.read_bytes() == original


def test_saved_portfolio_files_remain_selectable(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    first = tmp_path / "first" / "portfolio.sqlite3"
    second = tmp_path / "second" / "portfolio.sqlite3"
    for path in (first, second):
        db = Database(path)
        db.conn.close()
    Database.select_database(first)
    Database.select_database(second)
    assert Database.saved_databases() == [second, first]
    reopened = Database()
    try:
        assert reopened.path == second
    finally:
        reopened.conn.close()
    Database.select_database(first)
    assert Database.saved_databases() == [first, second]


def test_remove_saved_portfolio_forgets_entry_but_keeps_database(tmp_path, monkeypatch):
    _isolate_app_data(monkeypatch, tmp_path)
    first = tmp_path / "first" / "portfolio.sqlite3"
    second = tmp_path / "second" / "portfolio.sqlite3"
    for path in (first, second):
        db = Database(path)
        db.conn.close()
    Database.select_database(first)
    Database.select_database(second)

    with pytest.raises(ValueError, match="active"):
        Database.remove_saved_database(second)
    Database.remove_saved_database(first)

    assert Database.saved_databases() == [second]
    assert first.is_file()
    reopened = Database()
    try:
        assert reopened.path == second
    finally:
        reopened.conn.close()
    Database.select_database(first)
    assert Database.saved_databases() == [first, second]


def test_portfolio_chooser_removes_selected_entry_with_confirmation(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    active = tmp_path / "active" / "portfolio.sqlite3"
    missing = tmp_path / "missing" / "portfolio.sqlite3"
    db = Database(active)
    db.conn.close()
    Database.select_database(active)
    config = paths.local_config_path()
    saved = json.loads(config.read_text(encoding="utf-8"))
    saved["portfolio_directories"].append(str(missing.parent))
    config.write_text(json.dumps(saved), encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    chooser = DatabaseSelectionWindow(lambda _path: None, lambda: None,
                                      Database.remove_saved_database)
    chooser.refresh(Database.saved_databases(), current=active)
    try:
        assert not chooser.remove_selected_button.isEnabled()
        chooser.saved_list.setCurrentRow(1)
        assert chooser.remove_selected_button.isEnabled()
        assert not chooser.open_selected_button.isEnabled()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
        chooser.remove_selected_button.click()
        assert missing in Database.saved_databases()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
        chooser.remove_selected_button.click()
        assert Database.saved_databases() == [active]
        assert active.is_file()
    finally:
        chooser.close()


def test_missing_active_database_can_switch_to_saved_portfolio(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main as entry
    from app.views.database_selection import DatabaseSelectionWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    available = tmp_path / "available" / "portfolio.sqlite3"
    db = Database(available)
    db.conn.close()
    missing = tmp_path / "missing" / "portfolio.sqlite3"
    config = paths.local_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"data_directory": str(missing.parent),
                                  "portfolio_directories": [str(missing.parent),
                                                            str(available.parent)]}))
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    app = QApplication.instance() or QApplication([])
    observed = []

    def inspect():
        try:
            chooser = next(widget for widget in app.topLevelWidgets()
                           if isinstance(widget, DatabaseSelectionWindow) and widget.isVisible())
            chooser._open_database(str(available))
            observed.extend(widget.db.path for widget in app.topLevelWidgets()
                            if isinstance(widget, MainWindow) and widget.isVisible())
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow):
                    widget.close()
        finally:
            app.quit()

    QTimer.singleShot(0, inspect)
    assert entry.main([]) == 0
    assert observed == [available]
    assert Database.saved_databases() == [available, missing]


def test_portfolios_menu_switches_database_window(tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import main as entry
    from app.views.database_selection import DatabaseSelectionWindow
    from app.views.ui_theme import stylesheet

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    _isolate_app_data(monkeypatch, tmp_path)
    first = tmp_path / "first" / "portfolio.sqlite3"
    second = tmp_path / "second" / "portfolio.sqlite3"
    for path in (first, second):
        db = Database(path)
        db.conn.close()
    Database.select_database(first)
    monkeypatch.setattr(entry, "QApplication", lambda argv: QApplication.instance() or QApplication(argv))
    app = QApplication.instance() or QApplication([])
    observed = []
    visible_after = []
    errors = []
    monkeypatch.setattr(entry.QMessageBox, "warning",
                        lambda _parent, title, message: errors.append((title, message)))

    def inspect():
        try:
            first_window = next(widget for widget in app.topLevelWidgets()
                                if isinstance(widget, MainWindow) and widget.isVisible())
            observed.append(first_window.db.path)
            first_window._apply_theme("dark")
            next(action for action in first_window.file_menu.actions()
                 if action.text() == "Portfolios…").trigger()
            chooser = next(widget for widget in app.topLevelWidgets()
                           if isinstance(widget, DatabaseSelectionWindow) and widget.isVisible())
            assert chooser.styleSheet() == stylesheet("dark", first_window.zoom_percent)
            chooser._open_database(str(second))
            windows = [widget for widget in app.topLevelWidgets()
                       if isinstance(widget, MainWindow) and widget.isVisible()]
            observed.extend(window.db.path for window in windows)
            visible_after.append(len(windows))
            for window in windows:
                window.close()
        finally:
            app.quit()

    QTimer.singleShot(0, inspect)
    assert entry.main([]) == 0
    assert errors == []
    assert observed == [first, second]
    assert visible_after == [1]
    assert Database.saved_databases() == [second, first]




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
