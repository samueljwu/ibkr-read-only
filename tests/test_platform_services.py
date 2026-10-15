"""OS credential storage."""

import ctypes
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.platform import secret_store
def test_mac_token_uses_explicit_keychain_backend(monkeypatch):
    saved = {}
    backend = ModuleType("keyring.backends.macOS")

    class FakeKeyring:
        def set_password(self, service, user, password):
            saved[(service, user)] = password

        def get_password(self, service, user):
            return saved.get((service, user))

    backend.Keyring = FakeKeyring
    monkeypatch.setitem(sys.modules, "keyring.backends.macOS", backend)
    monkeypatch.setattr(secret_store.sys, "platform", "darwin")
    assert secret_store.load_flex_token("TEST") is None
    secret_store.save_flex_token("TEST", "1234567890123456")
    assert secret_store.load_flex_token("TEST") == "1234567890123456"
    assert list(saved) == [("custom-ib-monitor:ibkr-flex:TEST", "TEST")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_token_round_trips_through_credential_manager(monkeypatch):
    store = {}
    live = []
    credential_pointer = ctypes.POINTER(secret_store._CREDENTIAL)

    def fake_write(pointer, flags):
        credential = pointer[0]
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        store[credential.TargetName] = blob.decode("utf-16-le")
        return 1

    def fake_read(target, credential_type, flags, pointer):
        if target not in store:
            ctypes.set_last_error(1168)  # ERROR_NOT_FOUND
            return 0
        data = store[target].encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        credential = secret_store._CREDENTIAL()
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        live.extend((buffer, credential))
        pointer[0] = ctypes.pointer(credential)
        return 1

    api = SimpleNamespace(
        CredWriteW=ctypes.CFUNCTYPE(ctypes.c_int, credential_pointer, ctypes.c_uint32)(fake_write),
        CredReadW=ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_wchar_p, ctypes.c_uint32,
                                   ctypes.c_uint32, ctypes.POINTER(credential_pointer))(fake_read),
        CredFree=ctypes.CFUNCTYPE(None, ctypes.c_void_p)(lambda pointer: None))
    monkeypatch.setattr(secret_store, "_api", lambda: api)
    monkeypatch.setattr(secret_store.sys, "platform", "win32")

    assert secret_store.load_flex_token("TEST") is None
    secret_store.save_flex_token("TEST", "1234567890123456")
    assert secret_store.load_flex_token("TEST") == "1234567890123456"
    assert store == {"custom-ib-monitor:ibkr-flex:TEST": "1234567890123456"}
