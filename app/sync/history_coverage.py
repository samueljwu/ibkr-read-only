"""Date coverage shared by history imports and routine catch-up."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import xml.etree.ElementTree as ET


def report_through(today=None, *, now=None):
    """Use yesterday for explicit dates, or the previous New York weekday."""
    if today is not None:
        return today - timedelta(days=1)
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('Automatic Flex planning requires an aware clock')
    try:
        through = instant.astimezone(ZoneInfo('America/New_York')).date() - timedelta(days=1)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError('New York timezone data is unavailable; repair the local timezone data '
                           'or use Import history with explicit dates') from exc
    while through.weekday() >= 5:
        through -= timedelta(days=1)
    return through


def account_open_day(db, account):
    value = db.metadata(f'ibkr_account_open_date:{account}')
    return date.fromisoformat(value) if value else None


def remember_account_open_day(db, account, xml):
    days = set()
    for node in ET.fromstring(xml).iter():
        if node.tag.rsplit('}', 1)[-1] == 'AccountInformation' and node.get('accountId', account) == account:
            value = node.get('dateOpened')
            if value:
                compact = value[:10].replace('-', '')
                days.add(date.fromisoformat(f'{compact[:4]}-{compact[4:6]}-{compact[6:8]}'))
    if len(days) > 1:
        raise ValueError('Conflicting broker account opening dates')
    if days:
        opened = next(iter(days))
        prior = account_open_day(db, account)
        if prior and prior != opened:
            raise ValueError('Broker account opening date changed; review account information')
        db.set_metadata(f'ibkr_account_open_date:{account}', opened.isoformat())


def covered_windows(db, account, source, portfolio_id=1):
    if source == 'cash':
        return db.conn.execute('''SELECT DISTINCT period_from,period_to
            FROM cash_reconciliations WHERE portfolio_id=? AND account_id=?''',
            (portfolio_id, account)).fetchall()
    return db.conn.execute('''SELECT from_day,to_day FROM broker_sync_windows
        WHERE account_id=? AND source=? AND status='complete' ''',
        (account, {'trades': 'flex', 'analytics': 'flex_analytics'}[source])).fetchall()


def missing_windows(db, account, source, first, last, days, portfolio_id=1, *, report_through=None):
    cursor = first
    result = []
    covered = sorted(tuple(row) for row in covered_windows(db, account, source, portfolio_id))
    def append(start, end):
        while start <= end:
            size = min(days, 28) if source in {'analytics', 'cash'} and days <= 30 else min(days, 365)
            stop = min(start + timedelta(days=size - 1), end)
            if stop.weekday() >= 5:
                # Older Activity reports can reject a weekend ending
                # date with 1003 even though all nearby weekdays are available.
                # Include the following completed Monday rather than dropping
                # weekend activity. Cash must replay whole overlapping periods.
                weekday_end = stop + timedelta(days=7 - stop.weekday())
                if weekday_end > start + timedelta(days=364):
                    # A full-year request cannot extend past Flex's 365-day
                    # limit. Keep the weekend in the following request.
                    stop -= timedelta(days=stop.weekday() - 4)
                    result.append((start.isoformat(), stop.isoformat()))
                    start = stop + timedelta(days=1)
                    continue
                limit = report_through or last
                if source == 'cash':
                    while weekday_end <= limit:
                        overlapping = [date.fromisoformat(b) for a,b in covered
                                       if date.fromisoformat(a) <= weekday_end
                                       and date.fromisoformat(b) >= start]
                        extended = max([weekday_end, *overlapping])
                        if extended.weekday() >= 5:
                            extended += timedelta(days=7 - extended.weekday())
                        if extended == weekday_end:
                            break
                        weekday_end = extended
                if weekday_end <= min(limit, start + timedelta(days=364)):
                    stop = weekday_end
            result.append((start.isoformat(), stop.isoformat()))
            start = stop + timedelta(days=1)
    for start_text, end_text in covered:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        if end < cursor:
            continue
        if start > cursor:
            append(cursor, min(start - timedelta(days=1), last))
            if result:
                cursor = max(cursor, date.fromisoformat(result[-1][1]) + timedelta(days=1))
        cursor = max(cursor, end + timedelta(days=1))
        if cursor > last:
            break
    append(cursor, last)
    return result


def catchup_windows(db, account, source, today, days, portfolio_id=1):
    through = report_through(today)
    covered = covered_windows(db, account, source, portfolio_id)
    # Fresh setup stays small. Once history is imported, preserve interior gaps.
    first = min((date.fromisoformat(row[0]) for row in covered),
                default=through - timedelta(days=29))
    first = max(first, account_open_day(db, account) or first)
    if first > through:
        return []
    gaps = missing_windows(db, account, source, first, through, days, portfolio_id)
    # Refresh recent executions/NAV for broker corrections. Cash is reconciled
    # in complete, non-overlapping periods and is replayed explicitly instead.
    if source != 'cash' and covered:
        recent = (max(first, through - timedelta(days=7)).isoformat(), through.isoformat())
        if not any(start <= recent[0] and end >= recent[1] for start, end in gaps):
            gaps.append(recent)
    return sorted(set(gaps), reverse=bool(covered))
