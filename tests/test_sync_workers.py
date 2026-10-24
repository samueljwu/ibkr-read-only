"""Sync failures remain visible in local logs even without a console."""

import pytest

from app.flex.flex_import import FlexEnvironmentError, FlexServiceError, FlexValidationError
from app.storage.database import Database
from scripts import sync_analytics, sync_cash, sync_trades


@pytest.mark.parametrize("module,runner,source,log_name", [
    (sync_trades, "run_flex_sync", "flex", "sync-trades"),
    (sync_analytics, "run_analytics_flex_sync", "analytics", "sync-analytics"),
    (sync_cash, "run_cash_flex_sync", "cash", "sync-cash"),
])
def test_tls_failure_message_matches_across_sync_workers(
        tmp_path, monkeypatch, capsys, module, runner, source, log_name):
    message = "IBKR Flex TLS certificate verification failed. Check this Python installation's certificates."

    def fail(*_args, **_kwargs):
        raise FlexEnvironmentError(message)

    monkeypatch.setattr(module, runner, fail)
    path = tmp_path / "portfolio.sqlite3"
    assert module.main(path) == 1
    db = Database(path)
    try:
        assert db.metadata(f"ibkr_{source}_sync_status") == "failed"
        assert db.metadata(f"ibkr_{source}_sync_error") == message
        assert message in capsys.readouterr().err
        assert message in (tmp_path / "logs" / f"{log_name}.log").read_text()
    finally:
        db.conn.close()




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
