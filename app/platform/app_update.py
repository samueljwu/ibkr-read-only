"""Release selection and installer launch for packaged desktop builds."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from app.version import VERSION


REPOSITORY = "samueljwu/ibkr-read-only"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=30"
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\Z")
_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")


@dataclass(frozen=True)
class AvailableUpdate:
    version: str
    name: str
    url: str
    size: int
    sha256: str


def _version_tuple(value):
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("The release has an unsupported version number.")
    return tuple(map(int, match.groups()))


def installer_name(version, platform=sys.platform):
    if platform == "win32":
        return f"IBKRDashboard-{version}-windows-x64-setup.exe"
    if platform == "darwin":
        return f"IBKRDashboard-{version}-macos-arm64.dmg"
    raise ValueError("Updates are available only for Windows x64 and macOS Apple silicon.")


def select_update(releases, *, current=VERSION, platform=sys.platform):
    """Choose the highest versioned stable release and its platform asset."""
    if not isinstance(releases, list):
        raise ValueError("The release service did not return a release list.")
    versioned = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if isinstance(tag, str) and _VERSION.fullmatch(tag):
            versioned.append((_version_tuple(tag), tag, release))
    if not versioned:
        raise ValueError("No versioned stable release was found.")
    latest, version, release = max(versioned, key=lambda item: item[0])
    if latest <= _version_tuple(current):
        return None
    normalized = version.removeprefix("v")
    name = installer_name(normalized, platform)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("The release has no installer assets.")
    asset = next((item for item in assets if isinstance(item, dict) and
                  item.get("name") == name and item.get("state") == "uploaded"), None)
    if asset is None:
        raise ValueError(f"Version {normalized} has no installer for this platform.")
    url = asset.get("browser_download_url")
    parts = urlsplit(url) if isinstance(url, str) else None
    expected_path = f"/{REPOSITORY}/releases/download/{version}/{name}"
    if (parts is None or parts.scheme != "https" or parts.netloc != "github.com" or
            parts.path != expected_path or
            parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("The release installer URL is invalid.")
    digest = asset.get("digest")
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        raise ValueError("The release installer has no SHA-256 digest.")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise ValueError("The release installer size is invalid.")
    return AvailableUpdate(normalized, name, url, size, match.group(1).lower())


def installed_app_path():
    """Find the installation this process can safely update."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    if sys.platform == "win32":
        return executable if executable.name == "IBKRDashboard.exe" and (
            executable.parent / "unins000.exe").is_file() else None
    if sys.platform == "darwin":
        return next((parent for parent in executable.parents
                     if parent.name in ("IBKRDashboard.app", "IBKR Dashboard.app")), None)
    return None


def launch_update(installer: Path, installed: Path):
    """Start the downloaded installer after Qt has closed all app windows."""
    if sys.platform == "win32":
        helper = installer.parent / "apply-update.ps1"
        helper.write_text("""param([string]$Installer, [string]$Destination, [int]$ParentPid)
$ErrorActionPreference = 'Stop'
try {
    while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        Start-Sleep -Milliseconds 250
    }
    $arguments = @('/SILENT', '/NORESTART', '/NORESTARTAPPLICATIONS',
                   ('/DIR="' + $Destination + '"'))
    $setup = Start-Process -FilePath $Installer -ArgumentList $arguments -Wait -PassThru
    if ($setup.ExitCode -ne 0) {
        throw "Installer exited with code $($setup.ExitCode)."
    }
    Remove-Item -LiteralPath $Installer -Force
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "The update could not be installed. The installer remains at:`n$Installer`n`n$_",
        'IBKR Dashboard update') | Out-Null
}
""", encoding="utf-8")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-File", str(helper),
                          str(installer), str(installed.parent), str(os.getpid())],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
    elif sys.platform == "darwin":
        # The helper runs outside the bundle so it can replace that bundle once
        # this process exits. If the install folder is protected, Finder opens
        # the verified DMG for the user to complete the copy.
        helper = installer.parent / "apply-update.sh"
        helper.write_text("""#!/bin/sh
set -eu
dmg=$1
target=$2
parent_pid=$3
while kill -0 "$parent_pid" 2>/dev/null; do sleep 1; done
if [ ! -w "$(dirname "$target")" ]; then open "$dmg"; exit; fi
mount=$(mktemp -d)
stage="${target}.update.$$"
backup="${target}.previous.$$"
cleanup() {
    hdiutil detach -quiet "$mount" 2>/dev/null || true
    rmdir "$mount" 2>/dev/null || true
    rm -rf "$stage"
}
trap cleanup EXIT HUP INT TERM
if ! hdiutil attach -quiet -readonly -nobrowse -mountpoint "$mount" "$dmg"; then
    open "$dmg"; exit
fi
if [ ! -d "$mount/IBKR Dashboard.app" ] || ! ditto "$mount/IBKR Dashboard.app" "$stage"; then
    open "$dmg"; exit
fi
if ! mv "$target" "$backup"; then open "$dmg"; exit; fi
if ! mv "$stage" "$target"; then
    mv "$backup" "$target"
    open "$dmg"
    exit
fi
open "$target"
rm -rf "$backup"
""", encoding="utf-8")
        subprocess.Popen(["/bin/sh", str(helper), str(installer), str(installed),
                          str(os.getpid())], close_fds=True, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise ValueError("This platform has no installer.")
