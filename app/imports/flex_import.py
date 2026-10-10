"""Retrieve IBKR Activity Flex reports for automatic trade sync."""

from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
import time
from pathlib import Path
from hashlib import sha256
from tempfile import NamedTemporaryFile
from collections import deque
from app.platform.diagnostics import configure_diagnostics
from app.storage.local_files import replace_with_retry
import xml.etree.ElementTree as ET


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Flex authenticates with a token in the URL. Never forward it to a
        # redirect target, even when the target looks like another IBKR host.
        return None


_opener = build_opener(_RejectRedirects)


class FlexServiceError(ValueError):
    """A coded IBKR Flex response, without the token or account in the error."""

    def __init__(self, code, message, stage):
        self.code = int(code) if str(code).isdigit() else None
        self.stage = stage
        self.message = message
        super().__init__(f"Flex error {self.code}: {message}" if self.code else message)


class FlexReportTooLarge(ValueError):
    """A download exceeded the local report size limit."""


class FlexReportGenerationTimeout(TimeoutError):
    """A submitted report stayed pending throughout bounded polling."""


TEMPORARY_FLEX_CODES = frozenset({1001, 1004, 1005, 1006, 1007, 1008,
                                  1009, 1018, 1019, 1021})
CONFIGURATION_FLEX_CODES = frozenset({1010, 1011, 1012, 1013, 1014, 1015,
                                     1016, 1020, 1025})
# Both SendRequest and GetStatement count toward IBKR's ten requests/minute.
DEFAULT_REQUEST_PACING_SECONDS = 8
MAX_FLEX_WINDOW_DAYS = 365
MAX_FLEX_REPORT_BYTES = 30_000_000


def _smaller_report_windows(first, last, days, protected_windows=()):
    """Partition a request without gaps or cuts through saved cash periods."""
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    protected = []
    for a, b in sorted(protected_windows):
        a, b = date.fromisoformat(a), date.fromisoformat(b)
        if b < start or a > end:
            continue
        if a < start or b > end:
            raise ValueError('Cash recovery must include complete existing overlapping periods')
        if protected and a <= protected[-1][1]:
            protected[-1] = (protected[-1][0], max(b, protected[-1][1]))
        else:
            protected.append((a, b))
    windows = []
    while start <= end:
        stop = min(start + timedelta(days=days - 1), end)
        if stop < end and stop.weekday() >= 5:
            weekday_stop = stop - timedelta(days=stop.weekday() - 4)
            if weekday_stop >= start:
                stop = weekday_stop
        for a, b in protected:
            if a <= stop < b:
                if a > start:
                    stop = a - timedelta(days=1)
                    if stop.weekday() >= 5:
                        weekday_stop = stop - timedelta(days=stop.weekday() - 4)
                        if weekday_stop >= start and not any(c <= weekday_stop < d for c, d in protected):
                            stop = weekday_stop
                else:
                    stop = b
                break
        if (stop - start).days + 1 > MAX_FLEX_WINDOW_DAYS:
            raise ValueError('Cash recovery cannot preserve saved periods within the 365-day limit')
        windows.append((start.isoformat(), stop.isoformat(), 0))
        start = stop + timedelta(days=1)
    return windows


def weekends_only(first, last):
    """Whether a Flex report's omitted dates contain no weekdays."""
    day = date.fromisoformat(first)
    through = date.fromisoformat(last)
    while day <= through:
        if day.weekday() < 5:
            return False
        day += timedelta(days=1)
    return True


class FlexRequestPacer:
    """Keep sends spaced across windows and bound retries for temporary errors."""

    def __init__(self, seconds=DEFAULT_REQUEST_PACING_SECONDS):
        self.seconds = seconds
        self.last_request = 0.0
        self.db = None
        self.account = None
        self.reuse_cached = False
        self.last_cache = None
        self.consecutive_unavailable = 0
        self.unavailable_attempts = 0
        self.unverified = []
        self.recovery_stopped = False
        self.should_pause = None
        self.user_paused = False
        self.large_window_recovery = False
        self.large_window_days = MAX_FLEX_WINDOW_DAYS
        self.protected_windows = []

    def configure(self, db, account):
        self.db, self.account = db, account
        self.log = configure_diagnostics(db.path, 'flex-requests')

    def wait(self):
        """Pace sends AND statement polls, including sequential worker processes."""
        stamp = self.db.path.parent / 'flex-request-time.txt' if self.db else None
        previous = self.last_request
        if stamp:
            try:
                previous = max(previous, float(stamp.read_text()))
            except (OSError, ValueError):
                pass
        delay = self.seconds - (time.time() - previous)
        if previous and delay > 0:
            time.sleep(delay)
        self.last_request = time.time()
        if stamp:
            stamp.write_text(str(self.last_request))

    def fetch(self, fetcher, query_id, token, first, last):
        cache = None
        if self.db:
            key = sha256(f'{self.account}:{query_id}'.encode()).hexdigest()[:24]
            folder = self.db.path.parent / 'flex-reports'
            folder.mkdir(exist_ok=True)
            cache = folder / f'{key}-{first}-{last}.xml'
            self.last_cache = cache
            if self.reuse_cached and cache.exists():
                self.log.info('Using saved report %s to %s (%s)', first, last, key)
                return cache.read_bytes()
        for attempt in range(3):
            try:
                if self.db:
                    self.log.info('Requesting report %s to %s (%s), attempt %s', first, last, key, attempt + 1)
                if fetcher is fetch_flex:
                    xml = fetcher(query_id, token, from_day=first, to_day=last,
                                  request_pacer=self)
                else:
                    self.wait()
                    xml = fetcher(query_id, token, from_day=first, to_day=last)
                if cache:
                    # Keep the raw response before validation, for offline diagnosis.
                    with NamedTemporaryFile('wb', dir=cache.parent, prefix='flex-report-',
                                            suffix='.pending.xml', delete=False) as output:
                        temporary = Path(output.name)
                        output.write(xml)
                    # On a persistent write error leave the raw response intact.
                    replace_with_retry(temporary, cache)
                return xml
            except FlexServiceError as exc:
                if self.db:
                    self.log.warning('Report %s to %s failed: Flex %s during %s', first, last, exc.code, exc.stage)
                if self.db and exc.code in CONFIGURATION_FLEX_CODES:
                    self.db.set_metadata('ibkr_flex_paused', str(exc.code))
                if exc.code not in TEMPORARY_FLEX_CODES or attempt == 2:
                    raise
                time.sleep(min(30, 5 * (2 ** attempt)) if self.seconds else 0)

    def invalidate(self):
        """Retain rejected XML for diagnosis but fetch again after a query repair."""
        if self.last_cache and self.last_cache.exists():
            replace_with_retry(self.last_cache, self.last_cache.with_suffix('.invalid.xml'))

    def reports(self, fetcher, query_id, token, windows, on_start=None, on_unavailable=None):
        """One bounded recovery policy for daily sync and manual history imports."""
        pending = deque((first, last, 0) for first, last in windows)
        self.unverified = []
        while pending:
            # The caller commits a yielded report before requesting the next
            # one. Stop here rather than interrupting its fetch or transaction.
            if self.should_pause and self.should_pause():
                self.user_paused = True
                self.unverified = [(start, end) for start, end, _ in pending]
                break
            first, last, depth = pending.popleft()
            length = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
            if on_start:
                on_start(first, last)
            try:
                if self.large_window_recovery and length > self.large_window_days:
                    # Reuse the smaller size for the rest of this attempt.
                    children = _smaller_report_windows(first, last, self.large_window_days, self.protected_windows)
                    if len(children) > 1:
                        pending.extendleft(children)
                        continue
                xml = self.fetch(fetcher, query_id, token, first, last)
            except (FlexServiceError, FlexReportTooLarge, FlexReportGenerationTimeout) as exc:
                self.unverified = [(a, b) for a, b, _ in pending]
                unavailable = isinstance(exc, FlexServiceError) and exc.code == 1003
                if isinstance(exc, FlexServiceError) and not unavailable:
                    self.unverified = [(start, end) for start, end, _ in pending]
                    raise
                if unavailable:
                    self.consecutive_unavailable += 1
                    self.unavailable_attempts += 1
                start, end = date.fromisoformat(first), date.fromisoformat(last)
                if (self.large_window_recovery and length > 30
                        and self.consecutive_unavailable < 3 and self.unavailable_attempts < 6):
                    # Reach a monthly probe before the existing failed-request
                    # budget stops the source: year -> quarter -> month.
                    self.large_window_days = min(self.large_window_days, 90 if length > 90 else 28)
                    children = _smaller_report_windows(first, last, self.large_window_days, self.protected_windows)
                    if len(children) == 1:
                        raise ValueError('Cash recovery cannot split an existing saved period safely') from exc
                    pending.extendleft(children)  # Request recent children first.
                    if self.db:
                        self.log.info('Splitting large report %s to %s after %s into %s windows',
                                      first, last, type(exc).__name__, len(children))
                    self.unverified = []
                    continue
                if not unavailable:
                    # Oversized or pending reports do not prove missing history.
                    self.unverified = [(a, b) for a, b, _ in pending]
                    raise
                if self.consecutive_unavailable < 3 and self.unavailable_attempts < 6 and depth < 2 and start < end:
                    if self.protected_windows:
                        children = _smaller_report_windows(first, last, (length + 1) // 2,
                                                          self.protected_windows)
                        if len(children) == 1:
                            raise ValueError('Cash recovery cannot split an existing saved period safely') from exc
                        pending.extendleft((a, b, depth + 1) for a, b, _ in children)
                    else:
                        midpoint = start + (end - start) // 2
                        pending.appendleft((first, midpoint.isoformat(), depth + 1))
                        pending.appendleft(((midpoint + timedelta(days=1)).isoformat(), last, depth + 1))
                    if self.db:
                        self.log.info('Splitting unavailable report %s to %s, level %s', first, last, depth + 1)
                    self.unverified = []
                    continue
                if self.consecutive_unavailable >= 3 or self.unavailable_attempts >= 6:
                    self.recovery_stopped = True
                    if self.db:
                        self.log.warning('Source backfill paused; consecutive unavailable=%s total=%s',
                                         self.consecutive_unavailable, self.unavailable_attempts)
                if on_unavailable:
                    on_unavailable(first, last)
                else:
                    raise
                if self.recovery_stopped:
                    self.unverified = [(start, end) for start, end, _ in pending]
                    break
                self.unverified = []
                continue
            except Exception:
                self.unverified = [(start, end) for start, end, _ in pending]
                raise
            self.consecutive_unavailable = 0
            # Retain pending children while the caller validates/commits a yield.
            self.unverified = [(a, b) for a, b, _ in pending]
            yield first, last, xml
            self.unverified = []


def fetch_flex(query_id, token, timeout=20, from_day=None, to_day=None, request_pacer=None):
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
        if (last - first).days + 1 > MAX_FLEX_WINDOW_DAYS:
            raise ValueError("Flex date range exceeds 365 days")
        params.update({"fd": first.strftime("%Y%m%d"), "td": last.strftime("%Y%m%d")})
    url = base + "SendRequest?" + urlencode(params)
    pacer = request_pacer or FlexRequestPacer()
    pacer.wait()
    try:
        with _opener.open(Request(url, headers=headers), timeout=timeout) as response:
            status = ET.fromstring(response.read())
    except (HTTPError, URLError, TimeoutError):
        raise ValueError("Flex service request failed; check the query, token and connection") from None
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
    # Larger backfills can take longer to generate. Poll the same reference;
    # never submit the identical report again just because it is still pending.
    polls = 30 if from_day is not None and (last - first).days + 1 > 28 else 8
    for attempt in range(polls):
        pacer.wait()
        try:
            with _opener.open(Request(url, headers=headers), timeout=timeout) as response:
                data = response.read(MAX_FLEX_REPORT_BYTES + 1)
        except (HTTPError, URLError, TimeoutError):
            raise ValueError("Flex statement download failed; check the query, token and connection") from None
        if len(data) > MAX_FLEX_REPORT_BYTES:
            raise FlexReportTooLarge("Flex report is too large")
        error = ET.fromstring(data)
        code = error.findtext("ErrorCode")
        if not code and any(node.tag.rsplit("}", 1)[-1] == "FlexStatements"
                            for node in error.iter()):
            return data
        message = error.findtext("ErrorMessage") or "Flex statement unavailable"
        if code != "1019":
            raise FlexServiceError(code, message, "GetStatement")
        if attempt < polls - 1:
            time.sleep(8)
    raise FlexReportGenerationTimeout("Flex statement is still being generated")
