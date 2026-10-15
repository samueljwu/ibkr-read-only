import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.ui import MainWindow


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--history-worker":
        from scripts.history_worker import main as history_worker
        return history_worker(args[1:])
    if args and args[0] == "--legacy-history-worker":
        from scripts.legacy_history_worker import main as legacy_history_worker
        return legacy_history_worker(args[1:])
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
    app.setApplicationName("Portfolio Monitor")
    app.setApplicationDisplayName("Portfolio Monitor")
    window = MainWindow(database_path=database_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
