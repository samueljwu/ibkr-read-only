"""Small, rotating diagnostic logs beside the selected portfolio database."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def diagnostic_log_path(database_path: Path, component: str) -> Path:
    return Path(database_path).parent / "logs" / f"{component}.log"


def configure_diagnostics(database_path: Path, component: str) -> logging.Logger:
    path = diagnostic_log_path(database_path, component)
    logger = logging.getLogger(f"ibkr_dashboard.{component}")
    if getattr(logger, "_diagnostic_path", None) == path:
        return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger._diagnostic_path = path
    return logger
