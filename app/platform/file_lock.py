"""Prevent concurrent Flex writers across app processes."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def single_writer_lock(database_path: Path, lock_name="ibkr-sync.lock"):
    lock_path = database_path.parent / lock_name
    handle = None
    try:
        handle = lock_path.open("a+b")
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if handle:
            handle.close()
        raise RuntimeError("Another IBKR Flex sync is running") from exc
    try:
        yield
    finally:
        try:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
