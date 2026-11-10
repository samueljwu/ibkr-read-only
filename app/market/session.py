"""Trading-session dates for saved daily market bars."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


US_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "CBOE", "SMART"}


def trading_timezone(position, contract_timezone=""):
    if contract_timezone:
        try:
            return ZoneInfo(contract_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    exchange = str(position.get("primary_exchange") or position.get("exchange") or "").upper()
    if position.get("currency") == "USD" and exchange in US_EXCHANGES:
        return ZoneInfo("America/New_York")
    return None


def completed_history_day(position, contract_timezone="", now=None):
    """Last date whose daily bar is certainly final for this instrument."""
    now = now or datetime.now(timezone.utc)
    zone = trading_timezone(position, contract_timezone)
    if zone is not None:
        return now.astimezone(zone).date() - timedelta(days=1)
    # Without an exchange clock, wait until the following UTC day has ended.
    return now.astimezone(timezone.utc).date() - timedelta(days=2)
