import argparse
from contextlib import ExitStack
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog

from app.ui.window import MainWindow
from app.resources import asset_path
from app.storage.paths import create_portfolio_directory, validate_database_path
from app.platform.runtime import app_id, app_name, version, channel_mutex
from app.updates.update_handoff import admission, wait_for_postinstall_setup
from app.sync.workers import run_worker

VERSION = version()


def open_initial_window(database_path=None):
    """Explicit migration/recovery remains reachable when the local key is lost."""
    from app.storage.database import LegacyDatabaseError
    from app.storage.paths import default_data_dir, data_root
    from app.storage.migration import migrate_legacy, recover_operation
    from app.ui.recovery_ui import restore_backup

    try:
        return MainWindow(database_path=database_path)
    except LegacyDatabaseError:
        source = database_path or default_data_dir() / "portfolio.sqlite3"
        confirmed = QMessageBox.question(
            None, "Encrypt the existing portfolio",
            "Create a verified encrypted copy of this unencrypted portfolio?\n\n"
            "The original database and old backups will be preserved and remain unencrypted. "
            "Close every other app using the original portfolio first.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirmed != QMessageBox.Yes:
            return None
        try:
            restored = migrate_legacy(source)
            QMessageBox.information(None, "Encrypted copy created",
                                    "The app will open the encrypted copy. The original remains unencrypted:\n\n" + str(source))
            return MainWindow(database_path=restored)
        except Exception as exc:
            QMessageBox.warning(None, "Migration failed", str(exc))
            return None
    except Exception as exc:
        dialog = QMessageBox()
        dialog.setWindowTitle("Could not open portfolio")
        dialog.setText(str(exc))
        dialog.setInformativeText("A missing Windows profile key requires a portable backup and recovery code. "
                                  "An interrupted migration or move can recover its verified operation receipt.")
        portable = dialog.addButton("Restore portable backup", QMessageBox.ActionRole)
        operation = dialog.addButton("Recover completed operation", QMessageBox.ActionRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.exec()
        try:
            if dialog.clickedButton() == portable:
                restored = restore_backup(portable=True)
            elif dialog.clickedButton() == operation:
                receipt, _ = QFileDialog.getOpenFileName(
                    None, "Select verified operation receipt", str(data_root()), "Storage operation (*.json)")
                restored = recover_operation(receipt) if receipt else None
            else:
                return None
            return MainWindow(database_path=restored) if restored is not None else None
        except Exception as failure:
            QMessageBox.warning(None, "Recovery failed", str(failure))
            return None


def main(argv=None):
    # Register AppMutex before releasing admission, including frozen workers.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--postinstall-wait"]:
        if not getattr(sys, "frozen", False) or len(argv) != 1:
            return 2
        wait_for_postinstall_setup()
        argv = []
    with ExitStack() as registered:
        with admission():
            registered.enter_context(channel_mutex())
        return _run(argv)


def _run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker"]:
        return run_worker(argv[1], argv[2:]) if len(argv) >= 2 else 2
    if argv[:1] == ["--verify-package"]:
        from app.platform.package_check import verify_package
        return verify_package(argv[1:])
    parser = argparse.ArgumentParser(description=f"Run {app_name()}.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--database", type=Path,
                           help="Use a SQLite file within this channel's user data directory")
    selection.add_argument("--new-portfolio", action="store_true",
                           help="Create an empty portfolio in this channel's user data directory")
    args = parser.parse_args(argv)
    try:
        database_path = (create_portfolio_directory() / "portfolio.sqlite3" if args.new_portfolio
                         else validate_database_path(args.database) if args.database is not None else None)
    except ValueError as exc:
        parser.error(str(exc))
    if sys.platform == "win32":
        # Taskbar and installer close checks use the fixed channel identity.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id())
    app = QApplication([sys.argv[0]])
    app.setApplicationVersion(VERSION)
    app.setApplicationName(app_name())
    app.setApplicationDisplayName(app_name())
    app.setWindowIcon(QIcon(str(asset_path("doge.ico"))))
    window = open_initial_window(database_path)
    if window is None:
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
