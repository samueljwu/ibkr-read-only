"""Public GitHub release discovery and bounded installer verification."""

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from app.updates.release_policy import installed_version, installer_name, is_newer, release_version

API = "https://api.github.com/repos/samueljwu/custom-ib-monitor/releases"
MAX_INSTALLER = 512 * 1024 * 1024
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "PortfolioMonitor-Windows-Updater"}
HEX = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")
CHECKSUM = re.compile(r"([0-9a-fA-F]{64})  ([A-Za-z0-9._-]+)\Z")


class UpdateError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    notes: str
    release_id: int
    asset_id: int
    name: str
    size: int
    url: str
    digest: str | None
    checksum_url: str | None
    checksum_digest: str | None
    checksum_size: int | None


@dataclass(frozen=True)
class VerifiedFile:
    path: Path
    release: Release
    sha256: str
    generation: int
    cache_root: Path | None = None
    file_identity: tuple[int, int, int, int] | None = None
    lease: object | None = None


@dataclass(frozen=True)
class NoUpdate:
    reason: str  # "no_compatible_release" or "up_to_date"


def _positive(value):
    return type(value) is int and value > 0


def _digest(value):
    if value is None:
        return None
    found = HEX.fullmatch(value) if isinstance(value, str) else None
    if not found:
        raise UpdateError("integrity", "Release contains an invalid SHA-256 digest")
    return found.group(1).lower()


def validate_download_url(url, tag, name, *, cdn=False):
    if not isinstance(url, str) or any(not 33 <= ord(c) <= 126 or c == "\\" for c in url):
        raise UpdateError("url", "Unsafe release URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        invalid_authority = parsed.username or parsed.password or parsed.port is not None
    except ValueError as exc:
        raise UpdateError("url", "Unsafe release URL") from exc
    if parsed.scheme != "https" or invalid_authority or parsed.fragment:
        raise UpdateError("url", "Unsafe release URL")
    host = parsed.hostname
    if parsed.netloc != host or host not in ({"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} if cdn else {"github.com"}):
        raise UpdateError("url", "Unapproved release host")
    if re.search(r"%|//|/\.|\.\.|\\", parsed.path, re.I):
        raise UpdateError("url", "Unsafe release path")
    if host == "github.com":
        expected = f"/samueljwu/custom-ib-monitor/releases/download/{tag}/{name}"
        if parsed.path != expected or parsed.query:
            raise UpdateError("url", "Release URL does not match the selected asset")
    elif not cdn or not parsed.path.startswith("/"):
        raise UpdateError("url", "Unapproved asset redirect")
    return url


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, tag, name):
        self.tag, self.name, self.hops = tag, name, 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.tag is None:
            # Discovery URLs are generated locally. Never send a request to a
            # destination supplied by an API response, even to another HTTPS host.
            raise UpdateError("url", "Unexpected API redirect")
        self.hops += 1
        if self.hops > 5:
            raise UpdateError("url", "Too many asset redirects")
        validate_download_url(newurl, self.tag, self.name, cdn=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, *, tag=None, name=None, timeout=15):
    opener = urllib.request.build_opener(ReleaseRedirect(tag, name))
    return opener.open(urllib.request.Request(url, headers=HEADERS), timeout=timeout)


def _response(open_url, url, *, tag=None, name=None):
    try:
        response = open_url(url, tag=tag, name=name, timeout=15)
        try:
            if tag:
                validate_download_url(response.geturl(), tag, name, cdn=True)
            elif response.geturl() != url:
                raise UpdateError("url", "Unexpected API redirect")
        except BaseException:
            response.close()
            raise
        return response
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("rate_limit", "GitHub release access is rate limited") from exc
        if exc.code == 404:
            raise UpdateError("unavailable", "Public repository or release is unavailable (404)") from exc
        raise UpdateError("http", f"GitHub returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as exc:
        raise UpdateError("offline", "Could not contact GitHub securely; check connection and retry") from exc


def _read_bounded(response, ceiling, cancelled=lambda: False, deadline=None, clock=time.monotonic):
    data = bytearray()
    while True:
        if cancelled():
            raise UpdateError("cancelled", "Update cancelled")
        if deadline is not None and clock() > deadline:
            raise UpdateError("timeout", "Update check or download timed out")
        try:
            chunk = response.read(min(65536, ceiling + 1 - len(data)))
        except (OSError, TimeoutError) as exc:
            raise UpdateError("offline", "Connection interrupted while reading GitHub response") from exc
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > ceiling:
            raise UpdateError("size", "Release response exceeds size limit")


def _asset(row, name, *, limit):
    matches = [a for a in row["assets"] if isinstance(a, dict) and a.get("name") == name]
    if len(matches) != 1:
        raise UpdateError("asset", f"Release needs exactly one {name} asset")
    asset = matches[0]
    if not _positive(asset.get("id")) or not _positive(asset.get("size")) or asset["size"] > limit or asset.get("state") != "uploaded":
        raise UpdateError("asset", f"Release asset {name} is incomplete or too large")
    url = validate_download_url(asset.get("browser_download_url"), row["tag_name"], name)
    return asset, url


def _release(row, channel):
    if not isinstance(row, dict) or type(row.get("draft")) is not bool or type(row.get("prerelease")) is not bool:
        raise UpdateError("metadata", "Malformed GitHub release metadata")
    version = release_version(row.get("tag_name"), channel, draft=row["draft"], prerelease=row["prerelease"])
    if version is None:
        return None
    if not _positive(row.get("id")) or not isinstance(row.get("assets"), list) or not isinstance(row.get("body"), (str, type(None))):
        raise UpdateError("metadata", "Malformed matching release metadata")
    if any(not isinstance(asset, dict) or not isinstance(asset.get("name"), str) for asset in row["assets"]):
        raise UpdateError("metadata", "Malformed matching release assets")
    name = installer_name(row["tag_name"], channel)
    asset, url = _asset(row, name, limit=MAX_INSTALLER)
    digest = _digest(asset.get("digest"))
    checksum_url = checksum_digest = checksum_size = None
    if digest is None:
        check, checksum_url = _asset(row, name.removesuffix(".exe") + ".sha256", limit=4096)
        checksum_digest, checksum_size = _digest(check.get("digest")), check["size"]
    return version, Release(row["tag_name"], row["tag_name"][1:], row.get("body") or "", row["id"],
                            asset["id"], name, asset["size"], url, digest, checksum_url,
                            checksum_digest, checksum_size)


def validate_release(release, channel):
    if (not isinstance(release, Release) or
            release_version(release.tag, channel, prerelease=channel == "dev") is None or
            release.version != release.tag[1:] or release.name != installer_name(release.tag, channel) or
            not _positive(release.release_id) or not _positive(release.asset_id) or
            not _positive(release.size) or release.size > MAX_INSTALLER):
        raise UpdateError("asset", "Selected release is incompatible with this channel")
    validate_download_url(release.url, release.tag, release.name)
    if release.digest is None:
        if not release.checksum_url or not _positive(release.checksum_size) or release.checksum_size > 4096:
            raise UpdateError("integrity", "Selected release has no valid checksum asset")
        validate_download_url(release.checksum_url, release.tag, release.name.removesuffix(".exe") + ".sha256")
    else:
        if not isinstance(release.digest, str):
            raise UpdateError("integrity", "Selected release digest is invalid")
        _digest("sha256:" + release.digest)
    return release


def _installed_version(info):
    if not isinstance(info, dict) or info.get("channel") not in ("stable", "dev"):
        raise UpdateError("version", "Installed build metadata is invalid; upgrade manually")
    if not getattr(sys, "frozen", False):
        raise UpdateError("source", "Source checkouts require a manually obtained compatible installer")
    if sys.platform != "win32" or __import__("struct").calcsize("P") != 8:
        raise UpdateError("platform", "Updates require installed Windows x64")
    channel = info.get("channel")
    current = installed_version(info.get("version"), channel)
    if current is None:
        raise UpdateError("version", "Installed version is unknown; upgrade manually")
    if info.get("platform", "windows") != "windows" or info.get("architecture", "x64") != "x64":
        raise UpdateError("platform", "Installed build is not Windows x64")
    if "release" in info and type(info["release"]) is not bool:
        raise UpdateError("version", "Installed publication metadata is invalid; upgrade manually")
    kind = info.get("publication_kind")
    if kind is not None:
        if kind not in ("stable", "dev", "local") or kind == "stable" and channel != "stable" or kind == "dev" and channel != "dev":
            raise UpdateError("version", "Installed publication metadata is invalid; upgrade manually")
        tag = info.get("publication_tag")
        if kind == "local" and tag is not None or kind != "local" and tag != "v" + info["version"]:
            raise UpdateError("version", "Installed publication tag is invalid; upgrade manually")
    return current


def discover(info, *, open_url=_open, clock=time.monotonic, cancelled=lambda: False):
    current = _installed_version(info)
    channel = info["channel"]
    deadline = clock() + 60
    best = None
    seen_channel = False
    for page in range(1, 11):
        url = f"{API}?per_page=100&page={page}"
        with _response(open_url, url) as response:
            raw = _read_bounded(response, 1024 * 1024, cancelled, deadline, clock)
        try:
            rows = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateError("metadata", "Malformed GitHub release response") from exc
        if not isinstance(rows, list) or len(rows) > 100:
            raise UpdateError("metadata", "Malformed GitHub release list")
        for row in rows:
            if cancelled():
                raise UpdateError("cancelled", "Update cancelled")
            if not isinstance(row, dict):
                raise UpdateError("metadata", "Malformed GitHub release entry")
            if type(row.get("draft")) is not bool or type(row.get("prerelease")) is not bool:
                raise UpdateError("metadata", "Malformed GitHub release state")
            candidate_version = release_version(row.get("tag_name"), channel,
                draft=row.get("draft"), prerelease=row.get("prerelease"))
            seen_channel |= candidate_version is not None
            if candidate_version is None or not is_newer(candidate_version, current):
                continue
            if best is None or candidate_version.order > best[0].order:
                best = _release(row, channel)
            elif candidate_version.order == best[0].order:
                raise UpdateError("metadata", "Ambiguous duplicate release version")
        if len(rows) < 100:
            return best[1] if best else NoUpdate("up_to_date" if seen_channel else "no_compatible_release")
    raise UpdateError("truncated", "Release search limit reached; check could not complete")


def parse_checksum(contents, name):
    try:
        lines = contents.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise UpdateError("integrity", "Checksum is not ASCII") from exc
    if len(lines) != 1:
        raise UpdateError("integrity", "Checksum must contain one installer entry")
    found = CHECKSUM.fullmatch(lines[0])
    if not found or found.group(2) != name:
        raise UpdateError("integrity", "Checksum does not name the exact installer")
    return found.group(1).lower()


def _cache_root(channel, localappdata=None):
    if channel not in ("stable", "dev"):
        raise UpdateError("platform", "Unknown build channel")
    root = Path(localappdata or os.environ.get("LOCALAPPDATA", ""))
    if not root.is_absolute():
        raise UpdateError("cache", "LOCALAPPDATA is unavailable")
    target = root / "CustomIBMonitor" / channel / "updates"
    if not root.is_dir() or root.is_symlink() or (os.name == "nt" and root.stat().st_file_attributes & 0x400):
        raise UpdateError("cache", "Unsafe LOCALAPPDATA directory")
    path = root
    for part in ("CustomIBMonitor", channel, "updates"):
        path = path / part
        if path.exists() and (path.is_symlink() or (os.name == "nt" and path.stat().st_file_attributes & 0x400)):
            raise UpdateError("cache", "Linked update cache path")
        path.mkdir(exist_ok=True)
    if not target.resolve().is_relative_to(root.resolve()):
        raise UpdateError("cache", "Update cache escapes LOCALAPPDATA")
    return target


def cleanup_expired(root, *, now=None, age_seconds=14 * 86400):
    """Remove only old updater-owned files, without traversing other content."""
    if (root.name != "updates" or root.parent.name not in ("stable", "dev") or
            root.parent.parent.name != "CustomIBMonitor" or root.is_symlink() or
            not root.is_dir() or
            (os.name == "nt" and root.stat().st_file_attributes & 0x400)):
        return
    now = time.time() if now is None else now
    for attempt in root.iterdir():
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", attempt.name) or attempt.is_symlink() or not attempt.is_dir():
                continue
            if os.name == "nt" and attempt.stat().st_file_attributes & 0x400:
                continue
            if now - attempt.stat().st_mtime < age_seconds:
                continue
            children = list(attempt.iterdir())
            if any(child.name not in ("installer.exe", "installer.part") or child.is_symlink() or not child.is_file()
                   or (os.name == "nt" and child.stat().st_file_attributes & 0x400) for child in children):
                continue
            for child in children:
                child.unlink()
            attempt.rmdir()
        except OSError:
            # A running Setup may still have the retained installer open.
            continue


def cleanup_attempt(item):
    """Remove just this updater attempt after cancellation or failed verification."""
    if item.lease is not None:
        item.lease.close()
    path = item.path
    root = item.cache_root
    if (root is None or root.name != "updates" or root.parent.name not in ("stable", "dev") or
            root.parent.parent.name != "CustomIBMonitor" or path.parent.parent != root or
            path.name != "installer.exe" or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)):
        return
    try:
        for component in (root, path.parent, path):
            if component.is_symlink() or (os.name == "nt" and component.stat().st_file_attributes & 0x400):
                return
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


def _pinned_reader(path):
    """Deny writes/deletes while the final hash is checked and Popen begins."""
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    kernel = ctypes.windll.kernel32
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path.absolute()), 0x80000000, 1, None, 3, 0x200000, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise UpdateError("integrity", "Cannot lock verified installer against changes")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


def verify_file(item, *, cancelled=lambda: False, clock=time.monotonic, deadline=None, pin=False):
    if item.cache_root is not None and (
            item.path.parent.parent != item.cache_root or
            item.path.parent.is_symlink() or
            (os.name == "nt" and item.path.parent.stat().st_file_attributes & 0x400)):
        raise UpdateError("integrity", "Verified installer cache path changed")
    if (item.path.is_symlink() or not item.path.is_file() or
            (os.name == "nt" and item.path.stat().st_file_attributes & 0x400) or
            item.path.stat().st_size != item.release.size):
        raise UpdateError("integrity", "Verified installer changed")
    stream = _pinned_reader(item.path) if pin else item.path.open("rb")
    retained = False
    try:
        before = os.fstat(stream.fileno())
        if (os.name == "nt" and before.st_file_attributes & 0x400) or before.st_size != item.release.size:
            raise UpdateError("integrity", "Verified installer changed")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(65536), b""):
            if cancelled():
                raise UpdateError("cancelled", "Update cancelled")
            if deadline is not None and clock() > deadline:
                raise UpdateError("timeout", "Installer verification timed out")
            digest.update(block)
        if cancelled():
            raise UpdateError("cancelled", "Update cancelled")
        if digest.hexdigest() != item.sha256:
            raise UpdateError("integrity", "Verified installer changed")
        after = os.fstat(stream.fileno())
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if identity(before) != identity(after):
            raise UpdateError("integrity", "Verified installer changed during hashing")
        retained = pin
        return replace(item, file_identity=identity(after), lease=stream if pin else None)
    finally:
        if not retained:
            stream.close()


def identity_matches(item):
    """Cheap final path check after the background rehash and before Popen."""
    try:
        if item.file_identity is None or item.path.is_symlink() or not item.path.is_file():
            return False
        if os.name == "nt" and getattr(sys, "frozen", False) and (
                item.lease is None or item.lease.closed):
            return False
        if item.cache_root is not None and (
                item.path.parent.parent != item.cache_root or
                item.cache_root.is_symlink() or item.path.parent.is_symlink()):
            return False
        if os.name == "nt" and item.path.stat().st_file_attributes & 0x400:
            return False
        st = item.path.stat()
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) == item.file_identity
    except OSError:
        return False


def download(release, channel, generation, *, open_url=_open, localappdata=None,
             cancelled=lambda: False, progress=lambda received, total: None, clock=time.monotonic):
    validate_release(release, channel)
    root = _cache_root(channel, localappdata)
    cleanup_expired(root)
    attempt = root / uuid.uuid4().hex
    attempt.mkdir(mode=0o700)
    partial = attempt / "installer.part"
    final = attempt / "installer.exe"
    deadline = clock() + 15 * 60
    try:
        expected = release.digest
        if expected is None:
            if not release.checksum_url:
                raise UpdateError("integrity", "Release has no installer checksum")
            with _response(open_url, release.checksum_url, tag=release.tag,
                           name=release.name.removesuffix(".exe") + ".sha256") as response:
                content = _read_bounded(response, 4096, cancelled, deadline, clock)
            if len(content) != release.checksum_size:
                raise UpdateError("integrity", "Checksum asset size differs from release metadata")
            if release.checksum_digest and hashlib.sha256(content).hexdigest() != release.checksum_digest:
                raise UpdateError("integrity", "Checksum asset digest mismatch")
            expected = parse_checksum(content, release.name)
        digest = hashlib.sha256()
        received = 0
        with _response(open_url, release.url, tag=release.tag, name=release.name) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) != release.size):
                raise UpdateError("size", "Installer Content-Length differs from release metadata")
            with partial.open("xb") as output:
                while True:
                    if cancelled():
                        raise UpdateError("cancelled", "Update cancelled")
                    if clock() > deadline:
                        raise UpdateError("timeout", "Installer download timed out")
                    try:
                        block = response.read(65536)
                    except (OSError, TimeoutError) as exc:
                        raise UpdateError("offline", "Installer download was interrupted") from exc
                    if not block:
                        break
                    received += len(block)
                    if received > release.size or received > MAX_INSTALLER:
                        raise UpdateError("size", "Installer exceeds advertised size")
                    output.write(block)
                    digest.update(block)
                    progress(received, release.size)
        if cancelled():
            raise UpdateError("cancelled", "Update cancelled")
        if received != release.size or digest.hexdigest() != expected:
            raise UpdateError("integrity", "Installer size or SHA-256 mismatch")
        partial.replace(final)
        return VerifiedFile(final, release, expected, generation, root)
    except BaseException:
        partial.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        attempt.rmdir()
        raise
