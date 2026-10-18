import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit, QMessageBox

from app.ui import MainWindow
from app.storage.encryption import InvalidDatabaseKey, MissingDatabaseKey
from app.version import VERSION


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
        from scripts import (encrypt_database, history_worker, legacy_history_worker,
                             sync_analytics, sync_cash, sync_trades)
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring

        app = QApplication(sys.argv)
        root = Path(__file__).resolve().parent
        images = (root / "app" / "assets" / "app_icon.png",
                  root / "app" / "views" / "help_assets" / "start_connection_light.png")
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
    app.setApplicationName("IBKR Dashboard")
    app.setApplicationDisplayName("IBKR Dashboard")
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "app" / "assets" / "app_icon.png")))
    try:
        window = MainWindow(database_path=database_path)
    except (MissingDatabaseKey, InvalidDatabaseKey):
        while True:
            code, accepted = QInputDialog.getText(
                None, "Unlock portfolio", "Enter the 64-character database recovery code:",
                QLineEdit.Password)
            if not accepted:
                return 1
            try:
                window = MainWindow(database_path=database_path, recovery_key=code.strip())
                break
            except (MissingDatabaseKey, InvalidDatabaseKey, ValueError):
                QMessageBox.warning(None, "Unlock portfolio", "The recovery code could not open this database.")
    window.show()
    recovery_code = getattr(getattr(window, "db", None), "recovery_code", None)
    if recovery_code:
        _show_recovery_code(window, recovery_code)
        window.db.recovery_code = None
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
