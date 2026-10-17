"""Synthetic UI coverage: credential handling and asynchronous dated imports."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCalendarWidget, QLineEdit

import app.ui as ui
from app.flex.flex_import import oldest_flex_date
from app.platform.history_status import pause_request_path, read_status, write_status


TOKEN = "0000000000000000"


@pytest.fixture
def window(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    store = {}
    monkeypatch.setattr(ui.secret_store, "load_flex_token", lambda account: store.get(account))
    monkeypatch.setattr(ui.secret_store, "save_flex_token", lambda account, token: store.update({account: token}))
    result = ui.MainWindow(tmp_path / "synthetic.sqlite3")
    result.test_store = store
    result.test_warnings = []
    monkeypatch.setattr(ui.QMessageBox, "warning", lambda *args: result.test_warnings.append(args[2]))
    yield result
    result._history_process = None
    result._sync_process = None
    result._analytics_process = None
    result._cash_process = None
    result.close()


def periods_table_text(dialog):
    """Flatten the Imported periods table into one string for substring checks."""
    table = dialog.periods_table
    return "\n".join(
        f"{table.item(row, 0).text()}: {table.item(row, 1).text()}"
        for row in range(table.rowCount()))


def configure(window):
    window.db.set_setting("ib_account_id", "SYNTHETIC")
    window.settings_fields["ib_account_id"].setText("SYNTHETIC")
    for key in ("flex_query_id", "analytics_flex_query_id", "cash_flex_query_id"):
        window.db.set_setting(key, "123")
    window.test_store["SYNTHETIC"] = TOKEN
    window.open_history_import()


def test_import_defaults_to_recent_30_completed_days(window):
    window.open_history_import()
    dialog = window.history_import_dialog
    assert dialog.to_date.date() == QDate.currentDate().addDays(-1)
    assert dialog.from_date.date().daysTo(dialog.to_date.date()) == 29


def test_dates_can_be_typed_pasted_and_committed_with_tab(window):
    window.open_history_import()
    dialog = window.history_import_dialog
    app = QApplication.instance()
    app.processEvents()
    typed = QDate.currentDate().addDays(-12)
    pasted = QDate.currentDate().addDays(-3)
    dialog.from_date.setFocus()
    app.processEvents()
    dialog.from_date.line_edit.selectAll()
    QTest.keyClicks(dialog.from_date.line_edit, typed.toString("yyyy-MM-dd"))
    QTest.keyClick(dialog.from_date.line_edit, Qt.Key_Tab)
    app.processEvents()
    assert dialog.from_date.date() == typed
    assert dialog.to_date.hasFocus()

    dialog.to_date.line_edit.selectAll()
    app.clipboard().setText(pasted.toString("yyyy-MM-dd"))
    QTest.keyClick(dialog.to_date.line_edit, Qt.Key_V, Qt.ControlModifier)
    QTest.keyClick(dialog.to_date.line_edit, Qt.Key_Tab)
    app.processEvents()
    assert dialog.to_date.date() == pasted
    assert dialog.from_date.calendar_button.isVisible() and dialog.to_date.calendar_button.isVisible()


def test_typed_leap_day_range_reaches_worker_arguments(window, monkeypatch):
    configure(window)
    dialog = window.history_import_dialog
    app = QApplication.instance()
    app.processEvents()
    year = QDate.currentDate().year() - 1
    while not QDate.isLeapYear(year):
        year -= 1
    first = QDate(year, 2, 29)
    last = QDate(year, 3, 1)

    dialog.from_date.setFocus()
    app.processEvents()
    dialog.from_date.line_edit.selectAll()
    QTest.keyClicks(dialog.from_date.line_edit, first.toString("yyyy-MM-dd"))
    QTest.keyClick(dialog.from_date.line_edit, Qt.Key_Tab)
    dialog.to_date.line_edit.selectAll()
    app.clipboard().setText(last.toString("yyyy-MM-dd"))
    QTest.keyClick(dialog.to_date.line_edit, Qt.Key_V, Qt.ControlModifier)
    QTest.keyClick(dialog.to_date.line_edit, Qt.Key_Tab)
    app.processEvents()

    launches = []
    process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or process)
    window.start_history_import()
    assert window._history_process is process
    command = launches[0]
    assert command[command.index("--from-date") + 1] == first.toString("yyyy-MM-dd")
    assert command[command.index("--to-date") + 1] == last.toString("yyyy-MM-dd")


def test_calendar_picker_keeps_yesterday_limit(window):
    window.open_history_import()
    dialog = window.history_import_dialog
    dialog.from_date.calendar_button.click()
    QApplication.instance().processEvents()
    calendar = dialog.from_date.findChild(QCalendarWidget)
    assert calendar is not None
    assert calendar.maximumDate() == QDate.currentDate().addDays(-1)
    selected = QDate.currentDate().addDays(-8)
    calendar.clicked.emit(selected)
    QApplication.instance().processEvents()
    assert dialog.from_date.date() == selected


def test_import_dialog_keeps_explanation_and_results_compact(window):
    window.open_history_import()
    dialog = window.history_import_dialog
    QApplication.instance().processEvents()
    assert not dialog.help_text.isVisible()
    assert not dialog.progress_card.isVisible()
    assert not dialog.details.isVisible()
    assert not dialog.pause_button.isVisible()
    assert not dialog.progress_label.isVisible()
    assert dialog.periods_table.isVisible()
    assert oldest_flex_date().isoformat() in dialog.older_card_title.text()
    assert dialog.import_button.width() < dialog.width() // 2
    dialog.help_button.click()
    assert dialog.help_text.isVisible()
    assert "saved Flex token in Settings" in dialog.help_text.text()
    assert "Cash combines adjacent saved periods" in dialog.help_text.text()
    assert "backups" in dialog.help_text.text()
    dialog.show_status({"state": "needs_review", "started_sources": [],
                        "message": "Synthetic correction requires review."})
    assert dialog.progress_card.isVisible()
    assert dialog.progress_label.isVisible()
    assert dialog.details.isVisible()
    assert "Synthetic correction requires review." in dialog.details.toPlainText()


def test_token_stays_out_of_settings_and_is_never_prefilled(window):
    configure(window)
    assert window.flex_token_input.text() == ""
    assert window.flex_token_input.echoMode() == QLineEdit.Password
    assert "token" not in " ".join(window.settings_fields)
    window.flex_token_input.setText(TOKEN)
    window.save_flex_token()
    assert window.test_store == {"SYNTHETIC": TOKEN}
    assert window.flex_token_input.text() == ""
    assert "configured" in window.flex_token_status.text()
    assert TOKEN not in window.flex_token_status.text()
    assert TOKEN.encode() not in window.db.path.read_bytes()
    window.open_settings()
    assert window.flex_token_input.text() == ""
    window.flex_token_input.setText(TOKEN)
    window.settings_dialog.reject()
    assert window.flex_token_input.text() == ""


def test_token_replacement_and_account_binding(window):
    configure(window)
    window.flex_token_input.setText("1234567890123456")
    window.save_flex_token()
    assert window.test_store["SYNTHETIC"] == "1234567890123456"
    window.settings_fields["ib_account_id"].setText("OTHER")
    assert "Apply" in window.flex_token_status.text()
    window.flex_token_input.setText(TOKEN)
    window.save_flex_token()
    assert "Apply" in window.test_warnings[-1]
    assert "OTHER" not in window.test_store
    assert window.flex_token_input.text() == ""


def test_credential_backend_failure_never_displays_token(window, monkeypatch):
    configure(window)

    def fail(*args):
        raise ValueError("Backend failure " + TOKEN)

    monkeypatch.setattr(ui.secret_store, "save_flex_token", fail)
    window.flex_token_input.setText(TOKEN)
    window.save_flex_token()
    assert "credential store" in window.test_warnings[-1]
    assert TOKEN not in window.test_warnings[-1]
    assert window.flex_token_input.text() == ""


@pytest.mark.parametrize("invalid", ["", "short", "123456789", "９８７６５４３２１０１２３４５６"])
def test_invalid_token_is_cleared_without_replacing_saved_token(window, invalid):
    configure(window)
    window.flex_token_input.setText(invalid)
    window.save_flex_token()
    assert window.test_store == {"SYNTHETIC": TOKEN}
    assert "valid numeric Flex token" in window.test_warnings[-1]
    assert window.flex_token_input.text() == ""


@pytest.mark.parametrize("missing", ["ib_account_id", "flex_query_id", "analytics_flex_query_id", "cash_flex_query_id", "token"])
def test_import_requires_account_queries_and_saved_token(window, monkeypatch, missing):
    configure(window)
    if missing == "token":
        window.test_store.clear()
    else:
        window.db.set_setting(missing, "")
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    window.start_history_import()
    assert window.test_warnings
    assert not launches


def test_import_rejects_reversed_dates_and_today(window, monkeypatch):
    configure(window)
    dialog = window.history_import_dialog
    dialog.from_date.setDate(dialog.to_date.date().addDays(0))
    dialog.to_date.setDate(dialog.from_date.date().addDays(-1))
    window.start_history_import()
    assert "ordered date range" in window.test_warnings[-1]
    dialog.to_date.setMaximumDate(QDate.currentDate())
    dialog.to_date.setDate(QDate.currentDate())
    window.start_history_import()
    assert "ending before today" in window.test_warnings[-1]
    assert window._history_process is None


def test_import_rejects_incomplete_typed_date(window):
    configure(window)
    dialog = window.history_import_dialog
    dialog.from_date.line_edit.selectAll()
    QTest.keyClicks(dialog.from_date.line_edit, "2026-02-")
    window.start_history_import()
    assert "valid dates as YYYY-MM-DD" in window.test_warnings[-1]
    assert window._history_process is None


@pytest.mark.parametrize("busy", ["_sync_process", "_analytics_process", "_cash_process", "_live_sync_pending", "_sync_all_active", "_sync_all_queue"])
def test_import_waits_for_active_sync(window, busy):
    configure(window)
    setattr(window, busy, SimpleNamespace(poll=lambda: None) if busy.endswith("process") else True)
    window.start_history_import()
    assert "current IBKR sync" in window.test_warnings[-1]
    assert window._history_process is None


def test_launch_progress_result_and_sync_guards(window, monkeypatch):
    configure(window)
    dialog = window.history_import_dialog
    with window.db.conn:
        window.db.conn.execute("""INSERT INTO broker_sync_windows
            (account_id,source,from_day,to_day,status)
            VALUES ('SYNTHETIC','flex','2025-11-01','2025-11-30','complete')""")
    window.open_history_import()
    assert "Trades: 2025-11-01 to 2025-11-30" in periods_table_text(dialog)
    launches = []
    process = SimpleNamespace(code=None)
    process.poll = lambda: process.code
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs: launches.append((command, kwargs)) or process)
    window.start_history_import()
    assert window._history_process is process
    command, kwargs = launches[0]
    assert command[1:3] == ["-m", "scripts.history_worker"]
    assert command[command.index("--database") + 1] == str(window.db.path)
    assert command[command.index("--source") + 1] == "all"
    assert TOKEN not in " ".join(command)
    assert kwargs["creationflags"] == getattr(ui.subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["stdout"] == kwargs["stderr"] == ui.subprocess.DEVNULL
    assert not window.sync_button.isEnabled()
    assert window.sync_button.text() == ""
    assert window.sync_button.accessibleName() == "Importing Flex history…"
    assert window.sync_progress_label.text() == "Importing Flex history…"
    assert not dialog.import_button.isEnabled()
    for launch in (window.sync_all, window._queue_configured_syncs, window._launch_flex_sync,
                   window._launch_cash_sync, window._launch_analytics_sync):
        launch()
    assert len(launches) == 1
    window.apply_settings()
    assert "Wait" in window.test_warnings[-1]
    status = read_status(window._history_status_path)
    status.update(state="running", active_source="trades", started_sources=["trades"],
                  current_window=["2026-01-01", "2026-01-14"])
    write_status(window._history_status_path, status)
    window._poll_history_import()
    assert "Trades" in dialog.progress_label.text()
    assert "2026-01-14" in dialog.progress_label.text()
    assert "Trades: 2025-11-01 to 2025-11-30" in periods_table_text(dialog)
    status.update(state="completed_with_gaps", active_source=None, current_window=None,
                  completed_windows={"trades": [["2026-01-01", "2026-01-14"]]},
                  unavailable_windows={"trades": [["2025-12-18", "2025-12-31"]]})
    with window.db.conn:
        window.db.conn.execute("""INSERT INTO broker_sync_windows
            (account_id,source,from_day,to_day,status)
            VALUES ('SYNTHETIC','flex','2026-01-01','2026-01-14','complete')""")
    write_status(window._history_status_path, status)
    refreshed = []
    monkeypatch.setattr(window, "_refresh_models", lambda: refreshed.append("models"))
    monkeypatch.setattr(window.cash_tab, "refresh", lambda: refreshed.append("cash"))
    monkeypatch.setattr(window.analytics_tab, "refresh", lambda: refreshed.append("analytics"))
    process.code = 0
    window._poll_history_import()
    assert refreshed == ["models", "cash", "analytics"]
    assert window._history_process is None
    assert window.sync_button.isEnabled() and dialog.import_button.isEnabled()
    assert "Not available: 2025-12-18 to 2025-12-31" in dialog.details.toPlainText()
    assert ("Trades: 2025-11-01 to 2025-11-30, 2026-01-01 to 2026-01-14"
            in periods_table_text(dialog))
    assert "completed with gaps" in dialog.progress_label.text()
    assert TOKEN not in window._history_status_path.read_text()


def test_closing_app_interrupts_import_and_preserves_completed_progress(window, monkeypatch):
    configure(window)

    class Process:
        code = None
        terminated = False

        def poll(self):
            return self.code

        def terminate(self):
            self.terminated = True
            self.code = -15

        def wait(self, timeout):
            assert timeout == 3
            return self.code

    process = Process()
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: process)
    window.start_history_import()
    status_path = window._history_status_path
    status = read_status(status_path)
    status.update(state="running", pid=1234, started_sources=["trades"], active_source="trades",
                  current_window=["2026-01-15", "2026-01-28"],
                  completed_windows={"trades": [["2026-01-01", "2026-01-14"]]})
    write_status(status_path, status)
    pause_request_path(status_path).touch()

    assert window.close()
    assert process.terminated
    assert window._history_process is None
    assert not pause_request_path(status_path).exists()
    saved = read_status(status_path)
    assert saved["state"] == "interrupted"
    assert saved["pid"] is None
    assert saved["failed_window"] == ["2026-01-15", "2026-01-28"]
    assert saved["completed_windows"]["trades"] == [["2026-01-01", "2026-01-14"]]
    assert TOKEN not in status_path.read_text()


def test_pause_and_resume_reuse_the_import_status(window, monkeypatch):
    configure(window)
    processes = [SimpleNamespace(code=None), SimpleNamespace(code=None)]
    for process in processes:
        process.poll = lambda process=process: process.code
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or processes[len(launches) - 1])

    window.start_history_import()
    status_path = window._history_status_path
    dialog = window.history_import_dialog
    assert dialog.pause_button.isEnabled()
    dialog.pause_button.click()
    assert pause_request_path(status_path).exists()
    assert not dialog.pause_button.isEnabled()
    assert "Pausing" in dialog.progress_label.text()
    window._poll_history_import()
    assert "Pausing" in dialog.progress_label.text()

    status = read_status(status_path)
    status.update(state="paused", completed_windows={"trades": [["2026-01-01", "2026-01-14"]]})
    write_status(status_path, status)
    processes[0].code = 0
    window._poll_history_import()
    assert dialog.import_button.text() == "Resume import"
    assert dialog.import_button.isEnabled()
    assert "Import paused" in dialog.details.toPlainText()

    dialog.import_button.click()
    assert window._history_status_path == status_path
    assert len(launches) == 2
    assert not pause_request_path(status_path).exists()
    assert window._history_process is processes[1]


def test_global_pause_resumes_only_the_import_it_paused(window, monkeypatch):
    configure(window)
    processes = [SimpleNamespace(code=None), SimpleNamespace(code=None)]
    for process in processes:
        process.poll = lambda process=process: process.code
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or processes[len(launches) - 1])

    window.start_history_import()
    status_path = window._history_status_path
    assert window._history_process is processes[0]
    window.pause_sync_button.click()
    assert pause_request_path(status_path).exists()
    assert window._global_paused_history_path == status_path
    assert not window.sync_button.isEnabled()
    window.start_history_import()
    assert len(launches) == 1

    status = read_status(status_path)
    status.update(state="paused")
    write_status(status_path, status)
    processes[0].code = 0
    window._poll()
    assert window._history_process is None
    assert len(launches) == 1
    window.pause_sync_button.click()
    assert len(launches) == 2
    assert window._history_process is processes[1]
    assert window._history_status_path == status_path


def test_global_resume_leaves_a_manually_paused_import_paused(window, monkeypatch):
    configure(window)
    process = SimpleNamespace(code=None)
    process.poll = lambda: process.code
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or process)

    window.start_history_import()
    status_path = window._history_status_path
    window.pause_history_import()
    window.pause_sync_button.click()
    assert window._global_paused_history_path is None
    status = read_status(status_path)
    status.update(state="paused")
    write_status(status_path, status)
    process.code = 0
    window._poll()
    window.pause_sync_button.click()
    assert len(launches) == 1
    assert window._history_process is None
    assert window.history_import_dialog.import_button.text() == "Resume import"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_worker_exit_without_final_status_is_failure(window, monkeypatch, exit_code):
    configure(window)
    process = SimpleNamespace(poll=lambda: exit_code)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: process)
    window.start_history_import()
    window._poll_history_import()
    assert "failed" in window.history_import_dialog.progress_label.text()
    assert "without a completion result" in window.history_import_dialog.details.toPlainText()
    assert window.history_import_dialog.import_button.isEnabled()


def test_worker_launch_failure_is_visible_and_retryable(window, monkeypatch):
    configure(window)

    def fail(*args, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(ui.subprocess, "Popen", fail)
    window.start_history_import()
    assert window._history_process is None
    assert window.history_import_dialog.import_button.isEnabled()
    assert "Could not launch" in window.history_import_dialog.details.toPlainText()
    assert read_status(window._history_status_path)["state"] == "failed"


def test_worker_review_result_remains_visible_after_nonzero_exit(window, monkeypatch):
    configure(window)
    process = SimpleNamespace(code=None)
    process.poll = lambda: process.code
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: process)
    window.start_history_import()
    status = read_status(window._history_status_path)
    status.update(state="needs_review", active_source="cash", started_sources=["cash"],
                  review_windows={"cash": [["2026-01-01", "2026-01-30"]]},
                  message="Needs review: Cash report omits an existing currency.")
    write_status(window._history_status_path, status)
    process.code = 1
    window._poll_history_import()
    assert "needs review" in window.history_import_dialog.progress_label.text()
    assert "Needs review:" in window.history_import_dialog.details.toPlainText()
    assert read_status(window._history_status_path)["state"] == "needs_review"


def test_worker_failure_keeps_partial_results_and_failed_window(window, monkeypatch):
    configure(window)
    process = SimpleNamespace(code=None)
    process.poll = lambda: process.code
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *args, **kwargs: process)
    window.start_history_import()
    status = read_status(window._history_status_path)
    status.update(state="failed", active_source="cash", started_sources=["cash"],
                  completed_windows={"cash": [["2026-01-15", "2026-01-28"]]},
                  failed_window=["2026-01-01", "2026-01-14"],
                  message="Synthetic cash period overlaps an imported period.")
    write_status(window._history_status_path, status)
    process.code = 1
    window._poll_history_import()
    details = window.history_import_dialog.details.toPlainText()
    assert "Imported: 2026-01-15 to 2026-01-28" in details
    assert "Failed source: Cash" in details
    assert "Failed window: 2026-01-01 to 2026-01-14" in details
    assert "overlaps" in details


def test_frozen_launch_and_dispatch(window, monkeypatch):
    configure(window)
    launches = []
    monkeypatch.setattr(ui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs: launches.append(command) or SimpleNamespace(poll=lambda: None))
    window.history_import_dialog.source.setCurrentIndex(1)
    window.start_history_import()
    command = launches[0]
    assert command[1] == "--history-worker"
    assert command[command.index("--source") + 1] == "trades"
    import main
    from scripts import history_worker
    calls = []
    monkeypatch.setattr(history_worker, "main", lambda args: calls.append(args) or 0)
    assert main.main(command[1:]) == 0
    assert calls == [command[2:]]


def test_older_csv_launch_and_monthly_daily_boundary(window, monkeypatch):
    configure(window)
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileNames",
                        lambda *args: (["C:/synthetic/statement.csv",
                                        "C:/synthetic/portfolio.csv"], "CSV files (*.csv)"))
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or SimpleNamespace(poll=lambda: None))
    window.start_legacy_history_import()
    assert launches and "scripts.legacy_history_worker" in launches[0]
    assert launches[0][launches[0].index("--reports") + 1:] == [
        "C:/synthetic/statement.csv", "C:/synthetic/portfolio.csv"]
    assert window.history_import_dialog.older_import_button.isEnabled() is False
    assert window.history_import_dialog.pause_button.isVisible() is False

    with window.db.conn:
        window.db.conn.execute(
            """INSERT INTO account_monthly_history
               (portfolio_id,account_id,month,period_from,period_to,nav_usd,return_pct,source_hash)
               VALUES (1,'SYNTHETIC','2021-04','2021-04-13','2021-04-30','100','1.5','synthetic')""")
        window.db.conn.execute(
            """INSERT INTO legacy_account_performance
               (portfolio_id,account_id,period_from,period_to,cumulative_return_pct,
                ending_nav_usd,measure,source_hash)
               VALUES (1,'SYNTHETIC','2021-04-13','2026-10-14','10.0','120','TWR','synthetic')""")
    window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.monthly_page)
    window.analytics_tab.refresh()
    monthly = window.analytics_tab.monthly_table
    assert monthly.rowCount() == 1
    assert monthly.item(0, 0).text() == "2021-04"
    assert "since-inception TWR return" in window.analytics_tab.reported_performance_monthly.text()
    window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.pnl_page)
    window.analytics_tab.range_combo.setCurrentText("All")
    assert "Daily analysis unavailable for 2021" in window.analytics_tab.pnl_hint.text()
    assert window.analytics_tab.stats["Adjusted performance"].text() == "—"
    assert not window.analytics_tab.reported_performance_pnl.isHidden()


def test_monthly_tab_extends_older_history_with_latest_flex_close(window):
    configure(window)
    with window.db.conn:
        window.db.conn.execute(
            """INSERT INTO account_monthly_history
               (portfolio_id,account_id,month,period_from,period_to,nav_usd,return_pct,source_hash)
               VALUES (1,'SYNTHETIC','2026-01','2026-01-01','2026-01-30','1000','1.5','synthetic')""")
    window.db.save_account_history(1, "2026-01-30", 1000, source="flex", flow_covered=True)
    window.db.save_account_history(1, "2026-02-02", 1010, source="flex", flow_covered=True)
    window.db.save_account_history(1, "2026-02-03", 1020, source="flex", flow_covered=True)
    window.db.set_account_value(1, "NetLiquidation", 1100, "USD", "2026-03-02T12:00:00")
    window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.monthly_page)
    window.analytics_tab.refresh()
    monthly = window.analytics_tab.monthly_table
    assert monthly.rowCount() == 2
    assert monthly.item(0, 0).text() == "2026-02"
    assert monthly.item(0, 1).text() == "2026-02-02 to 2026-02-03"
    assert monthly.item(0, 3).raw_value == pytest.approx(2.0)
    assert monthly.item(0, 4).text() == "Flex"
    assert monthly.item(1, 0).text() == "2026-01"
    assert monthly.item(1, 4).text() == "PortfolioAnalyst"


def test_older_csv_picker_requires_both_reports(window, monkeypatch):
    configure(window)
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileNames",
                        lambda *args: (["C:/synthetic/portfolio.csv"], "CSV files (*.csv)"))
    window.start_legacy_history_import()
    assert window._history_process is None
    assert "Select both CSV reports" in window.test_warnings[-1]


def test_older_csv_removal_action_is_confirmed(window, monkeypatch):
    configure(window)
    dialog = window.history_import_dialog
    assert dialog.remove_older_button.isHidden()
    with window.db.conn:
        window.db.conn.execute(
            """INSERT INTO account_monthly_history
               (portfolio_id,account_id,month,period_from,period_to,nav_usd,return_pct,source_hash)
               VALUES (1,'SYNTHETIC','2021-04','2021-04-13','2021-04-30','100','1.5','synthetic')""")
    window._refresh_history_import_coverage()
    assert dialog.remove_older_button.isVisible()
    launches = []
    monkeypatch.setattr(ui.subprocess, "Popen", lambda command, **kwargs:
                        launches.append(command) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: ui.QMessageBox.No)
    dialog.remove_older_button.click()
    assert launches == []
    monkeypatch.setattr(ui.QMessageBox, "question", lambda *args: ui.QMessageBox.Yes)
    dialog.remove_older_button.click()
    assert launches and launches[0][-1] == "--remove"
    assert dialog.remove_older_button.isEnabled() is False
    dialog.show_status({"source": "legacy", "action": "remove", "state": "completed",
                        "legacy_result": {"months": 1, "statements": 0, "opening_positions": 0}})
    assert "Older CSV history removed" in dialog.progress_label.text()
