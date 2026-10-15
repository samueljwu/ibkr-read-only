"""Compact labels for option contracts shown in the desktop app."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re


_OPTION_SUFFIX = re.compile(r"^(.+?)\s*(?:\d{6}[CP]\d{8}|\d{6}\s+[CP]\d+(?:\.\d+)?)$", re.I)


def option_label(symbol, expiry, right, strike):
    """Return `QQQ 261120 C800`, or None when contract details are incomplete."""
    symbol = str(symbol or "").strip().upper()
    encoded = _OPTION_SUFFIX.fullmatch(symbol)
    if encoded:
        symbol = encoded.group(1).strip()
    expiry = str(expiry or "").strip().replace("-", "")
    right = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}.get(
        str(right or "").strip().upper())
    if not symbol or len(expiry) != 8 or not expiry.isascii() or not expiry.isdigit() or not right:
        return None
    try:
        date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
        price = Decimal(str(strike))
    except (ValueError, TypeError, InvalidOperation):
        return None
    if not price.is_finite() or price < 0:
        return None
    return f"{symbol} {expiry[2:]} {right}{format(price.normalize(), 'f')}"
