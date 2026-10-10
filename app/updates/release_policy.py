"""Version and asset policy shared by publication and the installed updater."""

from dataclasses import dataclass
import re

STABLE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
DEV = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-dev\.([1-9]\d*)\Z")
LOCAL_DEV = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-dev\+local\.[A-Za-z0-9.]+\Z")
LOCAL_STABLE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\+local\.[A-Za-z0-9.]+\Z")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    sequence: int = 0
    local: bool = False

    @property
    def order(self):
        return self.major, self.minor, self.patch, self.sequence


def release_version(tag, channel, *, draft=False, prerelease=False):
    if type(draft) is not bool or type(prerelease) is not bool or draft:
        return None
    if not isinstance(tag, str) or len(tag) > 80:
        return None
    match = (STABLE if channel == "stable" and not prerelease else
             DEV if channel == "dev" and prerelease else None)
    found = match.fullmatch(tag) if match and isinstance(tag, str) else None
    if not found:
        return None
    numbers = tuple(map(int, found.groups()))
    return Version(*numbers) if all(n <= 65535 for n in numbers) else None


def installed_version(label, channel):
    if not isinstance(label, str) or len(label) > 120:
        return None
    if channel == "stable":
        found = STABLE.fullmatch("v" + label) or LOCAL_STABLE.fullmatch(label)
        if found:
            numbers = tuple(map(int, found.groups()[:3]))
            return Version(*numbers, local=bool(LOCAL_STABLE.fullmatch(label))) if all(n <= 65535 for n in numbers) else None
        return None
    if channel == "dev":
        found = DEV.fullmatch("v" + label)
        if found:
            numbers = tuple(map(int, found.groups()))
            return Version(*numbers) if all(n <= 65535 for n in numbers) else None
        found = LOCAL_DEV.fullmatch(label)
        if found:
            numbers = tuple(map(int, found.groups()[:3]))
            return Version(*numbers, local=True) if all(n <= 65535 for n in numbers) else None
        return None
    return None


def is_newer(candidate, installed):
    return candidate.order > installed.order


def installer_name(tag, channel):
    version = release_version(tag, channel, prerelease=channel == "dev")
    if version is None:
        raise ValueError("Invalid release tag")
    label = tag[1:]
    prefix = "PortfolioMonitor" if channel == "stable" else "PortfolioMonitorDev"
    return f"{prefix}-{label}-windows-x64-setup.exe"


def publication(base, channel, commit, dirty, git, *, release=False, dev_release=None):
    """Return (label, tag, kind, asset, numeric) without invoking a compiler."""
    base_match = STABLE.fullmatch("v" + base)
    if not base_match or any(int(n) > 65535 for n in base_match.groups()):
        raise ValueError("BASE_VERSION must contain three Windows version components")
    if release and dev_release is not None:
        raise ValueError("Release modes are mutually exclusive")
    if release and channel != "stable" or dev_release is not None and channel != "dev":
        raise ValueError("Publication channel does not match release mode")
    if dev_release is not None and (type(dev_release) is not int or not 1 <= dev_release <= 65535):
        raise ValueError("Dev sequence must be 1–65535")
    tag = f"v{base}" if release else f"v{base}-dev.{dev_release}" if dev_release else None
    if tag:
        if dirty or git("rev-parse", f"{tag}^{{commit}}") != commit:
            raise ValueError("Publication requires a clean checkout at the exact tag")
        label, kind = tag[1:], "stable" if release else "dev"
    else:
        label, kind = None, "local"
    numeric = tuple(map(int, base_match.groups())) + (dev_release or 0,)
    asset = installer_name(tag, channel) if tag else None
    return label, tag, kind, asset, numeric
