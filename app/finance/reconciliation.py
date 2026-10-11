"""Dated, account-scoped comparisons; missing evidence is never a zero balance.

Cash uses the broker opening anchor, canonical executions and native cash fees.
Statement arithmetic is a separate check, not independent verification.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from hashlib import sha256

CENT = Decimal('0.01')


def number(value):
    if value is None or str(value).strip() in {'', '--', '-'}:
        return None
    try:
        result = Decimal(str(value).replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def cash_evidence(record):
    """Additive adapter: preserve original raw fields and their native precision."""
    raw = record.get('raw') or record
    flex = record.get('source') == 'flex' or 'ibCommission' in raw
    def original(*names):
        return next((raw[name] for name in names if raw.get(name) is not None), None)
    return {
        'day': record.get('trade_date') or original('trade_date', 'tradeDate'),
        'basis': 'trade-date', 'currency': record.get('currency') or raw.get('currency'),
        'security_type': record.get('security_type') or original('assetCategory', 'security_type'),
        'symbol': record.get('symbol') or raw.get('symbol') or raw.get('statement_symbol'),
        'side': record.get('side') or original('buySell', 'side'),
        'executed_at': record.get('executed_at') or original('tradeTime', 'time'),
        'quantity': original('quantity') if original('quantity') is not None else record.get('quantity'),
        'price': original('tradePrice', 'price') if original('tradePrice', 'price') is not None else record.get('price'),
        'multiplier': record.get('multiplier'),
        'proceeds': original('proceeds'),
        'commission': original('ibCommission') if flex else original('signed_commission'),
        'commission_currency': record.get('commission_currency') or original('ibCommissionCurrency', 'commission_currency'),
        'taxes': original('taxes') if original('taxes') is not None else record.get('taxes'),
        'posting_day': original('reportDate', 'settleDateTarget', 'settlement_date'),
        'revision': record.get('_cash_revision', 0),
        'portfolio_id': record.get('_cash_portfolio_id'),
    }


@dataclass(frozen=True)
class Issue:
    code: str
    category: str
    summary: str
    count: int = 1
    entities: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    action: str = ''
    technical_details: str = ''


@dataclass(frozen=True)
class Check:
    check: str
    status: str
    account: str
    portfolio: int
    period_from: str = ''
    period_to: str = ''
    currency: str = ''
    local: Decimal | None = None
    broker: Decimal | None = None
    difference: Decimal | None = None
    tolerance: Decimal = CENT
    completeness: str = ''
    source: str = ''
    reason: str = ''
    action: str = ''
    issues: tuple[Issue, ...] = ()
    technical_details: str = ''


def _day(value):
    value = str(value or '').split(';')[0]
    if len(value) >= 8 and value[:8].isdigit():
        value = f'{value[:4]}-{value[4:6]}-{value[6:8]}'
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return ''


def _brief(values, limit=3):
    values = sorted(set(filter(None, values)))
    return ', '.join(values[:limit]) + (f' (+{len(values) - limit} more)' if len(values) > limit else '')


def issue_text(issue):
    text = issue.summary
    if issue.entities:
        text += ' Affected: ' + _brief(issue.entities) + '.'
    if issue.dates:
        text += ' Recorded dates: ' + _brief(issue.dates, 2) + '.'
    return text


def _cash_issues(reasons, contexts, first, last, windows, opening_trades):
    """Presentation only: financial status is determined from original blockers."""
    groups = {}
    def add(code, category, message, action, identity, symbol='', day='', technical=''):
        group = groups.setdefault(code, dict(category=category, message=message, action=action,
                     identities=set(), entities=set(), dates=set(), technical=[]))
        group['identities'].add(identity)
        if symbol:
            group['entities'].add(symbol)
        if day:
            group['dates'].add(day)
        if technical and technical not in group['technical']:
            group['technical'].append(technical)
    handled = set()
    for reason, row, raw in contexts:
        handled.add(reason)
        raw = raw.get('raw', raw)
        symbol = str(raw.get('symbol') or raw.get('underlyingSymbol') or '')
        day = _day(raw.get('reported_day') or raw.get('trade_date') or raw.get('tradeDate') or
                   raw.get('dateTime') or raw.get('date') or raw.get('reportDate'))
        identity = (row.get('source', ''), row.get('source_id', reason))
        technical = f"{row.get('source', '')} / {row.get('source_id', '')}: {reason}"
        if symbol or day:
            technical += f'\n  Instrument: {symbol or "unknown"}; recorded date: {day or "unknown"}'
        if row.get('source') == 'flex_event':
            kind = str(raw.get('transactionType') or '').casefold()
            labels = {'expiration': 'Option expirations', 'exercise': 'Option exercises',
                      'assignment': 'Option assignments'}
            message = labels.get(kind, 'Option events' if row.get('source_id', '').startswith('OptionEAE:')
                                 or raw.get('assetCategory') == 'OPT' else 'Corporate actions or transfers')
            add('event_' + kind if kind in labels else 'other_events', 'app_limit',
                message + ' cannot yet be included in this app’s cash comparison',
                'Option or corporate events need app-side review; reimporting alone does not add support '
                'for their cash effects.', identity, symbol, day, technical)
        elif row.get('source') in {'flex_closed_lot', 'flex_open_lot'}:
            bought = _day(raw.get('openDateTime'))
            if bought:
                technical += f'\n  Original opening date: {bought}'
            conid = str(raw.get('conid') or raw.get('con_id') or '')
            try:
                opening_side = 'SELL' if float(raw.get('quantity', 0)) < 0 else 'BUY'
            except (TypeError, ValueError):
                opening_side = 'BUY'
            missing_opening = (bought and conid and (conid, bought, opening_side) not in opening_trades
                               and (conid, 'transferred', opening_side) not in opening_trades
                               and not _covered(windows, bought, bought))
            if row.get('status') == 'pending' and missing_opening:
                action = ('Trades history does not cover the recorded acquisition dates. Open Settings > '
                          'Import history for those dates; see History import Details for older statement options.')
                add('missing_openings', 'missing_input', 'Lot acquisition dates are outside saved Trades coverage',
                    action, identity, symbol, bought, technical)
            else:
                conflict = 'differs' in reason or 'disagree' in reason or 'basis' in reason.casefold()
                add('lot_conflict' if conflict else 'lot_matching', 'source_inconsistency' if conflict else 'app_review',
                    'Saved lot costs or allocations need review' if conflict else 'IBKR lot records are not yet matched to saved trades',
                    'For unmatched lots, open History import Details. If the needed trades are already saved, '
                    'matching needs app-side review; no additional download is identified.',
                    identity, symbol, day, technical)
        else:
            conflict = 'differs' in reason or 'disagree' in reason
            add('saved_amount_difference' if conflict else 'source_review',
                'source_inconsistency' if conflict else 'app_review',
                'Saved trade, fee or tax amounts disagree with IBKR source records' if conflict else 'Saved broker records need review',
                'Review the affected saved records and IBKR statement; copy diagnostics if they already agree.',
                identity, symbol, day, technical)
    for reason in dict.fromkeys(reasons):
        if reason in handled:
            continue
        lower = reason.casefold()
        action = 'Copy diagnostics and review the affected saved records; do not change amounts to force agreement.'
        if reason.startswith('Original Cash Report'):
            code, category, message = 'cash_report_evidence', 'missing_input', 'This saved Cash report lacks details needed for the comparison'
            action = (f'Settings > Import history: select Cash, {first} to {last}, and enable Recheck saved reports. '
                      'Include the entire saved period.')
        elif reason.startswith('Execution coverage'):
            code, category, message = 'trade_coverage', 'missing_input', f'Trades history is not fully checked for {first} to {last}'
            action = f'Settings > Import history: select Trades for {first} to {last}. Check History import Details for remaining gaps.'
        elif 'component differences:' in lower:
            code, category, message = 'component_difference', 'source_inconsistency', reason
            action = 'Expand this cash check and review the components marked Difference against your IBKR statement.'
        elif 'differs' in lower or 'disagree' in lower:
            code, category, message = 'saved_amount_difference', 'source_inconsistency', 'Saved trade, fee, refund or tax amounts disagree with IBKR source records'
        elif 'futures' in lower:
            code, category, message = 'futures_cash', 'app_limit', 'This app cannot yet compare futures margin cash'
            action = 'Compare futures cash with your IBKR statement; repeating the import does not add support for margin calculations.'
        elif 'refund' in lower:
            code, category, message = 'refund_matching', 'app_review', 'Commission refunds are not fully matched or converted'
        elif 'local trade' in lower or 'execution evidence' in lower:
            code, category, message = 'execution_matching', 'app_review', 'Saved trades are not linked to verified IBKR executions'
            action = ('Review the affected trades in Technical details. Import missing Trades only if they are absent; '
                      'otherwise copy diagnostics for matching review.')
        elif 'fx' in lower and ('conflict' in lower or 'duplicate' in lower or 'ambiguous' in lower):
            code, category, message = 'fx_matching', 'source_inconsistency', 'Currency conversion records are ambiguous or disagree'
        elif 'missing' in lower or 'incomplete' in lower or 'invalid' in lower:
            code, category, message = 'execution_fields', 'missing_input', 'Saved amounts, fees, taxes or trade details are incomplete'
            action = (f'Check that your Trades query includes Proceeds, Commission and its currency, and Taxes. '
                      f'In Settings > Import history, select Trades for {first} to {last} and enable Recheck saved reports.')
        else:
            code, category, message = 'other_review', 'app_review', 'Some saved records still need review'
        add(code, category, message, action, reason, technical=reason)
    issues = []
    for code, group in groups.items():
        count = len(group['identities'])
        counted = code.startswith('event_') or code in {'other_events', 'lot_matching', 'lot_conflict', 'missing_openings', 'source_review'}
        summary = group['message'] + (f' ({count:,} {"record" if count == 1 else "records"}).' if counted else '.')
        issues.append(Issue(code, group['category'], summary, count, tuple(sorted(group['entities'])),
                            tuple(sorted(group['dates'])), group['action'], '\n'.join(group['technical'])))
    return tuple(sorted(issues, key=lambda issue: 0 if issue.code == 'cash_report_evidence' else 1))


def _issue_summary(issues):
    lines = [issue_text(issue) for issue in issues[:5]]
    if len(issues) > 5:
        lines.append(f'{len(issues) - 5} other issue categories; see Technical details.')
    return '\n'.join(lines)


def _issue_actions(issues):
    ordered = sorted(issues, key=lambda issue: 0 if issue.code == 'cash_report_evidence' else 1)
    actions = list(dict.fromkeys(issue.action for issue in ordered if issue.action))
    return '\n'.join(actions[:4]) + ('\nSee Technical details for other actions.' if len(actions) > 4 else '')


def _cash_presentation(reasons, contexts, first, last, windows, opening_trades):
    issues = _cash_issues(reasons, contexts, first, last, windows, opening_trades)
    return dict(reason=_issue_summary(issues), action=_issue_actions(issues), issues=issues,
                technical_details='\n\n'.join(issue_text(issue) + '\nCategory: ' + issue.category + '\n' +
                                             issue.technical_details + '\nAction: ' + issue.action for issue in issues))


def _accounted_expiration(row, raw, executions):
    """Only an already linked, still unchanged, explicitly zero-cash expiry."""
    trade = next((t for t in executions if t['id'] == row['trade_row_id']), None)
    qty = number(raw.get('quantity'))
    return bool(row['status'] == 'informational' and raw.get('transactionType') == 'Expiration'
                and raw.get('assetCategory') == 'OPT' and number(raw.get('tradePrice')) == 0
                and number(raw.get('proceeds')) == 0 and trade and trade['security_type'] == 'OPT'
                and number(trade['price']) == 0 and qty and number(trade['quantity']) == abs(qty)
                and str(trade['position_con_id']) == str(raw.get('conid'))
                and trade['position_portfolio_id'] == trade['portfolio_id']
                and trade['side'] == ('BUY' if qty > 0 else 'SELL')
                and trade['trade_date'] == _day(raw.get('date')) and trade['currency'] == raw.get('currency'))


def _covered(windows, first, last):
    cursor = date.fromisoformat(first)
    for start, end in sorted(windows):
        if start > cursor.isoformat():
            return False
        if end >= cursor.isoformat():
            cursor = date.fromisoformat(end) + timedelta(days=1)
        if cursor.isoformat() > last:
            return True
    return False


def _raw(row):
    if '_decoded_raw' in row:
        return row['_decoded_raw']
    try:
        raw = json.loads(row['raw_json'])
        return raw if isinstance(raw, dict) else {}
    except (ValueError, TypeError):
        return {}


def _legacy_evidence(row, trade):
    raw = _raw(row)
    if '_cash_evidence' in raw:
        return raw['_cash_evidence']
    # Old saved sources retain enough raw Flex/CSV evidence in many cases.
    record = dict(trade or {}, source=row['source'], raw=raw)
    record.update(commission_currency=raw.get('ibCommissionCurrency') or raw.get('commission_currency'))
    if raw.get('ibExecID'):
        from app.broker.broker_sync import execution_identity
        record['_cash_revision'] = execution_identity({'ib_exec_id': raw['ibExecID']})[1]
    return cash_evidence(record)


def independent_cash_checks(db, portfolio_id=1):
    account = db.setting('ib_account_id').strip()
    bridges = [r for r in db.cash_reconciliation_rows(portfolio_id) if r['account_id'] == account]
    if not bridges:
        return [Check('Independent cash', 'Unavailable', account, portfolio_id,
                      reason='No cash statement saved for the selected account.',
                      action='Import Cash history for completed dates.' if account else
                             'Set your IBKR account and Cash query in Settings, then import completed dates.')]
    windows = [(r[0], r[1]) for r in db.conn.execute('''SELECT from_day,to_day
        FROM broker_sync_windows WHERE account_id=? AND source IN ('flex','activity_statement')
        AND status='complete' ''', (account,))]
    sources = [dict(r) for r in db.conn.execute('''SELECT r.* FROM broker_source_records r
        LEFT JOIN trades t ON t.id=r.trade_row_id WHERE r.account_id=?
        AND (t.portfolio_id=? OR r.trade_row_id IS NULL)''', (account, portfolio_id))]
    for row in sources:
        row['_decoded_raw'] = _raw(row)
    executions = [dict(r) for r in db.conn.execute('''SELECT t.*,e.execution_key,e.revision,
        p.con_id AS position_con_id,p.portfolio_id AS position_portfolio_id
        FROM broker_executions e JOIN trades t ON t.id=e.trade_row_id JOIN positions p ON p.id=t.position_id
        WHERE e.account_id=? AND t.portfolio_id=?''', (account, portfolio_id))]
    local_only = [dict(r) for r in db.conn.execute('''SELECT t.* FROM trades t
        WHERE t.portfolio_id=? AND NOT EXISTS (SELECT 1 FROM broker_executions e
        WHERE e.trade_row_id=t.id AND e.account_id=?)''', (portfolio_id, account))]
    opening_trades = set()
    for row in db.conn.execute('''SELECT p.con_id,t.trade_date,t.side,t.source FROM trades t
            JOIN positions p ON p.id=t.position_id WHERE t.portfolio_id=?''', (portfolio_id,)):
        opening_trades.add((str(row[0]), row[1], row[2]))
        if row[3] in {'IBKR legacy transfer', 'IBKR statement transfer'}:
            opening_trades.add((str(row[0]), 'transferred', row[2]))
    movements = [r for r in db.cash_movement_rows(portfolio_id) if r['account_id'] == account]
    refunds = {r['source_key']: dict(r) for r in db.conn.execute('''SELECT * FROM trade_fee_adjustments
        WHERE account_id=? AND portfolio_id=?''', (account, portfolio_id))}
    by_execution = defaultdict(list)
    fx = defaultdict(list)
    for row in sources:
        if row['trade_row_id'] is not None:
            by_execution[row['execution_key']].append(row)
        elif row['status'] == 'cash-only':
            ev = _legacy_evidence(row, None)
            if ev.get('portfolio_id') == portfolio_id:
                fx[row['execution_key']].append((row, ev))
    evidence = []
    for trade in executions:
        candidates = [(r, _legacy_evidence(r, trade)) for r in by_execution[trade['execution_key']]
                      if r['source'] in {'flex', 'activity_statement', 'tws'} and
                      (r['status'] == 'complete' or r['status'].startswith('pending '))]
        candidates = [(r, ev) for r, ev in candidates if ev.get('revision', 0) == trade['revision']]
        if candidates:
            # One canonical execution, current correction, richest native evidence.
            row, ev = max(candidates, key=lambda pair: (sum(pair[1].get(k) is not None
                          for k in ('proceeds', 'commission', 'commission_currency', 'taxes')), pair[0]['seen_at']))
            ev = dict(ev)
            # Cash proceeds are calculated from the saved local execution. Split
            # share-only rows retain the original execution, and contribute no cash.
            ev.update(quantity=trade['quantity'], price=trade['price'], multiplier=trade['multiplier'],
                      side=trade['side'], day=trade['trade_date'], currency=trade['currency'],
                      local_commission_usd=trade['commission_usd'], local_tax_lc=trade['tax_lc'],
                      local_tax_usd=trade['tax_usd'], local_fx=trade['fx_to_usd'])
            evidence.append((row, ev))
        else:
            evidence.append(({'source_id': trade['execution_key']}, {'day': trade['trade_date'],
                             'currency': trade['currency'], 'missing': 'Accepted native execution evidence is missing.'}))
    fx_signatures = defaultdict(set)
    for key, candidates in fx.items():
        for row, ev in candidates:
            signature = tuple(str(ev.get(k) or '') for k in ('day', 'executed_at', 'symbol', 'side', 'quantity', 'price'))
            fx_signatures[signature].add((key, row['source']))
    for candidates in fx.values():
        revision = max(ev.get('revision', 0) for _, ev in candidates)
        current = [(r, ev) for r, ev in candidates if ev.get('revision', 0) == revision]
        score = lambda ev: sum(ev.get(k) is not None for k in ('proceeds', 'commission', 'commission_currency', 'taxes'))
        best = max(score(ev) for _, ev in current)
        current = [(r, ev) for r, ev in current if score(ev) == best]
        numeric_fields = {'quantity', 'price', 'multiplier', 'proceeds', 'commission', 'taxes'}
        distinct = {json.dumps({k: number(v).normalize() if k in numeric_fields and number(v) is not None else v
                               for k, v in ev.items()}, sort_keys=True, default=str) for _, ev in current}
        row, ev = current[-1]
        if len(distinct) > 1:
            ev = dict(ev, missing='Conflicting FX evidence without a shared accepted revision.')
        signature = tuple(str(ev.get(k) or '') for k in ('day', 'executed_at', 'symbol', 'side', 'quantity', 'price'))
        matches = fx_signatures[signature]
        if len({key for key, _ in matches}) > 1 and len({source for _, source in matches}) > 1:
            ev = dict(ev, missing='Possible duplicate FX conversion across sources without a shared execution identity.')
        evidence.append((row, ev))
    results = []
    for bridge in bridges:
        first, last, code = bridge['period_from'], bridge['period_to'], bridge['currency']
        scope = dict(account=account, portfolio=portfolio_id, period_from=first, period_to=last, currency=code)
        blockers = []
        contexts = []
        def block(reason, row=None, raw=None):
            blockers.append(reason)
            if row is not None:
                contexts.append((reason, row, raw or _raw(row)))
        if any(number(bridge.get(field)) is None for field in
               ('opening_cash', 'closing_cash', 'trade_sales', 'trade_purchases', 'trade_commissions', 'trade_tax', 'nontrade_cash')):
            blockers.append('Saved Cash Report has missing or invalid native amounts.')
        if not _covered(windows, first, last):
            blockers.append('Execution coverage has missing or unfinished dates.')
        totals = defaultdict(Decimal)
        missing = defaultdict(list)
        for trade in local_only:
            if first <= trade['trade_date'] <= last:
                # Verified zero-cash split/transfer rows are explicitly noncash.
                noncash = any(r['trade_row_id'] == trade['id'] and r['status'] == 'complete' and
                              (r['source'] == 'activity_statement_transfer_lot' or
                               r['source'] == 'flex_event' and _raw(r).get('type') == 'FS') for r in sources)
                if not noncash:
                    blockers.append(f"Local trade {trade['trade_id']} has no accepted broker execution or noncash evidence.")
        for row, ev in evidence:
            if not ev.get('day') or not first <= ev['day'] <= last:
                continue
            currencies = {ev.get('currency'), ev.get('commission_currency')}
            is_fx = ev.get('security_type') == 'CASH'
            pair = (ev.get('symbol') or '').upper().split('.')
            if is_fx and len(pair) == 2:
                currencies.update(pair)
            if code not in currencies and ev.get('currency') and ev.get('commission_currency'):
                continue
            issue = ev.get('missing')
            if issue:
                blockers.append(issue)
                for component in ('trade_sales', 'trade_purchases', 'trade_commissions', 'trade_tax'):
                    missing[component].append(issue)
                continue
            qty, price, mult = (number(ev.get(k)) for k in ('quantity', 'price', 'multiplier'))
            proceeds = number(ev.get('proceeds'))
            if qty is None or price is None or mult is None or ev.get('side') not in {'BUY', 'SELL'}:
                missing['trade_sales'].append('Native quantity, price, multiplier or side missing.')
                missing['trade_purchases'].append('Native quantity, price, multiplier or side missing.')
            else:
                gross = abs(qty) * price * mult * (-1 if ev['side'] == 'BUY' else 1)
                if is_fx:
                    if len(pair) != 2 or pair[1] != ev.get('currency'):
                        blockers.append('FX pair or quote currency is ambiguous.')
                        missing['trade_sales'].append(blockers[-1])
                        missing['trade_purchases'].append(blockers[-1])
                        continue
                    leg = abs(qty) * (1 if ev['side'] == 'BUY' else -1) if code == pair[0] else gross
                    if code in pair:
                        totals['trade_sales' if leg >= 0 else 'trade_purchases'] += leg
                elif code == ev.get('currency'):
                    totals['trade_sales' if gross >= 0 else 'trade_purchases'] += gross
                if proceeds is None:
                    missing['trade_sales'].append('Signed native proceeds missing; accounting basis cannot be confirmed.')
                    missing['trade_purchases'].append('Signed native proceeds missing; accounting basis cannot be confirmed.')
                elif abs(proceeds - gross) > CENT:
                    block(f"{row['source_id']}: native proceeds disagree with local quantity × price × multiplier.", row, ev)
                if ev.get('security_type') == 'FUT':
                    blockers.append('Futures use margin/variation cash; notional proceeds cannot establish a cash bridge.')
            commission = number(ev.get('commission'))
            if commission is None or not ev.get('commission_currency'):
                missing['trade_commissions'].append('Original signed commission or native fee currency missing.')
            elif code == ev['commission_currency']:
                totals['trade_commissions'] += commission
            if commission is not None and 'local_commission_usd' in ev:
                fee_fx = (Decimal(1) if ev.get('commission_currency') == 'USD' else
                          number(ev.get('local_fx')) if ev.get('commission_currency') == ev.get('currency') else
                          number(db.fx_rate(ev.get('commission_currency'), ev['day'])) if ev.get('commission_currency') else None)
                if fee_fx and abs(number(ev['local_commission_usd']) + commission * fee_fx) > CENT:
                    block(f"{row['source_id']}: saved local fee differs from the original native fee.", row, ev)
            tax = number(ev.get('taxes'))
            if tax is None:
                missing['trade_tax'].append('Transaction tax evidence missing (zero must be explicit).')
            elif code == ev.get('currency'):
                totals['trade_tax'] += tax
            if tax is not None and 'local_tax_lc' in ev:
                local_tax = number(ev['local_tax_lc'])
                local_usd, local_fx = number(ev['local_tax_usd']), number(ev['local_fx'])
                if local_tax is None or local_usd is None:
                    block(f"{row['source_id']}: saved local tax is incomplete.", row, ev)
                elif abs(local_tax + tax) > CENT or local_fx and abs(local_usd + tax * local_fx) > CENT:
                    block(f"{row['source_id']}: saved local tax differs from native execution evidence.", row, ev)
        for row in sources:
            raw = _raw(row)
            day = raw.get('reported_day') or raw.get('trade_date') or raw.get('tradeDate') or raw.get('dateTime')
            if day:
                day = str(day)[:10].replace('-', '')[:8]
                if len(day) == 8 and day.isdigit():
                    day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
            in_period = not day or first <= day <= last
            raw_scope = raw.get('portfolio_id', raw.get('_portfolio_id',
                        (raw.get('_cash_evidence') or {}).get('portfolio_id')))
            if raw_scope is not None and raw_scope != portfolio_id:
                continue
            if row['status'] in {'review', 'pending'} and in_period:
                block(f"{row['source_id']}: {row['issue'] or row['status']}", row, raw)
            if row['source'] == 'flex_fee' and in_period and raw.get('currency') == code:
                if row['status'] == 'complete' and number(raw.get('amount')) is not None:
                    totals['trade_commissions'] += number(raw['amount'])
                    refund = refunds.get(row['source_id'])
                    fee_fx = Decimal(1) if code == 'USD' else number(db.fx_rate(code, raw['reported_day']))
                    if refund is None or refund['trade_row_id'] != row['trade_row_id']:
                        blockers.append('Native commission refund has no matching local fee adjustment.')
                    elif fee_fx is None:
                        blockers.append('Local commission refund conversion is unavailable.')
                    elif number(refund['refund_usd']) is None or abs(number(refund['refund_usd']) - number(raw['amount']) * fee_fx) > CENT:
                        blockers.append('Local commission refund differs from native refund evidence.')
                else:
                    missing['trade_commissions'].append('Commission refund is unresolved.')
            if (row['source'] == 'flex_event' and in_period and raw.get('type') != 'FS'
                    and not _accounted_expiration(row, raw, executions)):
                block(f"{row['source_id']}: lifecycle cash needs explicit accounting evidence.", row, raw)
        for row in movements:
            if row['currency'] == code and first <= row['reported_day'] <= last:
                if row['kind'] not in {'Transaction tax', 'Commission adjustment'}:
                    value = number(row['amount'])
                    if value is None:
                        missing['nontrade_cash'].append('Invalid native cash movement amount.')
                    else:
                        totals['nontrade_cash'] += value
        report = next((_raw(row) for row in sources if row['source'] == 'cash_report' and
                       _raw(row).get('portfolio_id') == portfolio_id and
                       _raw(row).get('period_from') == first and _raw(row).get('period_to') == last and
                       _raw(row).get('currency') == code and _raw(row).get('source_hash') == bridge['source_hash']), None)
        if report is None or report.get('accounting_basis') != 'trade-date':
            blockers.append('Original Cash Report component and accounting-basis evidence is missing; '
                            'use Import history with Recheck saved reports for this complete period.')
        complete = not blockers and not any(missing.values())
        evidence_difference = any('disagree' in reason or 'differs' in reason for reason in blockers)
        uncertain = any('disagree' not in reason and 'differs' not in reason for reason in blockers)
        component_differences = []
        if report:
            grouping = {r['source_key']: r.get('report_component') for r in report.get('movements', [])}
            subtotals = defaultdict(Decimal)
            for row in movements:
                if row['currency'] == code and first <= row['reported_day'] <= last and grouping.get(row['source_key']):
                    value = number(row['amount'])
                    if value is not None:
                        subtotals[grouping[row['source_key']]] += value
            for field, label in (('depositWithdrawals', 'Deposits / withdrawals'), ('dividends', 'Dividends'),
                                 ('paymentInLieu', 'Payments in lieu'), ('withholdingTax', 'Withholding tax'),
                                 ('otherFees', 'Other fees'), ('brokerInterest', 'Broker interest'), ('bondInterest', 'Bond interest')):
                reported = number(report.get('report_fields', {}).get(field))
                if reported is None:
                    continue
                difference = subtotals[field] - reported
                off = abs(difference) > CENT
                if off:
                    component_differences.append(label)
                results.append(Check('Non-trade · ' + label, 'Difference' if off else 'Verified' if complete else 'Partial',
                    **scope, local=subtotals[field], broker=reported, difference=difference,
                    source='Saved native cash movements / Cash Report component',
                    reason='Individual non-trade components cannot offset one another.',
                    action='Review cash movement amounts and posting dates.' if off else ''))
        for component, name in (('trade_sales', 'Sales / positive FX legs'), ('trade_purchases', 'Purchases / negative FX legs'),
                                ('trade_commissions', 'Commissions / refunds'), ('trade_tax', 'Transaction tax'),
                                ('nontrade_cash', 'Non-trade movements')):
            broker = number(bridge.get(component))
            local = totals[component] if not missing[component] and not uncertain else None
            difference = local - broker if local is not None and broker is not None else None
            off = difference is not None and abs(difference) > CENT
            if off:
                component_differences.append(name)
            results.append(Check(name, 'Difference' if off else 'Verified' if complete and difference is not None else 'Partial',
                **scope, local=local, broker=broker, difference=difference,
                completeness='Complete saved interval' if complete else 'Incomplete saved interval',
                source='Saved execution ledger + native cash movements / Cash Report',
                **_cash_presentation(missing[component] + blockers +
                                    ([f'Component differences: {name}'] if off else []), contexts,
                                    first, last, windows, opening_trades)))
        local = number(bridge['opening_cash']) + sum(totals.values(), Decimal(0)) if not any(missing.values()) and not uncertain else None
        broker = number(bridge['closing_cash'])
        difference = local - broker if local is not None and broker is not None else None
        off = difference is not None and abs(difference) > CENT
        results.append(Check('Independent cash', 'Difference' if off or component_differences or evidence_difference else 'Verified' if complete else 'Partial',
            **scope, local=local, broker=broker, difference=difference,
            completeness='Complete saved interval' if complete else 'Incomplete saved interval',
            source='Broker opening anchor + local executions + native cash movements / Cash Report close',
            **_cash_presentation(blockers + [s for values in missing.values() for s in values] +
                        (['Component differences: ' + ', '.join(component_differences)] if component_differences else []),
                        contexts, first, last, windows, opening_trades)))
        arithmetic = number(bridge['difference'])
        results.append(Check('Statement arithmetic', 'Unavailable' if arithmetic is None else 'Verified' if abs(arithmetic) <= CENT else 'Difference',
                             **scope, difference=arithmetic, source='Cash Report and its Cash Transactions',
                             reason='Checks the report itself; independent executions are checked separately.'))
    periods = defaultdict(set)
    for bridge in bridges:
        periods[bridge['period_from'], bridge['period_to']].add(bridge['currency'])
    for (first, last), reported_currencies in periods.items():
        affected = set()
        for _, ev in evidence:
            if ev.get('day') and first <= ev['day'] <= last:
                affected.update(code for code in (ev.get('currency'), ev.get('commission_currency')) if code)
                if ev.get('security_type') == 'CASH':
                    pair = (ev.get('symbol') or '').split('.')
                    if len(pair) == 2:
                        affected.update(pair)
        affected.update(row['currency'] for row in movements if first <= row['reported_day'] <= last)
        for code in sorted(affected - reported_currencies):
            results.append(Check('Independent cash', 'Partial', account, portfolio_id,
                period_from=first, period_to=last, currency=code,
                completeness='Missing affected currency report', source='Saved executions and native cash movements',
                reason=f'{code} cash movements or an FX leg have no matching currency Cash Report for this interval.',
                action='Include every affected currency in the Cash query. In Settings > Import history, '
                       'select Cash and these complete dates, then enable Recheck saved reports.'))
    return results


def freshness_checks(db, portfolio_id=1):
    """Latest outcomes never relabel an old verified interval as current."""
    from app.sync.history_status import read_status, status_path
    account = db.setting('ib_account_id').strip()
    checks = []
    for source, prefix in (('Trades', 'ibkr_flex'), ('Cash', 'ibkr_cash')):
        state = db.metadata(prefix + '_sync_status')
        if state in {'failed', 'starting', 'running', 'review', 'paused', 'partial'}:
            original = f"Latest {source.lower()} sync: {state}. " + (db.metadata(prefix + '_sync_error') or '')
            action = ('Wait for this sync to finish.' if state in {'starting', 'running'} else
                      'Resume the paused import or retry sync when ready.' if state == 'paused' else
                      'Check the saved sync result in Settings or History import Details before retrying.')
            checks.append(Check(source + ' freshness', 'Partial', account, portfolio_id,
                source='Latest sync outcome', reason=f'Latest {source} sync: {state}. Previously saved periods do not verify current totals.',
                action=action, technical_details=original))
    history = read_status(status_path(db.path))
    gaps = any(any((history.get(key) or {}).values()) for key in
               ('failed_windows', 'unavailable_windows', 'unverified_windows'))
    if history.get('account_id') == account and (gaps or history.get('state') in
            {'failed', 'completed_with_gaps', 'starting', 'running', 'paused'}):
        reasons = [f"Latest history import: {history.get('state', 'incomplete')}."]
        for key, label in (('failed_windows', 'Failed'), ('unavailable_windows', 'Unavailable'),
                           ('unverified_windows', 'Not checked')):
            for source, spans in (history.get(key) or {}).items():
                for span in spans[:3]:
                    first, last = ((span.get('from_date', ''), span.get('to_date', '')) if isinstance(span, dict)
                                   else (span[0], span[1]))
                    reasons.append(f'{label} {source}: {first} to {last}.')
        checks.append(Check('History freshness', 'Partial', account, portfolio_id,
            source='Latest history import outcome',
            reason='Latest history import is unfinished or has unchecked dates.\n' + '\n'.join(reasons[1:5]) +
                   (f'\n{len(reasons) - 5} other date ranges; see Technical details.' if len(reasons) > 5 else ''),
            action='Open History import Details for the remaining dates and source issues.',
            technical_details='\n'.join(reasons) + '\nSaved intervals do not verify current totals.'))
    return checks


class ReconciliationService:
    """Cache full-history evidence until a local or external database write occurs."""
    def __init__(self, db, portfolio_id=1):
        self.db, self.portfolio_id = db, portfolio_id
        self._key, self._cash = None, []
        self._write_key, self._fingerprint = None, None

    def cash_checks(self):
        account = self.db.setting('ib_account_id').strip()
        write_key = (self.db.conn.total_changes, self.db.conn.execute('PRAGMA data_version').fetchone()[0], account, self.portfolio_id)
        if write_key != self._write_key:
            # Quote/account-value writes are frequent. Scan only relevant native
            # columns to fingerprint monetary evidence, without reparsing JSON or
            # rebuilding interval comparisons when those inputs did not change.
            digest = sha256()
            queries = (
                ('''SELECT id,position_id,security_type,source,trade_date,side,quantity,price,multiplier,currency,commission_usd,tax_lc,tax_usd,fx_to_usd
                    FROM trades WHERE portfolio_id=? ORDER BY id''', (self.portfolio_id,)),
                ('''SELECT p.id,p.con_id,p.portfolio_id FROM positions p JOIN trades t ON t.position_id=p.id
                    WHERE t.portfolio_id=? GROUP BY p.id,p.con_id,p.portfolio_id ORDER BY p.id''', (self.portfolio_id,)),
                ('''SELECT e.execution_key,e.trade_row_id,e.revision,e.status FROM broker_executions e JOIN trades t ON t.id=e.trade_row_id
                    WHERE e.account_id=? AND t.portfolio_id=? ORDER BY e.execution_key''', (account, self.portfolio_id)),
                ('''SELECT r.source,r.source_id,r.execution_key,r.trade_row_id,r.status,r.issue,r.raw_json FROM broker_source_records r
                    LEFT JOIN trades t ON t.id=r.trade_row_id WHERE r.account_id=? AND (t.portfolio_id=? OR r.trade_row_id IS NULL)
                    ORDER BY r.source,r.source_id''', (account, self.portfolio_id)),
                ('''SELECT from_day,to_day,status,source_count,imported_count FROM broker_sync_windows
                    WHERE account_id=? AND source IN ('flex','activity_statement') ORDER BY source,from_day,to_day''', (account,)),
                ('''SELECT source_key,reported_day,currency,kind,amount FROM cash_movements
                    WHERE account_id=? AND portfolio_id=? ORDER BY source_key''', (account, self.portfolio_id)),
                ('''SELECT period_from,period_to,currency,opening_cash,trade_sales,trade_purchases,trade_commissions,
                    trade_tax,nontrade_cash,closing_cash,difference,source_hash FROM cash_reconciliations
                    WHERE account_id=? AND portfolio_id=? ORDER BY period_from,period_to,currency''', (account, self.portfolio_id)),
                ('''SELECT source_key,trade_row_id,reported_day,refund_usd FROM trade_fee_adjustments
                    WHERE account_id=? AND portfolio_id=? ORDER BY source_key''', (account, self.portfolio_id)),
                ('''SELECT currency,day,usd_per_unit FROM fx_rates ORDER BY currency,day''', ()),
            )
            for query, args in queries:
                digest.update(query.encode())
                for row in self.db.conn.execute(query, args):
                    digest.update(json.dumps(tuple(row), separators=(',', ':'), default=str).encode())
            self._fingerprint = digest.hexdigest()
            self._write_key = write_key
        key = (self._fingerprint, account, self.portfolio_id)
        if key != self._key:
            self._cash = independent_cash_checks(self.db, self.portfolio_id)
            self._key = key
        return self._cash + freshness_checks(self.db, self.portfolio_id)

    def checks(self, rows, lots):
        account = self.db.setting('ib_account_id').strip()
        scope = dict(account=account, portfolio=self.portfolio_id)
        snapshot = self.db.metadata(f'broker_positions_full_snapshot_{self.portfolio_id}') or ''
        broker_rows = {row['con_id']: row for row in self.db.broker_positions(self.portfolio_id)}
        results = list(self.cash_checks())
        seen = set()
        for row in rows:
            seen.add(row.get('con_id'))
            local, broker = number(row.get('position_qty')), number(row.get('broker_qty'))
            broker_row = broker_rows.get(row.get('con_id'))
            row_snapshot = (broker_row['as_of'] if broker_row else max(snapshot,
                self.db.metadata(f"broker_position_zero_{self.portfolio_id}_{row.get('con_id')}") or ''))
            diff = local - broker if local is not None and broker is not None else None
            status = 'Unavailable' if diff is None else 'Difference' if abs(diff) > Decimal('0.000000001') else 'Verified'
            stale = row_snapshot[:10] != date.today().isoformat()
            if status == 'Verified' and stale:
                status = 'Partial'
            results.append(Check('Quantity · ' + row['display_ticker'], status, **scope,
                period_to=row_snapshot, local=local, broker=broker, difference=diff, tolerance=Decimal('0.000000001'),
                source='Local holdings / broker position snapshot',
                reason='Broker snapshot is missing or dated; refresh positions.' if stale else '',
                action='Refresh broker positions and resolve opening holdings or unmatched trades.' if status != 'Verified' else ''))
        for row in self.db.broker_positions(self.portfolio_id):
            if row['con_id'] not in seen and number(row['quantity']) != 0:
                results.append(Check('Broker-only holding · ' + str(row['con_id']), 'Difference', **scope,
                    period_to=row.get('as_of', snapshot), local=Decimal(0), broker=number(row['quantity']),
                    difference=-number(row['quantity']), source='Broker position snapshot',
                    reason='Broker holding has no local security ledger.', action='Import missing executions or verified transferred opening lots.'))
        quantities = [c for c in results if c.check.startswith(('Quantity · ', 'Broker-only holding · '))]
        quantity_status = ('Difference' if any(c.status == 'Difference' for c in quantities) else
                           'Partial' if any(c.status != 'Verified' for c in quantities) else
                           'Verified' if quantities or snapshot[:10] == date.today().isoformat() else 'Unavailable')
        results.append(Check('Holdings quantities', quantity_status, **scope,
            source='Local ledger / dated broker position snapshots',
            reason=f'{len(quantities):,} security comparisons. Expand to see individual holdings and their snapshot dates.',
            action='Refresh broker positions and resolve missing trade or opening holdings evidence.' if quantity_status != 'Verified' else ''))
        for row in rows:
            if abs(row.get('position_qty') or 0) <= 1e-9:
                continue
            reasons = []
            if row.get('last_px') is None:
                reasons.append('Missing current price')
            if row.get('fx_to_usd') is None or row.get('fx_to_usd') == 0:
                reasons.append('Missing current FX')
            if row.get('daily_pnl_note'):
                reasons.append(row['daily_pnl_note'])
            if row.get('data_status') not in {'OK', 'LIVE', 'DELAYED'}:
                reasons.append(row.get('data_status') or 'Source status unavailable')
            if row.get('unrealized_pnl') is None:
                reasons.append('Unrealized P&L inputs or opening lot basis incomplete')
            if reasons:
                results.append(Check('Inputs · ' + row['display_ticker'], 'Partial', **scope,
                    currency=row.get('currency', ''), source='Local holding inputs',
                    reason='; '.join(dict.fromkeys(reasons)),
                    action='Refresh market data and review this holding’s executions, opening lots, fees and taxes.'))
        for row in lots:
            if row.get('lot_note') and (row.get('realized_net') is None and str(row.get('lot_state', '')).startswith('Closed')
                    or row.get('lot_note') in {'Fee pending', 'Tax pending', 'Unmatched sale'}):
                results.append(Check('Lot inputs · ' + row.get('ticker', str(row.get('position_id', ''))), 'Partial', **scope,
                    period_from=row.get('buy_date', ''), period_to=row.get('sell_date', ''),
                    source='Local matched lots', reason=row['lot_note'], action='Review this lot’s opening basis, matching, fees and tax evidence.'))
        problems = sorted({row.get('data_status', '') for row in rows if abs(row.get('position_qty') or 0) > 1e-9
                           and (row.get('value') is None or row.get('data_status') not in {'OK', 'LIVE', 'DELAYED'})})
        lot_problems = sorted({row.get('lot_note') or row.get('valuation_status') or 'Incomplete lot basis'
                               for row in lots if (str(row.get('lot_state', '')).startswith('Closed') and row.get('realized_net') is None)
                               or row.get('lot_note') in {'Fee pending', 'Tax pending', 'Unmatched sale'}})
        for name, reason in (
            ('Holdings valuation', 'The app does not yet save IBKR holdings values for the same date, prices and currency rates.'),
            ('NAV', 'The app saves current IBKR account totals, but cannot yet compare them with cash and holdings for one matching date.'),
            ('Daily P&L', 'The app does not yet save an IBKR daily profit report for the same market session and currency.'),
            ('Realized / unrealized P&L', 'The app does not yet save an IBKR profit report covering the same trades, fees, taxes and currency rates.')):
            results.append(Check(name, 'Partial' if rows or lots else 'Unavailable', **scope,
                currency='USD', source='Available local calculations; broker comparison unavailable',
                completeness='IBKR comparison needs additional app support', reason=reason + (' Inputs: ' + _brief(problems + lot_problems) if problems or lot_problems else ''),
                action='Review the affected holdings under Missing calculation inputs. Reimporting alone does not enable this IBKR comparison.'))
        return results
