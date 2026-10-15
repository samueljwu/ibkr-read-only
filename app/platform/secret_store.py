"""Store the IBKR Flex token in the OS credential store."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys


_TARGET_PREFIX = "custom-ib-monitor:ibkr-flex:"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if sys.platform != "win32":
        raise OSError("Windows Credential Manager is required")
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def save_flex_token(account_id: str, token: str):
    if not account_id or not token.isdecimal() or len(token) < 16:
        raise ValueError("Invalid account or Flex token")
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        Keyring().set_password(_TARGET_PREFIX + account_id, account_id, token)
        return
    data = token.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    credential = _CREDENTIAL()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = _TARGET_PREFIX + account_id
    credential.CredentialBlobSize = len(data)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = account_id
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def load_flex_token(account_id: str) -> str | None:
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring().get_password(_TARGET_PREFIX + account_id, account_id)
    pointer = ctypes.POINTER(_CREDENTIAL)()
    api = _api()
    if not api.CredReadW(_TARGET_PREFIX + account_id, _CRED_TYPE_GENERIC, 0,
                         ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return None
        raise ctypes.WinError(error)
    try:
        credential = pointer.contents
        data = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return data.decode("utf-16-le")
    finally:
        api.CredFree(pointer)
