"""Explicit recovery-code custody and authenticated backup UI handlers."""

from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMessageBox

from app.storage import recovery
from app.storage.recovery_code import encode_recovery_key, decode_recovery_key
from app.storage.security_storage import new_key


def export_portable(parent, db):
    destination, _ = QFileDialog.getSaveFileName(
        parent, "Export portable recovery backup", "portfolio.pmbackup", "Encrypted recovery backup (*.pmbackup)")
    if not destination:
        return None
    code = encode_recovery_key(new_key())
    QMessageBox.information(parent, "Save your recovery code",
                            "Save this code separately from the backup. It is required if your Windows profile is lost. "
                            "The app will not save it for you.\n\n" + code)
    confirmation, accepted = QInputDialog.getText(
        parent, "Confirm saved recovery code", "Re-enter the code from your saved copy:", QLineEdit.Password)
    if not accepted:
        return None
    try:
        if decode_recovery_key(confirmation) != decode_recovery_key(code):
            raise ValueError("The saved recovery code does not match; export was cancelled")
        result = recovery.export_portable(db, destination, code)
        QMessageBox.information(parent, "Portable recovery backup verified",
                                "Encrypted backup created and restored successfully in an isolated test portfolio.\n\n"
                                + str(result) + "\n\nKeep the saved recovery code separately.")
        return result
    except Exception as exc:
        QMessageBox.warning(parent, "Portable backup failed", str(exc))
        return None


def restore_backup(parent=None, *, portable=True):
    title = "Restore portable recovery backup" if portable else "Restore encrypted local backup"
    backup, _ = QFileDialog.getOpenFileName(parent, title, "", "Encrypted backups (*.pmbackup *.sqlite3);;All files (*)")
    if not backup:
        return None
    code = None
    if portable:
        code, accepted = QInputDialog.getText(parent, title, "Recovery code:", QLineEdit.Password)
        if not accepted:
            return None
    try:
        account = recovery.inspect_portable(backup, code) if portable else recovery.inspect_local(backup)
        confirmed = QMessageBox.question(
            parent, "Restore into a new portfolio",
            "Account: " + (account or "No brokerage account configured")
            + "\n\nCreate a new encrypted portfolio from this backup? Your current portfolio and the backup will be preserved.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirmed != QMessageBox.Yes:
            return None
        if portable:
            result = recovery.restore_portable(backup, code, expected_account=account)
        else:
            result = recovery.restore_local(backup, expected_account=account)
        QMessageBox.information(parent, "Portfolio restored",
                                "The restored portfolio is selected. Restart the app to open it.\n\n" + str(result))
        return result
    except Exception as exc:
        QMessageBox.warning(parent, "Restore failed", str(exc))
        return None
