"""OS-released portfolio admission locks and nonsecret operation receipts.

Acquire the admission gate before a session lease. Maintenance holds the gate
and requires all other leases to be released. No SQLite transaction is used as
a process lock. Crashed processes release their OS locks automatically.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4


class StorageBusyError(RuntimeError):
    pass


def _lock(file):
    file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise StorageBusyError("Another application session is using this portfolio; close it and retry") from exc


def _unlock(file):
    file.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _gate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".storage-admission.lock").open("a+b") as gate:
        _lock(gate)
        try:
            yield
        finally:
            _unlock(gate)


class SessionLease:
    def __init__(self, path):
        self.directory = Path(path).resolve().parent
        self.file = None
        with _gate(self.directory):
            if (self.directory / ".storage-retired.json").exists():
                raise ValueError("This retained portfolio was relocated; open the selected destination instead")
            self.path = self.directory / (".storage-session-" + uuid4().hex + ".lock")
            self.file = self.path.open("x+b")
            try:
                _lock(self.file)
            except BaseException:
                self.file.close()
                self.file = None
                self.path.unlink(missing_ok=True)
                raise

    def close(self):
        if self.file is not None:
            _unlock(self.file)
            self.file.close()
            self.file = None
            self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def maintenance(path, *, owner=None):
    directory = Path(path).resolve().parent
    with _gate(directory):
        for lease_path in directory.glob(".storage-session-*.lock"):
            if owner is not None and owner.file is not None and lease_path == owner.path:
                continue
            try:
                with lease_path.open("r+b") as lease:
                    _lock(lease)
                    _unlock(lease)
            except FileNotFoundError:
                continue
            lease_path.unlink(missing_ok=True)
        yield


def record_operation(directory, operation_id, phase, **details):
    """Persist phases without keys, account identifiers or SQL statements."""
    allowed = {"kind", "source", "destination", "portfolio_uuid", "retained_plaintext"}
    if set(details) - allowed:
        raise ValueError("Unsupported operation receipt fields")
    directory = Path(directory)
    target = directory / (".storage-operation-" + str(uuid4() if operation_id is None else operation_id) + ".json")
    temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump({"version": 1, "phase": phase, **details}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def retire(directory, destination):
    """Refuse future admission to a retained relocated copy."""
    from app.storage.encrypted_backup import _publish
    target = Path(directory) / ".storage-retired.json"
    temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
    published = False
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump({"version": 1, "destination": str(destination)}, file)
            file.flush()
            os.fsync(file.fileno())
        # Admission must never observe a partially written retirement marker.
        # Retain exclusive publication so an existing retirement is preserved.
        _publish(temporary, target)
        published = True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            if not published:
                raise
            # A finalized retirement must not look like a failed operation
            # merely because a leftover owned temporary link could not be removed.
