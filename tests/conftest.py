"""Keep synthetic database keys out of the user's credential store."""

from pathlib import Path

import pytest

from app.platform import secret_store


@pytest.fixture(autouse=True)
def synthetic_database_keys(monkeypatch):
    keys = {}

    def identity(path):
        return str(Path(path).resolve()).casefold()

    monkeypatch.setattr(secret_store, "save_database_key",
                        lambda path, key: keys.__setitem__(identity(path), key))
    monkeypatch.setattr(secret_store, "load_database_key",
                        lambda path: keys.get(identity(path)))
    return keys
