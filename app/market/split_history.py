"""Fetch public stock-split dates for chart-only execution comparisons."""

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

from PySide6.QtCore import QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


_SYMBOL = re.compile(r"[A-Za-z0-9.\-^=]{1,32}\Z")
_CLASS_SYMBOL = re.compile(r"([A-Za-z]{1,12})[. /-]([A-Za-z])\Z")


def yahoo_symbol_candidates(position):
    """Translate IBKR labels to the exchange-qualified Yahoo symbols."""
    symbol = str(position.get("ib_symbol") or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        return []
    currency = str(position.get("currency") or "").strip().upper()
    venue = str(position.get("primary_exchange") or position.get("exchange") or "").strip().upper()
    candidates = []

    def add(candidate):
        if _SYMBOL.fullmatch(candidate) and candidate not in candidates:
            candidates.append(candidate)

    for label in (position.get("local_symbol"), position.get("display_ticker")):
        label = str(label or "").strip().upper()
        share_class = _CLASS_SYMBOL.fullmatch(label)
        if share_class and share_class[1] == symbol:
            add(f"{symbol}-{share_class[2]}")
        elif label.startswith(symbol + ".") and _SYMBOL.fullmatch(label):
            add(label)
    if symbol.isdigit() and len(symbol) == 4 and currency == "JPY" and venue in ("", "SMART", "TSEJ", "TSE"):
        add(f"{symbol}.T")
    if "." not in symbol and currency == "AUD" and venue in ("", "SMART", "ASX", "ASXALL", "ASXT"):
        add(f"{symbol}.AX")
    if not candidates:
        add(symbol)
    return candidates


def _split_factor(numerator, denominator):
    try:
        top, bottom = Decimal(str(numerator)), Decimal(str(denominator))
        if not top.is_finite() or not bottom.is_finite() or top <= 0 or bottom <= 0:
            raise ValueError("Invalid split ratio")
        return top / bottom
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError("Invalid split ratio") from exc


def parse_split_history(payload, symbol, currency):
    """Validate a Yahoo chart response and return effective dates and factors."""
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        if (meta["symbol"].upper() != symbol.upper() or
                meta["currency"].upper() != currency.upper() or
                meta.get("instrumentType", "EQUITY") not in ("EQUITY", "ETF")):
            raise ValueError("Split-history instrument does not match this stock")
        events = result.get("events", {}).get("splits", {})
        splits = {}
        for event in events.values():
            day = datetime.fromtimestamp(int(event["date"]), timezone.utc).date().isoformat()
            ratio = _split_factor(event["numerator"], event["denominator"])
            if ratio == 1 or day in splits:
                raise ValueError("Invalid or duplicate split event")
            splits[day] = ratio
        return splits
    except (AttributeError, IndexError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid split-history response") from exc


def combine_split_history(recorded, public):
    """Prefer market effective dates; include ledger events absent from the feed."""
    combined = dict(public)
    for day, ratio in recorded.items():
        nearby = [(remote_day, remote_ratio) for remote_day, remote_ratio in public.items()
                  if abs((date.fromisoformat(remote_day) - date.fromisoformat(day)).days) <= 4]
        if nearby:
            if any(remote_ratio != ratio for _, remote_ratio in nearby):
                raise ValueError("Recorded and public split ratios disagree")
            continue
        combined[day] = ratio
    return combined


class SplitHistoryClient(QObject):
    """Asynchronous lookup with a same-day verified cache in portfolio settings."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.network = QNetworkAccessManager(self)
        self._pending = {}
        self._failed_at = {}

    @staticmethod
    def _key(position):
        return f"analytics_levels_split_history_{position['id']}"

    def events(self, position, first_trade_day, changed):
        """Return (events, state); None means verified split history is pending."""
        symbol = position.get("ib_symbol") or ""
        currency = position.get("currency") or ""
        con_id = position.get("con_id")
        candidates = yahoo_symbol_candidates(position)
        if not candidates or not currency or not con_id:
            return None, "unavailable"
        key = self._key(position)
        try:
            cached = json.loads(self.db.setting(key))
            if (cached["date"] == date.today().isoformat() and
                    cached["symbol"] == symbol and cached["currency"] == currency and
                    cached["con_id"] == int(con_id) and
                    (cached.get("lookup_symbol") in candidates or
                     "lookup_symbol" not in cached and candidates == [symbol])):
                events = {day: _split_factor(numerator, denominator)
                          for day, numerator, denominator in cached["events"]}
                return events, "ready"
        except (ValueError, TypeError, KeyError):
            pass
        if time.monotonic() - self._failed_at.get(key, float("-inf")) < 300:
            return None, "unavailable"
        if key not in self._pending:
            try:
                first = date.fromisoformat(first_trade_day)
            except (TypeError, ValueError):
                return None, "unavailable"
            period1 = int(datetime.combine(first - timedelta(days=7), datetime.min.time(),
                                           tzinfo=timezone.utc).timestamp())
            period2 = int(datetime.combine(date.today() + timedelta(days=1), datetime.min.time(),
                                           tzinfo=timezone.utc).timestamp())
            query = urlencode({"interval": "1mo", "period1": period1,
                               "period2": period2, "events": "splits"})
            self._request_candidate(position.copy(), candidates, query, changed)
        return None, "fetching"

    def _request_candidate(self, position, candidates, query, changed):
        symbol = candidates[0]
        url = QUrl(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?{query}")
        request = QNetworkRequest(url)
        request.setRawHeader(b"User-Agent", b"CustomIBMonitor/1.0")
        request.setTransferTimeout(10000)
        reply = self.network.get(request)
        self._pending[self._key(position)] = reply
        reply.finished.connect(
            lambda p=position, r=reply, c=candidates: self._finished(p, r, c, query, changed))

    def _finished(self, position, reply, candidates, query, changed):
        key = self._key(position)
        self._pending.pop(key, None)
        retrying = False
        try:
            if reply.error() != QNetworkReply.NoError:
                raise ValueError("Split-history request failed")
            payload = json.loads(bytes(reply.readAll()))
            events = parse_split_history(payload, candidates[0], position["currency"])
            self.db.set_setting(key, json.dumps({
                "date": date.today().isoformat(), "symbol": position["ib_symbol"],
                "lookup_symbol": candidates[0],
                "currency": position["currency"], "con_id": int(position["con_id"]),
                "events": [[day, str(ratio), "1"]
                           for day, ratio in sorted(events.items())],
            }, separators=(",", ":")))
            self._failed_at.pop(key, None)
        except (ValueError, TypeError, KeyError, OSError):
            if len(candidates) > 1:
                try:
                    self._request_candidate(position, candidates[1:], query, changed)
                    retrying = True
                except (ValueError, TypeError, OSError):
                    pass
            if not retrying:
                self._failed_at[key] = time.monotonic()
        finally:
            reply.deleteLater()
            if not retrying:
                changed()
