"""Offline package verification, refused unless this channel's storage is empty."""

from datetime import date, timedelta
import json
from pathlib import Path
import subprocess

from app.storage.database import Database
from app.resources import asset_path
from app.storage.paths import data_root
from app.platform.runtime import app_name, build_info, window_title
from app.storage.secret_store import credential_target
from app.sync.workers import worker_launch


def verify_package(args):
    if len(args) != 1:
        return 2
    output = Path(args[0]).resolve()
    report = dict(build=build_info(), passed=False, checks=[])
    root = data_root()
    # No use of existing personal data, settings, or credentials during QA.
    if root.exists() and any(root.iterdir()):
        raise ValueError("Package verification requires an empty disposable user data root")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from app.ui.window import MainWindow, COLUMNS
        from app.imports.excel_io import export_workbook
        from app.sync.history_status import read_status

        qt = QApplication.instance() or QApplication([])
        qt.setApplicationName(app_name())
        qt.setWindowIcon(QIcon(str(asset_path("doge.ico"))))
        assert not qt.windowIcon().isNull()
        window = MainWindow()
        try:
            assert window.db.rows("positions", 1) == []
            assert window.db.rows("trades", 1) == []
            assert window.db.setting("ib_account_id") == ""
            assert window.db.setting("flex_query_id") == ""
            assert window.windowTitle() == window_title("Main")
            assert window.db.setting("provider") == "Mock"
            report["checks"].append("empty offline GUI, identity and icon")
            window.show()
            qt.processEvents()
            assert window.grab().save(str(output.with_suffix(".png")))
            path = window.db.path
            window.db.set_setting("refresh_seconds", "47")
            backup = window.db.backup()
            assert backup.is_file()
            export_workbook(path.parent / "synthetic-export.xlsx", [], [], [], COLUMNS)
            report["checks"].append("backup and workbook export")
        finally:
            window.close()
            qt.processEvents()
        reopened = Database()
        try:
            assert reopened.path == path
            assert reopened.setting("refresh_seconds") == "47"
            assert reopened.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            reopened.conn.close()
        report["checks"].append("restart preserves database and settings")
        results = {}
        for module in ("scripts.sync_trades", "scripts.sync_analytics", "scripts.sync_cash"):
            command, cwd = worker_launch(module, [path])
            result = subprocess.run(command, cwd=cwd, timeout=45,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert result.returncode == 1, (module, result.returncode)
            results[module] = result.returncode
        status = path.parent / "history-import-status.json"
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        command, cwd = worker_launch("scripts.history_worker", ["--status-file", status,
                                   "--source", "all", "--from-date", yesterday,
                                   "--to-date", yesterday, "--database", path])
        result = subprocess.run(command, cwd=cwd, timeout=45,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 2
        state = read_status(status)
        assert state["state"] == "failed" and state["pid"] is None
        assert "Set the IBKR account" in state["message"]
        results["scripts.history_worker"] = result.returncode
        report.update(database=str(path), credentials_target=credential_target("SYNTHETIC"),
                      workers=results, passed=True)
        report["checks"].append("all workers run and persist expected unconfigured outcomes")
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1
