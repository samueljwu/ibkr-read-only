"""Persistent per-channel storage, independent of source and install directories."""

from __future__ import annotations

import os
import json
import secrets
import sys
from datetime import date
from uuid import uuid4
from pathlib import Path
from app.platform.runtime import channel
from app.resources import project_directory


APP_DATA_NAME = "CustomIBMonitor"


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_DATA_NAME / channel()
        return Path.home() / "AppData" / "Local" / APP_DATA_NAME / channel()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DATA_NAME / channel()
    return Path.home() / ".local" / "share" / APP_DATA_NAME / channel()


def legacy_source_dir() -> Path:
    return project_directory()


def default_data_dir() -> Path:
    """Return the selected portfolio folder, or reserve a new portfolio ID."""
    selected = selected_data_directory()
    if selected is not None:
        return selected
    if (data_root() / "portfolio.sqlite3").exists():
        raise ValueError("Move data/portfolio.sqlite3 into a portfolio subfolder and select it before launching")
    return create_portfolio_directory()


def selected_data_directory() -> Path | None:
    config = local_config_path()
    if config.exists():
        configured = json.loads(config.read_text(encoding="utf-8")).get("data_directory", "")
        if configured:
            return validate_data_directory(configured)
    return None


def local_config_path() -> Path:
    # Never silently adopt another channel's or the old checkout's selection.
    return data_root() / "local_config.json"


def data_root() -> Path:
    return (user_data_dir() / "data").resolve()


def validate_data_directory(directory) -> Path:
    """Reject the data root itself and paths that escape it, including symlinks."""
    directory = Path(directory).expanduser().resolve()
    root = data_root()
    try:
        relative = directory.relative_to(root)
    except ValueError:
        relative = None
    if relative is None or not relative.parts:
        raise ValueError(f"SQLite databases must live in a subfolder within {root}")
    return directory


def validate_database_path(path) -> Path:
    path = Path(path).expanduser().resolve()
    validate_data_directory(path.parent)
    return path


def create_portfolio_directory() -> Path:
    """Reserve a unique folder once; keep this ID when the portfolio is renamed."""
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    created = date.today().strftime("%Y%m%d")
    while True:
        directory = validate_data_directory(root / f"{created}_{secrets.token_hex(6)}")
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory


def select_data_directory(directory):
    directory = validate_data_directory(directory)
    config = local_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.with_name(f".{config.name}-{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump({"data_directory": str(directory)}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, config)
    finally:
        temporary.unlink(missing_ok=True)
