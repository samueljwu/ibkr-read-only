"""Exact-target binary database key custody in Windows Credential Manager.

Keys never enter local configuration, command lines or environment variables.
An unavailable store is an error; a missing key is never regenerated on open.
"""

import ctypes
import hmac
from uuid import UUID

from app.storage.secret_store import _api, _CREDENTIAL, _CRED_TYPE_GENERIC, _CRED_PERSIST_LOCAL_MACHINE
from app.storage.security_storage import credential_target, new_key


def load_key(channel, portfolio_id):
    target = credential_target(channel, portfolio_id)
    pointer = ctypes.POINTER(_CREDENTIAL)()
    api = _api()
    if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == 1168:  # ERROR_NOT_FOUND
            return None
        raise ctypes.WinError(error)
    try:
        credential = pointer.contents
        if credential.CredentialBlobSize != 32:
            raise ValueError("Stored database key has an unsupported length")
        return ctypes.string_at(credential.CredentialBlob, 32)
    finally:
        api.CredFree(pointer)


def save_key(channel, portfolio_id, key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("A random 32-byte database key is required")
    target = credential_target(channel, portfolio_id)
    if load_key(channel, portfolio_id) is not None:
        raise FileExistsError("Database key already exists")
    blob = (ctypes.c_ubyte * 32).from_buffer_copy(key)
    credential = _CREDENTIAL()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = 32
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = str(UUID(portfolio_id))
    api = _api()
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        stored = load_key(channel, portfolio_id)
        if stored is None or not hmac.compare_digest(key, stored):
            raise RuntimeError("Database key custody verification failed")
    except BaseException:
        delete_key(channel, portfolio_id)
        raise


def delete_key(channel, portfolio_id):
    """Delete only this exact target, for rollback of owned creation artifacts."""
    target = credential_target(channel, portfolio_id)
    api = _api()
    api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    api.CredDeleteW.restype = ctypes.c_int
    if not api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != 1168:
            raise ctypes.WinError(error)


def create_key(channel, portfolio_id):
    key = new_key()
    save_key(channel, portfolio_id, key)
    return key
