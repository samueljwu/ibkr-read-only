import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

from app.ui import MainWindow
from app.platform.paths import default_data_dir, new_portfolio_dir
from app.storage.database import Database, MissingSelectedDatabase
from app.storage.encryption import InvalidDatabaseKey, MissingDatabaseKey
from app.views.database_selection import DatabaseSelectionWindow
from app.views.typography import install_app_font
from app.version import VERSION
from app.platform.app_update import launch_update


def _show_recovery_code(parent, code):
    message = QMessageBox(parent)
    message.setWindowTitle("Save database recovery code")
    message.setIcon(QMessageBox.Warning)
    message.setText("Save this recovery code outside the database and backup folders. "
                    "You need it to open an encrypted portfolio if its stored key is unavailable, "
                    "including on another device or user account.\n\n" + code)
    message.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
    message.exec()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--bundle-check"]:
        from zoneinfo import ZoneInfo

        from sqlcipher3 import dbapi2 as sqlite3
        from PySide6.QtGui import QPixmap
        from app.market.market_data import EClient
        from scripts import (encrypt_database, move_database, history_worker, legacy_history_worker,
                             sync_analytics, sync_cash, sync_trades)
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring

        app = QApplication(sys.argv)
        install_app_font(app)
        root = Path(__file__).resolve().parent
        images = (root / "app" / "assets" / "app_icon.png",
                  root / "app" / "views" / "help_assets" / "start_flex_light@2x.png")
        licenses = (root / "licenses" / "inventory.json",
                    root / "licenses" / "Python-LICENSE.txt",
                    root / "licenses" / "Apache-2.0.txt")
        with sqlite3.connect(":memory:") as connection:
            cipher_available = bool(connection.execute("PRAGMA cipher_version").fetchone())
        return 0 if (cipher_available and EClient is not None
                     and ZoneInfo("America/New_York")
                     and all(not QPixmap(str(path)).isNull() for path in images)
                     and (not getattr(sys, "frozen", False)
                          or all(path.is_file() for path in licenses))) else 1
    if args and args[0] == "--history-worker":
        from scripts.history_worker import main as history_worker
        return history_worker(args[1:])
    if args and args[0] == "--legacy-history-worker":
        from scripts.legacy_history_worker import main as legacy_history_worker
        return legacy_history_worker(args[1:])
    if args and args[0] == "--encrypt-database":
        from scripts.encrypt_database import main as encrypt_database
        return encrypt_database(args[1:])
    if args and args[0] == "--move-database":
        from scripts.move_database import main as move_database
        return move_database(args[1:])
    if args and args[0] in ("--sync-trades", "--sync-analytics", "--sync-cash") and len(args) <= 2:
        from scripts.sync_trades import main as sync_trades
        from scripts.sync_analytics import main as sync_analytics
        from scripts.sync_cash import main as sync_cash
        return {
            "--sync-trades": sync_trades,
            "--sync-analytics": sync_analytics,
            "--sync-cash": sync_cash,
        }[args[0]](args[1] if len(args) == 2 else None)
    database_path = None
    if "--database" in args:
        index = args.index("--database")
        if index + 1 < len(args):
            database_path = Path(args[index + 1])
    app = QApplication(sys.argv)
    install_app_font(app)
    app.setApplicationName("IBKR Dashboard")
    app.setApplicationDisplayName("IBKR Dashboard")
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "app" / "assets" / "app_icon.png")))
    windows = {}
    chooser = None
    switch_from = None

    def show_chooser(current_window=None):
        nonlocal switch_from
        switch_from = current_window
        current = current_window.db.path if current_window is not None else None
        if current_window is not None:
            chooser.set_appearance(current_window.theme, current_window.zoom_percent,
                                   current_window.font())
        chooser.refresh(Database.saved_databases(), current=current)
        chooser.show()
        chooser.raise_()
        chooser.activateWindow()

    def open_window(path, *, parent=None):
        try:
            return MainWindow(database_path=path, on_manage_portfolios=show_chooser)
        except (MissingDatabaseKey, InvalidDatabaseKey):
            while True:
                code, accepted = QInputDialog.getText(
                    parent, "Unlock portfolio", "Enter the 64-character database recovery code:",
                    QLineEdit.Password)
                if not accepted:
                    return None
                try:
                    return MainWindow(database_path=path, recovery_key=code.strip(),
                                      on_manage_portfolios=show_chooser)
                except (MissingDatabaseKey, InvalidDatabaseKey, ValueError):
                    QMessageBox.warning(parent, "Unlock portfolio",
                                        "The recovery code could not open this database.")

    def register(window):
        marker = id(window)
        window.setAttribute(Qt.WA_DeleteOnClose)
        windows[marker] = window
        window.destroyed.connect(lambda _object=None, key=marker: windows.pop(key, None))
        window.show()
        return window

    def activate(path, recovery_code=None):
        nonlocal switch_from
        key = Path(path).resolve()
        existing = next((window for window in windows.values()
                         if window.db.path.resolve() == key), None)
        if existing is not None:
            try:
                Database.select_database(key)
            except Exception as exc:
                QMessageBox.warning(chooser, "Open portfolio", str(exc))
                return
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            chooser.close()
            return
        if not key.is_file():
            QMessageBox.warning(chooser, "Open portfolio", "The selected database is unavailable.")
            return
        if switch_from is not None:
            previous = switch_from
            if not previous.close():
                return
            windows.pop(id(previous), None)
            switch_from = None
        selected = None
        try:
            selected = open_window(key, parent=chooser)
            if selected is None:
                return
            Database.select_database(selected.db.path)
        except Exception as exc:
            if selected is not None:
                selected.close()
            QMessageBox.warning(chooser, "Open portfolio", str(exc))
            return
        register(selected)
        chooser.close()
        code = recovery_code or selected.db.recovery_code
        if code:
            _show_recovery_code(selected, code)
            selected.db.recovery_code = None

    def create_new():
        try:
            folder = new_portfolio_dir(default_data_dir())
            created = Database(folder / "portfolio.sqlite3")
            code = created.recovery_code
            created.conn.close()
        except Exception as exc:
            QMessageBox.warning(chooser, "Create portfolio", str(exc))
            return
        activate(created.path, code)

    def remove_saved(path):
        try:
            Database.remove_saved_database(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(chooser, "Remove saved portfolio", str(exc))
            return
        chooser.refresh(Database.saved_databases(),
                        current=switch_from.db.path if switch_from is not None else None)

    chooser = DatabaseSelectionWindow(activate, create_new, remove_saved)
    try:
        window = open_window(database_path)
    except MissingSelectedDatabase:
        show_chooser()
    else:
        if window is None:
            return 1
        if database_path is not None:
            Database.select_database(window.db.path)
        register(window)
        if window.db.recovery_code:
            _show_recovery_code(window, window.db.recovery_code)
            window.db.recovery_code = None
    result = app.exec()
    pending_update = getattr(app, "pending_update", None)
    if pending_update is not None:
        try:
            launch_update(*pending_update)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(None, "Update could not start",
                                f"The installer was downloaded to:\n{pending_update[0]}\n\n{exc}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
