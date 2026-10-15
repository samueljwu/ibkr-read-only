"""Sync failures remain visible in local logs even without a console."""

import pytest

from app.flex.flex_import import FlexServiceError, FlexValidationError
from app.storage.database import Database
from scripts import sync_analytics, sync_cash, sync_trades


@pytest.mark.parametrize("module,runner", [
    (sync_trades, "run_flex_sync"),
    (sync_analytics, "run_analytics_flex_sync"),
    (sync_cash, "run_cash_flex_sync"),
])
def test_worker_failure_without_console(tmp_path, monkeypatch, module, runner):
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic offline failure")

    monkeypatch.setattr(module, runner, fail)
    monkeypatch.setattr(module.sys, "stderr", None)
    assert module.main(tmp_path / "portfolio.sqlite3") == 1


@pytest.mark.parametrize("module,runner,source", [
    (sync_analytics, "run_analytics_flex_sync", "analytics"),
    (sync_cash, "run_cash_flex_sync", "cash"),
])
@pytest.mark.parametrize("kind", ["service", "unexpected", "validation"])
def test_worker_persists_only_safe_error_context(tmp_path, monkeypatch, capsys, module, runner, source, kind):
    token = "synthetic-credential"
    untrusted = f"https://example.invalid/?t={token}&private=other"
    error = (FlexServiceError(1025, untrusted, "GetStatement") if kind == "service" else
             RuntimeError(untrusted) if kind == "unexpected" else
             FlexValidationError("Cash Flex query needs a USD Cash Report row"))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(module, runner, fail)
    path = tmp_path / "portfolio.sqlite3"
    assert module.main(path) == 1
    db = Database(path)
    try:
        message = db.metadata(f"ibkr_{source}_sync_error")
        assert db.metadata(f"ibkr_{source}_sync_status") == "failed"
        output = capsys.readouterr().err + (tmp_path / "logs" / f"sync-{source}.log").read_text()
        assert token not in message + output and "https://" not in message + output
        assert "private=other" not in message + output
        if kind == "service":
            assert "1025" in message and "GetStatement" in message
        elif kind == "validation":
            assert "USD Cash Report row" in message
        else:
            assert message.startswith("Flex import failed;")
    finally:
        db.conn.close()
