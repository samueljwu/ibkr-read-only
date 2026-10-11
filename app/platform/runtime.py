"""Fixed build identity. Source is always Dev; frozen identity is build-time metadata."""

from functools import lru_cache
from contextlib import contextmanager
import json
import subprocess
import sys
from app.resources import app_directory, project_directory

BASE_VERSION = "1.0.2"


@lru_cache(maxsize=1)
def build_info():
    if getattr(sys, "frozen", False):
        info = json.loads((app_directory() / "_build_info.json").read_text(encoding="utf-8"))
        if info.get("channel") not in ("stable", "dev"):
            raise ValueError("Invalid packaged build channel")
        return info
    root = project_directory()
    revision = "source"
    dirty = True
    try:
        options = dict(cwd=root, text=True, capture_output=True, timeout=3,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        revision = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], check=True, **options).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                    check=True, **options).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return dict(channel="dev", version=f"{BASE_VERSION}-dev+{revision}{'.dirty' if dirty else ''}",
                commit=revision, dirty=dirty, release=False)


def channel():
    return build_info()["channel"]


def app_name():
    return "Portfolio Monitor" if channel() == "stable" else "Portfolio Monitor Dev"


def app_id():
    return f"CustomIBMonitor.PortfolioMonitor.{channel()}"


def version():
    return build_info()["version"]


def window_title(portfolio):
    return f"{app_name()} {version()} · {portfolio}"


@contextmanager
def channel_mutex():
    """Keep installer close checks aware of GUI and surviving workers alike."""
    handle = None
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateMutexW(None, False, app_id())
        if not handle:
            raise ctypes.WinError()
    try:
        yield
    finally:
        if handle:
            kernel.CloseHandle(handle)
