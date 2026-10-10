"""Modeless, explicitly invoked update dialog; all I/O stays off the Qt thread."""

import threading
import time

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout

from app.platform.runtime import build_info
from app.updates.updater import NoUpdate, UpdateError, VerifiedFile, cleanup_attempt, discover, download, verify_file


class UpdateWorker(QThread):
    result = Signal(int, object)
    problem = Signal(int, str)
    progress = Signal(int, int, int)

    def __init__(self, generation, action, release=None, parent=None):
        super().__init__(parent)
        self.generation, self.action, self.release = generation, action, release
        self.cancelled = threading.Event()

    def run(self):
        result = None
        try:
            if self.action == "check":
                result = discover(build_info(), cancelled=self.cancelled.is_set)
            elif self.action == "download":
                result = download(self.release, build_info()["channel"], self.generation,
                                  cancelled=self.cancelled.is_set,
                                  progress=lambda count, total: self.progress.emit(self.generation, count, total))
                result = verify_file(result, cancelled=self.cancelled.is_set, deadline=time.monotonic() + 900)
            else:
                result = verify_file(self.release, cancelled=self.cancelled.is_set,
                                     deadline=time.monotonic() + 900, pin=True)
            if not self.cancelled.is_set():
                self.result.emit(self.generation, result)
            elif self.action == "download" and result is not None:
                cleanup_attempt(result)
            elif self.action == "finalize":
                cleanup_attempt(result if result is not None else self.release)
        except UpdateError as exc:
            if self.action == "download" and result is not None:
                cleanup_attempt(result)
            elif self.action == "finalize":
                cleanup_attempt(result if result is not None else self.release)
            if not self.cancelled.is_set():
                self.problem.emit(self.generation, str(exc))
        except Exception:
            if self.action == "download" and result is not None:
                cleanup_attempt(result)
            elif self.action == "finalize":
                cleanup_attempt(result if result is not None else self.release)
            if not self.cancelled.is_set():
                self.problem.emit(self.generation, "Update check failed; retry later")


class UpdateDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Check for updates")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.resize(520, 390)
        self.generation = 0
        self.worker = None
        self.workers = []
        self.release = None
        self.status = QLabel(self)
        self.identity = QLabel(self)
        info = build_info()
        self.identity.setText(f"Installed: {info['version']}  ·  {info['channel'].title()}")
        self.notes = QPlainTextEdit(self)
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes appear here as plain text")
        self.progress = QProgressBar(self)
        self.progress.hide()
        self.update_button = QPushButton("Update now", self)
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self.update_now)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(self.cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.identity)
        layout.addWidget(self.status)
        layout.addWidget(self.notes)
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        buttons.addWidget(self.update_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def check(self):
        if self.worker and self.worker.isRunning():
            return
        self.release = None
        self.update_button.setEnabled(False)
        self.notes.clear()
        self.status.setText("Checking public GitHub Releases…")
        self.progress.setRange(0, 0)
        self.progress.show()
        self._start("check")

    def _start(self, action, payload=None):
        self.generation += 1
        worker = UpdateWorker(self.generation, action, self.release if payload is None else payload, self)
        self.worker = worker
        self.workers.append(worker)
        worker.result.connect(self._result)
        worker.problem.connect(self._problem)
        worker.progress.connect(self._progress)
        worker.finished.connect(lambda w=worker: self._finished(w))
        worker.start()

    def update_now(self):
        if not self.release or self.worker and self.worker.isRunning():
            return
        reason = self.window.update_readiness()
        if reason:
            self.status.setText(reason)
            return
        self.status.setText("Downloading and verifying installer…")
        self.update_button.setEnabled(False)
        self.progress.setRange(0, self.release.size)
        self.progress.setValue(0)
        self.progress.show()
        self._start("download")

    def _result(self, generation, result):
        if generation != self.generation or not self.isVisible():
            if isinstance(result, VerifiedFile):
                cleanup_attempt(result)
            return
        self.progress.hide()
        if isinstance(result, NoUpdate):
            self.status.setText("No compatible public release is available" if result.reason == "no_compatible_release"
                                else "This installed channel is up to date")
        elif hasattr(result, "path") and self.worker and self.worker.action == "finalize":
            try:
                self.window.launch_verified_update(result, self)
            finally:
                if result.lease is not None:
                    result.lease.close()
        elif hasattr(result, "path"):
            self.status.setText("Installer verified. Closing the app to run Setup…")
            self.window.begin_update_handoff(result, self)
        else:
            self.release = result
            self.status.setText(f"Available: {result.version} · Update now closes the app and opens Setup")
            self.notes.setPlainText(result.notes)
            self.update_button.setEnabled(True)

    def _problem(self, generation, message):
        if generation == self.generation and self.worker and self.worker.action == "finalize" and self.window._update_handoff:
            self.window._rollback_update(message, self)
        elif generation == self.generation and self.isVisible():
            self.status.setText(message)
            self.progress.hide()
            self.update_button.setEnabled(self.release is not None)

    def _progress(self, generation, count, total):
        if generation == self.generation and self.isVisible():
            self.progress.setRange(0, total)
            self.progress.setValue(count)

    def finalize(self, result):
        self.status.setText("Rechecking installer before Setup…")
        self._start("finalize", result)

    def _finished(self, worker):
        if worker in self.workers:
            self.workers.remove(worker)
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()
        if self.window._update_close_wait and not self.workers:
            self.window.close()

    def cancel(self):
        if getattr(self.window, "_update_process", None) is not None:
            self.status.setText("Setup is starting. Finish or cancel it in the Setup window.")
            return
        self.generation += 1
        for worker in self.workers:
            worker.cancelled.set()
        if self.window._update_handoff:
            self.window._rollback_update("Update cancelled", self)
        self.progress.hide()
        if not self.window._update_handoff:
            self.status.setText("Cancelled")
        self.update_button.setEnabled(False)

    def closeEvent(self, event):
        if getattr(self.window, "_update_process", None) is not None:
            self.status.setText("Setup is starting. Finish or cancel it in the Setup window.")
            event.ignore()
            return
        self.cancel()
        super().closeEvent(event)
