"""Store the IBKR Flex token in the OS credential store."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from hashlib import sha256
from pathlib import Path
import re
import sys


_TARGET_PREFIX = "ibkr-dashboard:ibkr-flex:"
_DATABASE_PREFIX = "ibkr-dashboard:database:"
_OLD_TARGET_PREFIX = "custom-ib-monitor:ibkr-flex:"
_OLD_DATABASE_PREFIX = "custom-ib-monitor:database:"
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


def _save(target: str, value: str, user: str):
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        Keyring().set_password(target, user, value)
        return
    data = value.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    credential = _CREDENTIAL()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(data)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = user
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def _load(target: str, user: str) -> str | None:
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring().get_password(target, user)
    pointer = ctypes.POINTER(_CREDENTIAL)()
    api = _api()
    if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0,
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


def save_flex_token(account_id: str, token: str):
    if not account_id or not token.isdecimal() or len(token) < 16:
        raise ValueError("Invalid account or Flex token")
    _save(_TARGET_PREFIX + account_id, token, account_id)


def load_flex_token(account_id: str) -> str | None:
    return (_load(_TARGET_PREFIX + account_id, account_id)
            or _load(_OLD_TARGET_PREFIX + account_id, account_id))


def _database_target(path, prefix=_DATABASE_PREFIX) -> str:
    # A path is only a local lookup name. The encryption key is never derived from it.
    location = str(Path(path).expanduser().resolve()).casefold()
    return prefix + sha256(location.encode("utf-8")).hexdigest()


def save_database_key(path, key: str):
    if re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
        raise ValueError("Database recovery code must be 64 hexadecimal characters")
    target = _database_target(path)
    _save(target, key.lower(), target)


def load_database_key(path) -> str | None:
    target = _database_target(path)
    return _load(target, target) or _load(
        _database_target(path, _OLD_DATABASE_PREFIX),
        _database_target(path, _OLD_DATABASE_PREFIX))
