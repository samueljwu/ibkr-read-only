"""Retrieve IBKR Activity Flex reports for automatic trade sync."""

from datetime import date, datetime
from ssl import SSLCertVerificationError, create_default_context
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.error import HTTPError, URLError
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import certifi

from app.platform.secret_store import load_flex_token


MAX_FLEX_WINDOW_DAYS = 365
HISTORY_YEARS = 4  # Current year plus four previous calendar years.


def flex_today():
    """IBKR reports a trading day complete only once it ends in US Eastern time."""
    return datetime.now(ZoneInfo("America/New_York")).date()


def oldest_flex_date():
    """First day in IBKR's current-year-plus-history Flex window."""
    return date(flex_today().year - HISTORY_YEARS, 1, 1)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Flex authenticates with a token in the URL. Never forward it to a
        # redirect target, even when the target looks like another IBKR host.
        return None


def _build_opener():
    context = create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return build_opener(_RejectRedirects, HTTPSHandler(context=context))


_opener = _build_opener()


class FlexServiceError(ValueError):
    """A coded IBKR Flex response, without the token or account in the error."""

    def __init__(self, code, message, stage):
        self.code = int(code) if str(code).isdigit() else None
        self.stage = stage
        self.message = message
        super().__init__(f"Flex error {self.code}: {message}" if self.code else message)


class FlexValidationError(ValueError):
    """A local validation message containing no report values or server text."""


class FlexEnvironmentError(ValueError):
    """A fixed, credential-free description of a local import failure."""


def resolve_flex_token(account_id, token=None):
    if token:
        return token
    try:
        secret = load_flex_token(account_id)
    except Exception:
        raise FlexEnvironmentError(
            "The import process could not read the Flex token from the OS credential store. "
            "Check Keychain access on macOS or Credential Manager on Windows.") from None
    if not secret:
        raise FlexValidationError("IBKR Flex token is missing; save it in Settings first")
    return secret


def flex_day(value):
    """Normalize the compact or ISO date prefix used by Flex XML fields."""
    value = str(value or "").strip()
    try:
        if len(value) >= 8 and value[:8].isdigit():
            return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        raise FlexValidationError("Invalid Flex report date; include valid report dates in the query") from None


def require_usd_base(statement, account_id, *, conversion_evidence=False):
    """Account Information Currency is IBKR's account base currency field."""
    bases = set()
    if statement.get("baseCurrency"):
        bases.add(statement.get("baseCurrency").upper())
    for node in statement.iter():
        if node.tag.rsplit("}", 1)[-1] != "AccountInformation":
            continue
        if node.get("accountId", account_id) != account_id:
            raise FlexValidationError("Flex Account Information account mismatch")
        for field in ("currency", "baseCurrency"):
            if node.get(field):
                bases.add(node.get(field).upper())
    if bases and bases != {"USD"}:
        raise FlexValidationError("Flex import requires a USD base account; check Account Information Currency")
    if not bases:
        targets = {node.get("toCurrency", "").upper() for node in statement.iter()
                   if node.tag.rsplit("}", 1)[-1] == "ConversionRate"}
        if not conversion_evidence or targets != {"USD"}:
            raise FlexValidationError("Flex report must identify USD as the base currency; include Account Information Currency")


def safe_flex_error(exc):
    """Return only local validation text or allowlisted service context."""
    if isinstance(exc, (FlexValidationError, FlexEnvironmentError)):
        return str(exc)
    if isinstance(exc, FlexServiceError):
        code = exc.code if type(exc.code) is int and 1000 <= exc.code <= 1999 else None
        stage = exc.stage if exc.stage in ("SendRequest", "GetStatement") else "Flex service"
        message = f"IBKR Flex request failed{f' with code {code}' if code else ''} ({stage})."
        if code == 1025:
            message += " IBKR has restricted the access token; wait for IBKR access to recover before retrying."
        return message
    return "Flex import failed; check the query sections, account, dates, token configuration and connection."


TEMPORARY_FLEX_CODES = frozenset({1001, 1004, 1005, 1006, 1007, 1008,
                                  1009, 1018, 1019, 1021})


class FlexRequestPacer:
    """Keep sends spaced across windows and bound retries for temporary errors."""

    def __init__(self, seconds=15):
        self.seconds = seconds
        self.last_request = 0.0

    def fetch(self, fetcher, query_id, token, first, last):
        for attempt in range(3):
            delay = self.seconds - (time.monotonic() - self.last_request)
            if self.last_request and delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            try:
                return fetcher(query_id, token, from_day=first, to_day=last)
            except FlexServiceError as exc:
                if exc.code not in TEMPORARY_FLEX_CODES or attempt == 2:
                    raise


def _transport_failure(exc, stage):
    if isinstance(exc, HTTPError):
        return FlexEnvironmentError(f"IBKR Flex {stage} returned HTTP {exc.code}.")
    if (isinstance(exc, SSLCertVerificationError) or
            isinstance(exc, URLError) and isinstance(exc.reason, SSLCertVerificationError)):
        return FlexEnvironmentError(
            "IBKR Flex TLS certificate verification failed. Check this Python installation's certificates.")
    return FlexEnvironmentError(
        f"Could not connect to IBKR Flex during {stage}. Check the network and proxy settings.")


def fetch_flex(query_id, token, timeout=20, from_day=None, to_day=None):
    """Fetch a saved Flex query; the token is supplied at call time only."""
    if not query_id or not token:
        raise ValueError("Flex query ID and token are required")
    base = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/"
    headers = {"User-Agent": "Python/PortfolioMonitor"}
    params = {"t": token, "q": query_id, "v": 3}
    if (from_day is None) != (to_day is None):
        raise ValueError("Both Flex date bounds are required")
    if from_day is not None:
        first, last = date.fromisoformat(str(from_day)), date.fromisoformat(str(to_day))
        if first > last:
            raise ValueError("Flex start date is after end date")
        params.update({"fd": first.strftime("%Y%m%d"), "td": last.strftime("%Y%m%d")})
    url = base + "SendRequest?" + urlencode(params)
    try:
        with _opener.open(Request(url, headers=headers), timeout=timeout) as response:
            status = ET.fromstring(response.read())
    except (HTTPError, URLError, SSLCertVerificationError, TimeoutError) as exc:
        raise _transport_failure(exc, "request") from None
    if status.findtext("Status") != "Success":
        code = status.findtext("ErrorCode")
        message = status.findtext("ErrorMessage") or "Flex request failed"
        raise FlexServiceError(code, message, "SendRequest")
    reference = status.findtext("ReferenceCode")
    if not reference:
        raise ValueError("Flex did not return a reference code")
    url = base + "GetStatement?" + urlencode({"t": token, "q": reference, "v": 3})
    # IBKR limits each token to one request per second and ten per minute.
    time.sleep(2)
    for _ in range(8):
        try:
            with _opener.open(Request(url, headers=headers), timeout=timeout) as response:
                data = response.read(30_000_001)
        except (HTTPError, URLError, SSLCertVerificationError, TimeoutError) as exc:
            raise _transport_failure(exc, "statement download") from None
        if len(data) > 30_000_000:
            raise ValueError("Flex report is too large")
        error = ET.fromstring(data)
        code = error.findtext("ErrorCode")
        if not code and any(node.tag.rsplit("}", 1)[-1] == "FlexStatements"
                            for node in error.iter()):
            return data
        message = error.findtext("ErrorMessage") or "Flex statement unavailable"
        if code != "1019":
            raise FlexServiceError(code, message, "GetStatement")
        time.sleep(8)
    raise TimeoutError("Flex statement is still being generated")
