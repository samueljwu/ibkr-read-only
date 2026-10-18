"""Locations for user data, independent of the application installation."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from secrets import token_hex


APP_DATA_NAME = "IBKRDashboard"


def user_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DATA_NAME
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DATA_NAME
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / APP_DATA_NAME
    return Path.home() / ".local" / "share" / APP_DATA_NAME


def default_data_dir() -> Path:
    profile = "prod" if getattr(sys, "frozen", False) else "dev"
    return user_data_dir() / profile / "data"


def new_portfolio_dir(root: Path) -> Path:
    """Reserve a directory with an opaque, date-prefixed portfolio identifier."""
    root.mkdir(parents=True, exist_ok=True)
    while True:
        folder = root / f"{date.today():%Y%m%d}_{token_hex(6)}"
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return folder


def local_config_path() -> Path:
    return default_data_dir() / "local_config.json"
