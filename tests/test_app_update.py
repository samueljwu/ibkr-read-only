"""Synthetic release metadata and installer launch checks."""

import hashlib
import json
import sys

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication

from app.platform import app_update
from app.views import update_dialog


def release(version="1.2.0", *, platform="win32"):
    name = app_update.installer_name(version, platform)
    return {"tag_name": f"v{version}", "draft": False, "prerelease": False,
            "assets": [{"name": name, "state": "uploaded", "size": 123,
                        "digest": "sha256:" + "a" * 64,
                        "browser_download_url":
                            f"https://github.com/{app_update.REPOSITORY}/releases/download/v{version}/{name}"}]}


def test_selects_newer_verified_platform_installer():
    result = app_update.select_update([release()], current="1.1.1", platform="win32")
    assert result.version == "1.2.0"
    assert result.sha256 == "a" * 64
    assert result.name.endswith("windows-x64-setup.exe")
    assert app_update.select_update([release()], current="1.2.0", platform="win32") is None
    assert app_update.select_update([release("1.2.0")], current="2.0.0", platform="win32") is None


def test_ignores_untagged_release_and_compares_highest_version():
    untagged = {"tag_name": "untagged-80e451de01fddec64d92",
                "draft": False, "prerelease": False, "assets": []}
    assert app_update.select_update([untagged, release("1.1.0")],
                                    current="1.1.1", platform="win32") is None
    selected = app_update.select_update([untagged, release("1.2.0"), release("1.1.0")],
                                        current="1.1.1", platform="win32")
    assert selected.version == "1.2.0"
    with pytest.raises(ValueError, match="No versioned stable release"):
        app_update.select_update([untagged], current="1.1.1", platform="win32")


@pytest.mark.parametrize("change", [
    lambda r: r["assets"][0].update(digest=None),
    lambda r: r["assets"][0].update(size=0),
    lambda r: r["assets"][0].update(browser_download_url="https://example.com/setup.exe"),
    lambda r: r["assets"][0].update(browser_download_url=
                                     r["assets"][0]["browser_download_url"].replace(
                                         "github.com", "github.com.evil.example")),
    lambda r: r["assets"][0].update(name="other.exe"),
    lambda r: r.update(prerelease=True),
])
def test_rejects_untrusted_or_incomplete_release(change):
    payload = release()
    change(payload)
    with pytest.raises(ValueError):
        app_update.select_update([payload], current="1.1.1", platform="win32")


def test_mac_release_uses_its_native_dmg():
    result = app_update.select_update([release(platform="darwin")],
                                      current="1.1.1", platform="darwin")
    assert result.name.endswith("macos-arm64.dmg")


def test_automatic_update_requires_an_installed_windows_build(tmp_path, monkeypatch):
    executable = tmp_path / "IBKRDashboard.exe"
    executable.touch()
    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.sys, "executable", str(executable))
    assert app_update.installed_app_path() is None
    (tmp_path / "unins000.exe").touch()
    assert app_update.installed_app_path() == executable


def test_mac_installed_path_matches_name_in_dmg(tmp_path, monkeypatch):
    executable = tmp_path / "IBKR Dashboard.app" / "Contents" / "MacOS" / "IBKRDashboard"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(app_update.sys, "platform", "darwin")
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.sys, "executable", str(executable))
    assert app_update.installed_app_path() == tmp_path / "IBKR Dashboard.app"


def test_windows_launcher_targets_existing_install_folder(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(app_update.sys, "platform", "win32")
    monkeypatch.setattr(app_update.subprocess, "Popen",
                        lambda command, **options: launches.append((command, options)))
    installed = tmp_path / "Program Files" / "IBKRDashboard.exe"
    installer = tmp_path / "IBKRDashboard-1.2.0-windows-x64-setup.exe"
    app_update.launch_update(installer, installed)
    command, options = launches[0]
    assert command[:2] == ["powershell.exe", "-NoProfile"]
    assert str(installer) in command
    assert str(installed.parent) in command
    assert "Get-Process" in (tmp_path / "apply-update.ps1").read_text(encoding="utf-8")
    assert options["close_fds"] is True


def test_mac_launcher_stages_bundle_outside_running_app(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(app_update.sys, "platform", "darwin")
    monkeypatch.setattr(app_update.subprocess, "Popen",
                        lambda command, **options: launches.append((command, options)))
    installer = tmp_path / "IBKRDashboard-1.2.0-macos-arm64.dmg"
    installed = tmp_path / "IBKR Dashboard.app"
    app_update.launch_update(installer, installed)
    command, _options = launches[0]
    assert command[:2] == ["/bin/sh", str(tmp_path / "apply-update.sh")]
    assert command[2:4] == [str(installer), str(installed)]
    helper = (tmp_path / "apply-update.sh").read_text(encoding="utf-8")
    assert "hdiutil attach" in helper
    assert 'mv "$stage" "$target"' in helper


class FakeReply(QObject):
    readyRead = Signal()
    finished = Signal()
    downloadProgress = Signal(int, int)

    def __init__(self, data):
        super().__init__()
        self.data = data

    def readAll(self):
        data, self.data = self.data, b""
        return data

    def error(self):
        return QNetworkReply.NoError

    def errorString(self):
        return "network error"

    def abort(self):
        pass


def test_dialog_treats_unversioned_latest_release_as_no_new_update(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_dialog.QTimer, "singleShot", lambda *_args: None)
    feed = [{"tag_name": "untagged-80e451de01fddec64d92", "assets": []},
            release("1.1.0")]
    dialog = update_dialog.UpdateDialog(lambda *_args: pytest.fail("Unexpected installation"))
    monkeypatch.setattr(dialog, "_request",
                        lambda *_args: FakeReply(json.dumps(feed).encode()))
    dialog._check()
    dialog._reply.finished.emit()
    assert "up to date" in dialog.message.text()
    assert not dialog.action.isEnabled()
    dialog.close()


@pytest.mark.parametrize("valid", [True, False])
def test_dialog_only_installs_a_matching_download(tmp_path, monkeypatch, valid):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_dialog.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(update_dialog, "installed_app_path",
                        lambda: tmp_path / "IBKRDashboard.exe")
    monkeypatch.setattr(update_dialog.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    content = b"synthetic installer bytes"
    metadata = release(platform=sys.platform)
    asset = metadata["assets"][0]
    asset["size"] = len(content)
    asset["digest"] = "sha256:" + hashlib.sha256(content if valid else b"other").hexdigest()
    replies = [FakeReply(json.dumps([metadata]).encode()), FakeReply(content)]
    installed = []
    dialog = update_dialog.UpdateDialog(lambda *args: installed.append(args))
    monkeypatch.setattr(dialog, "_request", lambda *_args: replies.pop(0))
    dialog._check()
    dialog._reply.finished.emit()
    assert dialog.action.isEnabled()
    dialog.action.click()
    dialog._reply.finished.emit()
    if valid:
        assert installed == [(tmp_path / asset["name"], tmp_path / "IBKRDashboard.exe")]
        assert installed[0][0].read_bytes() == content
    else:
        assert not installed
        assert "checksum" in dialog.message.text()
        assert not (tmp_path / asset["name"]).exists()
    dialog.close()


def test_closing_dialog_aborts_download_and_removes_partial_file(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_dialog.QTimer, "singleShot", lambda *_args: None)
    monkeypatch.setattr(update_dialog, "installed_app_path",
                        lambda: tmp_path / "IBKRDashboard.exe")
    download_dir = tmp_path / "download"
    monkeypatch.setattr(update_dialog.tempfile, "mkdtemp",
                        lambda **_kwargs: str(download_dir.mkdir() or download_dir))
    metadata = release(platform=sys.platform)
    replies = [FakeReply(json.dumps([metadata]).encode()), FakeReply(b"part")]
    dialog = update_dialog.UpdateDialog(lambda *_args: pytest.fail("Unexpected installation"))
    monkeypatch.setattr(dialog, "_request", lambda *_args: replies.pop(0))
    dialog._check()
    dialog._reply.finished.emit()
    dialog.action.click()
    dialog._reply.readyRead.emit()
    assert any(download_dir.iterdir())
    dialog.close()
    assert not download_dir.exists()
