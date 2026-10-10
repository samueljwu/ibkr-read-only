"""Channel admission and handoff gates for Windows GUI and frozen workers."""

from contextlib import contextmanager, ExitStack
import sys
import time

from app.platform.runtime import app_id
from app.platform.runtime import channel_mutex


class AdmissionError(RuntimeError):
    pass


def _kernel():
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.WaitForSingleObject.restype = ctypes.c_uint
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.restype = ctypes.c_bool
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_bool
    return kernel


def _present(name):
    if sys.platform != "win32":
        return False
    kernel = _kernel()
    handle = kernel.OpenMutexW(0x00100000, False, name)
    if handle:
        kernel.CloseHandle(handle)
        return True
    if kernel.GetLastError() != 2:
        raise AdmissionError("Cannot inspect updater admission guard")
    return False


def setup_running():
    # Inno checks AppMutex before creating its built-in SetupMutex. The
    # InitializeSetup signal covers that earlier phase and stays for its life.
    return _present(app_id() + ".SetupStarting") or _present(app_id() + ".Setup")


def handoff_running():
    return _present(app_id() + ".Handoff")


@contextmanager
def admission():
    if sys.platform != "win32":
        yield
        return
    kernel = _kernel()
    handle = kernel.CreateMutexW(None, False, app_id() + ".Admission")
    if not handle:
        raise AdmissionError("Cannot create updater admission guard")
    owned = False
    try:
        if kernel.WaitForSingleObject(handle, 0) not in (0, 0x80):
            raise AdmissionError("Updater admission guard is busy")
        owned = True
        if handoff_running() or setup_running():
            raise AdmissionError("Update handoff or Setup is running; start the app afterward")
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def wait_for_postinstall_setup(*, clock=time.monotonic, sleep=time.sleep, timeout=120):
    """The [Run] reopen waits for both Setup guards, then normal admission."""
    if sys.platform != "win32":
        raise AdmissionError("Postinstall reopen is available only on Windows")
    deadline = clock() + timeout
    while setup_running():
        if clock() >= deadline:
            raise AdmissionError("Setup is still running; open the app after it finishes")
        sleep(.1)


class Handoff:
    def __init__(self):
        self.handle = None
        self.admission_handle = None
        self.admission_owned = False

    def __enter__(self):
        if sys.platform == "win32":
            try:
                kernel = _kernel()
                self.admission_handle = kernel.CreateMutexW(None, False, app_id() + ".Admission")
                if not self.admission_handle or kernel.WaitForSingleObject(self.admission_handle, 0) not in (0, 0x80):
                    raise AdmissionError("Cannot acquire updater admission guard")
                self.admission_owned = True
                if setup_running() or handoff_running():
                    raise AdmissionError("Another update is already running")
                self.handle = kernel.CreateMutexW(None, False, app_id() + ".Handoff")
                if not self.handle:
                    raise AdmissionError("Cannot create updater handoff guard")
            except BaseException:
                self.close()
                raise
        return self

    def close(self):
        if sys.platform == "win32":
            kernel = _kernel()
            if self.handle:
                kernel.CloseHandle(self.handle)
                self.handle = None
            if self.admission_handle:
                if self.admission_owned:
                    kernel.ReleaseMutex(self.admission_handle)
                kernel.CloseHandle(self.admission_handle)
                self.admission_handle = None
                self.admission_owned = False

    def __exit__(self, *args):
        self.close()


def source_worker_entry(run):
    """Direct ``python -m scripts.*`` workers share installed worker admission."""
    with ExitStack() as registered:
        with admission():
            registered.enter_context(channel_mutex())
        return run()
