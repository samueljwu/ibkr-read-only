"""Nonblocking release check and verified installer download."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.platform.app_update import RELEASES_API, MAX_DOWNLOAD_BYTES, installed_app_path, select_update
from app.version import VERSION
from app.views import widgets


class UpdateDialog(QDialog):
    def __init__(self, install_ready, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update IBKR Dashboard")
        self.setMinimumWidth(380)
        self._install_ready = install_ready
        self._manager = QNetworkAccessManager(self)
        self._reply = None
        self._release_bytes = bytearray()
        self._update = None
        self._download_dir = None
        self._download_file = None
        self._received = 0
        self._hash = None
        self._installing = False

        layout = QVBoxLayout(self)
        self.message = QLabel(f"Checking for updates to version {VERSION}…")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        progress_row = QHBoxLayout()
        self.progress = widgets.progress_bar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        progress_row.addWidget(self.progress, 1)
        self.progress_percent = QLabel("0%")
        self.progress_percent.setObjectName("progressPercent")
        self.progress_percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.progress_percent.setVisible(False)
        progress_row.addWidget(self.progress_percent)
        layout.addLayout(progress_row)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.action = QPushButton("Download and install")
        self.action.setEnabled(False)
        self.action.clicked.connect(self._download)
        buttons.addWidget(self.action)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self._check)

    def _request(self, url, accept, timeout_ms):
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", f"IBKRDashboard/{VERSION}".encode("ascii"))
        request.setRawHeader(b"Accept", accept)
        request.setTransferTimeout(timeout_ms)
        return self._manager.get(request)

    def _check(self):
        self._reply = self._request(RELEASES_API, b"application/vnd.github+json", 30000)
        self._reply.readyRead.connect(self._read_release)
        self._reply.finished.connect(self._checked)

    def _read_release(self):
        self._release_bytes.extend(bytes(self._reply.readAll()))
        if len(self._release_bytes) > 1024 * 1024:
            self._reply.abort()

    def _checked(self):
        reply = self._reply
        self._read_release()
        self._reply = None
        try:
            if reply.error() != QNetworkReply.NoError:
                raise ValueError(reply.errorString())
            update = select_update(json.loads(self._release_bytes))
            if update is None:
                self.message.setText(f"IBKR Dashboard {VERSION} is up to date.")
                return
            self._update = update
            if installed_app_path() is None:
                self.message.setText(
                    f"Version {update.version} is available. Automatic installation requires an installed desktop build.")
                return
            self.message.setText(f"Version {update.version} is available. Download and install it now?")
            self.action.setEnabled(True)
        except (ValueError, json.JSONDecodeError) as exc:
            self.message.setText(f"Could not check for updates: {exc}")
        finally:
            reply.deleteLater()

    def _download(self):
        self.action.setEnabled(False)
        self.message.setText(f"Downloading version {self._update.version}…")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.progress_percent.setText("0%")
        self.progress_percent.setVisible(True)
        try:
            self._download_dir = Path(tempfile.mkdtemp(prefix="ibkr-dashboard-update-"))
            self._download_file = (self._download_dir / self._update.name).open("wb")
        except OSError as exc:
            self._failed(exc)
            return
        self._hash = hashlib.sha256()
        self._received = 0
        self._reply = self._request(self._update.url, b"application/octet-stream", 120000)
        self._reply.readyRead.connect(self._read_download)
        self._reply.finished.connect(self._downloaded)
        self._reply.downloadProgress.connect(self._show_progress)

    def _read_download(self):
        if self._download_file is None:
            return
        chunk = bytes(self._reply.readAll())
        self._received += len(chunk)
        if self._received > min(self._update.size, MAX_DOWNLOAD_BYTES):
            self._reply.abort()
            return
        try:
            self._download_file.write(chunk)
        except OSError:
            self._reply.abort()
            return
        self._hash.update(chunk)

    def _show_progress(self, received, total):
        if total > 0:
            value = min(100, int(received * 100 / total))
            self.progress.setValue(value)
            self.progress_percent.setText(f"{value}%")

    def _downloaded(self):
        reply = self._reply
        self._read_download()
        self._reply = None
        try:
            if self._download_file is not None:
                self._download_file.close()
                self._download_file = None
            if reply.error() != QNetworkReply.NoError:
                raise ValueError(reply.errorString())
            if self._received != self._update.size or self._hash.hexdigest() != self._update.sha256:
                raise ValueError("The download did not match the release checksum.")
            installed = installed_app_path()
            if installed is None:
                raise ValueError("The installed application could not be found.")
            path = self._download_dir / self._update.name
            self._installing = True
            self._install_ready(path, installed)
        except (OSError, ValueError) as exc:
            self._failed(exc)
        finally:
            reply.deleteLater()

    def _failed(self, exc):
        self._cleanup_download()
        self.progress.setVisible(False)
        self.progress_percent.setVisible(False)
        self.message.setText(f"Could not install update: {exc}")
        self.action.setEnabled(self._update is not None and installed_app_path() is not None)
        self._installing = False

    def _cleanup_download(self):
        if self._download_file is not None:
            self._download_file.close()
            self._download_file = None
        if self._download_dir is not None:
            shutil.rmtree(self._download_dir, ignore_errors=True)
            self._download_dir = None

    def closeEvent(self, event):
        if self._reply is not None:
            reply = self._reply
            self._reply = None
            for signal in (reply.readyRead, reply.finished, reply.downloadProgress):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            reply.abort()
            reply.deleteLater()
        if not self._installing:
            self._cleanup_download()
        super().closeEvent(event)
