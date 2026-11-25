"""OS credential storage."""

import ctypes
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.platform import secret_store
from app.platform.secret_store import load_database_key as credential_load_database_key


def test_mac_credentials_reuse_successful_keychain_reads(monkeypatch, tmp_path):
    saved = {}
    reads = []
    backend = ModuleType("keyring.backends.macOS")

    class FakeKeyring:
        def set_password(self, service, user, password):
            saved[(service, user)] = password

        def get_password(self, service, user):
            reads.append((service, user))
            return saved.get((service, user))

    backend.Keyring = FakeKeyring
    monkeypatch.setitem(sys.modules, "keyring.backends.macOS", backend)
    monkeypatch.setattr(secret_store.sys, "platform", "darwin")
    monkeypatch.setattr(secret_store, "_macos_credentials", {})
    token_target = ("ibkr-dashboard:ibkr-flex:TEST", "TEST")
    old_target = ("custom-ib-monitor:ibkr-flex:TEST", "TEST")
    assert secret_store.load_flex_token("TEST") is None
    assert reads == [token_target, old_target]
    saved[token_target] = "1234567890123456"
    assert secret_store.load_flex_token("TEST") == "1234567890123456"
    assert secret_store.load_flex_token("TEST") == "1234567890123456"
    assert reads == [token_target, old_target, token_target]

    path = tmp_path / "portfolio.sqlite3"
    target = secret_store._database_target(path)
    saved[target, target] = "a" * 64
    assert credential_load_database_key(path) == "a" * 64
    assert credential_load_database_key(path) == "a" * 64
    assert reads[-1] == (target, target)
    assert reads.count((target, target)) == 1

    secret_store.save_flex_token("TEST", "2345678901234567")
    assert secret_store.load_flex_token("TEST") == "2345678901234567"
    assert saved[token_target] == "2345678901234567"
    assert len(reads) == 4

    # A new process can still fall back to an item stored by an older version.
    secret_store._macos_credentials.clear()
    saved[old_target] = saved.pop(token_target)
    assert secret_store.load_flex_token("TEST") == "2345678901234567"
    assert reads[-2:] == [token_target, old_target]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_token_round_trips_through_credential_manager(monkeypatch, tmp_path):
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
    assert store == {"ibkr-dashboard:ibkr-flex:TEST": "1234567890123456"}
    path = tmp_path / "portfolio.sqlite3"
    target = secret_store._database_target(path)
    secret_store._save(target, "a" * 64, target)
    assert secret_store._load(target, target) == "a" * 64
    assert len(store) == 2
    assert str(path) not in next(key for key in store if key.startswith("ibkr-dashboard:database:"))
    store.pop(target)
    old_target = secret_store._database_target(path, "custom-ib-monitor:database:")
    store[old_target] = "a" * 64
    assert credential_load_database_key(path) == "a" * 64
