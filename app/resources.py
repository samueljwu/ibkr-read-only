"""Locations anchored to the app package in source and frozen builds."""

from pathlib import Path


def app_directory() -> Path:
    return Path(__file__).resolve().parent


def project_directory() -> Path:
    return app_directory().parent


def asset_path(name: str) -> Path:
    return app_directory() / "assets" / name
