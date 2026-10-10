"""One worker protocol for source and frozen app launches."""

from pathlib import Path
import sys
from app.resources import project_directory

WORKERS = {"scripts.sync_trades": "trades", "scripts.sync_analytics": "analytics",
           "scripts.sync_cash": "cash", "scripts.history_worker": "history"}


def worker_launch(module, args):
    if module not in WORKERS:
        raise ValueError("Unknown worker")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", WORKERS[module], *map(str, args)], Path(sys.executable).parent
    return [sys.executable, "-m", module, *map(str, args)], project_directory()


def run_worker(name, args):
    # Static imports let the freezer discover every worker dependency.
    from scripts import sync_trades, sync_analytics, sync_cash, history_worker
    if name == "history":
        return history_worker.main(args)
    runners = {"trades": sync_trades.main, "analytics": sync_analytics.main, "cash": sync_cash.main}
    if name not in runners or len(args) != 1:
        return 2
    return runners[name](args[0])
