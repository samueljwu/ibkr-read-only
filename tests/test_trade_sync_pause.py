"""Broker restrictions survive automatic launches but allow an explicit retry."""

from datetime import date

import pytest
from PySide6.QtWidgets import QApplication

from app.storage.database import Database
from app.flex.flex_import import FlexServiceError
from app.sync.sync_service import run_flex_sync, trade_sync_paused
from app import ui


def test_restriction_persists_and_successful_retry_resumes_sync(tmp_path):
    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "TEST")

    def restricted(*_args, **_kwargs):
        raise FlexServiceError(1025, "Too many failed attempts.", "SendRequest")

    with pytest.raises(FlexServiceError):
        run_flex_sync(db, "TEST", "query", token="dummy", fetcher=restricted,
                      windows_override=[("2026-10-07", "2026-10-07")], pacing_seconds=0)
    assert db.metadata("ibkr_flex_sync_error_code") == "1025"
    db.conn.close()
    db = Database(path)
    try:
        assert trade_sync_paused(db)
        xml = (b'<FlexQueryResponse><FlexStatements><FlexStatement accountId="TEST" '
               b'baseCurrency="USD" fromDate="20261007" toDate="20261007"><Trades/>'
               b'</FlexStatement></FlexStatements></FlexQueryResponse>')
        run_flex_sync(db, "TEST", "query", token="dummy", fetcher=lambda *_a, **_kw: xml,
                      windows_override=[("2026-10-07", "2026-10-07")], pacing_seconds=0)
        assert not trade_sync_paused(db)
        assert db.metadata("ibkr_flex_sync_error_code") == ""
    finally:
        db.conn.close()


def test_existing_1025_pauses_startup_settings_and_periodic_but_manual_sync_retries(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    path = tmp_path / "portfolio.sqlite3"
    db = Database(path)
    db.set_setting("ib_account_id", "TEST")
    for key in ("flex_query_id", "analytics_flex_query_id", "cash_flex_query_id"):
        db.set_setting(key, "query")
    # Existing databases have the message without the new error-code metadata.
    db.set_metadata("ibkr_flex_sync_status", "failed")
    db.set_metadata("ibkr_flex_sync_error", "Flex error 1025: Too many failed attempts.")
    db.conn.close()
    calls = []

    class Finished:
        def poll(self):
            return 0

    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **_kwargs: calls.append(command) or Finished())
    window = ui.MainWindow(database_path=path)
    try:
        assert [command[-2] for command in calls] == ["scripts.sync_analytics", "scripts.sync_cash"]
        assert window._trade_flex_display_status() == "paused"
        window._update_status()
        assert '>paused</a>' in window.trade_sync_status.text()
        notices = []
        monkeypatch.setattr(ui.QMessageBox, "warning", lambda _w, _title, message: notices.append(message))
        window.show_trade_sync_details()
        assert "Automatic Trades sync is paused" in notices[0]

        # Settings Apply queues configured syncs with manual=True internally.
        window.apply_settings()
        window._launch_flex_sync(manual=True)
        assert all(command[-2] != "scripts.sync_trades" for command in calls)
        assert trade_sync_paused(window.db)

        # A paused Trades query must not starve the other daily sources.
        calls.clear()
        window._sync_all_queue = []
        window._sync_all_active = False
        window._analytics_process = window._cash_process = None
        window._last_analytics_sync_attempt = 0
        window._periodic_refresh()
        assert [command[-2] for command in calls] == ["scripts.sync_analytics"]
        window.db.set_metadata("ibkr_analytics_sync_completed", date.today().isoformat())
        window._periodic_refresh()
        assert calls[-1][-2] == "scripts.sync_cash"
        assert trade_sync_paused(window.db)

        calls.clear()
        window.sync_all()
        assert "scripts.sync_trades" in [command[-2] for command in calls]
    finally:
        window.close()
