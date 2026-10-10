"""Printable encoding of an independent random SQLCipher recovery key.

The checksum detects transcription errors; SQLCipher authenticates the backup.
This encoding is not a password derivation or an encryption format.
"""

import base64
import hashlib
import hmac
import re


def encode_recovery_key(key: bytes) -> str:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("A random 32-byte recovery key is required")
    encoded = base64.b32encode(key).decode("ascii").rstrip("=")
    checksum = base64.b32encode(hashlib.sha256(b"PMR1\0" + key).digest()).decode("ascii")[:4]
    symbols = encoded + checksum
    return "PMR1-" + "-".join(symbols[index:index + 8] for index in range(0, len(symbols), 8))


def decode_recovery_key(code: str) -> bytes:
    if not isinstance(code, str):
        raise ValueError("Enter a valid portable recovery code")
    normalized = code.strip().upper()
    if not re.fullmatch(r"PMR1-(?:[A-Z2-7]{8}-){6}[A-Z2-7]{8}", normalized):
        raise ValueError("Enter the complete PMR1 recovery code, including its hyphens")
    symbols = normalized[5:].replace("-", "")
    try:
        key = base64.b32decode(symbols[:52] + "====", casefold=False)
    except Exception:
        raise ValueError("Enter a valid portable recovery code") from None
    # Re-encoding rejects noncanonical padding bits as well as wrong checksums.
    if len(key) != 32 or not hmac.compare_digest(encode_recovery_key(key), normalized):
        raise ValueError("The recovery code contains a transcription error")
    return key
