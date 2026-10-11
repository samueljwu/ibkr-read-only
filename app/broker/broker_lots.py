"""Retain broker lot evidence and apply only exact, validated closed-lot links."""
from app.finance.trade_costs import trade_cost_usd

from collections import defaultdict
from hashlib import sha256
import json
import math
import re
from itertools import combinations
import xml.etree.ElementTree as ET

from app.broker.broker_sync import execution_identity, _day, _clock, _effective_multiplier
from app.sync.history_coverage import account_open_day, missing_windows
from datetime import date
from app.finance.services import MatchingService


def retain_lots(db, account, xml, *, existing_only=False):
    root = ET.fromstring(xml)
    known_keys = {row[0] for row in db.conn.execute('''SELECT source_id FROM broker_source_records
        WHERE account_id=? AND source='flex_closed_lot' ''', (account,))} if existing_only else set()
    associations = {}
    for section in root.iter():
        if section.tag.rsplit('}', 1)[-1] != 'Trades':
            continue
        execution, lots = None, []

        def associate():
            if execution is None or not lots:
                return
            key, _revision = execution_identity({'ib_exec_id': execution.get('ibExecID'),
                                                 'flex_trade_id': execution.get('tradeID')})
            try:
                quantities = [float(lot.get('quantity')) for lot in lots]
                quantity = abs(float(execution.get('quantity')))
                valid = (key and execution.get('accountId') == account
                    and execution.get('buySell') in {'BUY', 'SELL'}
                    and execution.get('levelOfDetail') in {'EXECUTION', 'EXECUTIONS'}
                    and math.isfinite(quantity) and quantity > 0
                    and all(math.isfinite(qty) and
                            (qty > 0 if execution.get('buySell') == 'SELL' else qty < 0)
                            for qty in quantities)
                    and abs(abs(sum(quantities)) - quantity) < 1e-8
                    and all(lot.get('accountId') == account
                            and lot.get('conid') == execution.get('conid')
                            and lot.get('dateTime') == execution.get('dateTime')
                            and lot.get('currency') == execution.get('currency') for lot in lots))
            except (TypeError, ValueError):
                valid = False
            if valid:
                associations.update((id(lot), key) for lot in lots)

        for node in section:
            tag = node.tag.rsplit('}', 1)[-1]
            if tag == 'Trade':
                associate()
                execution, lots = node, []
            elif tag == 'Lot' and node.get('levelOfDetail') == 'CLOSED_LOT':
                lots.append(node)
            else:
                associate()
                execution, lots = None, []
        associate()
    for node in root.iter():
        tag = node.tag.rsplit('}', 1)[-1]
        if not ((tag == 'Lot' and node.get('levelOfDetail') == 'CLOSED_LOT') or
                (tag == 'OpenPosition' and node.get('levelOfDetail') in {'LOT', 'SUMMARY'})):
            continue
        if node.get('accountId') != account:
            raise ValueError('Broker lot account mismatch')
        raw = json.dumps(node.attrib, sort_keys=True)
        source = 'flex_closed_lot' if tag == 'Lot' else 'flex_open_lot'
        legacy_key = sha256(raw.encode()).hexdigest()
        execution_key = associations.get(id(node), '')
        key = sha256((execution_key + ':' + raw).encode()).hexdigest() if execution_key else legacy_key
        if existing_only and (not execution_key or not {legacy_key, key}.intersection(known_keys)):
            continue
        db.conn.execute('''INSERT INTO broker_source_records
            (account_id,source,source_id,execution_key,status,issue,raw_json)
            VALUES (?,?,?,?,'pending','Waiting for complete execution history',?)
            ON CONFLICT(account_id,source,source_id) DO UPDATE SET execution_key=excluded.execution_key''',
            (account, source, key, execution_key, raw))
        if execution_key:
            db.conn.execute('''DELETE FROM broker_source_records WHERE account_id=? AND source=?
                AND source_id=? AND execution_key='' ''', (account, source, legacy_key))


def restore_saved_lot_links(db, account, query_id):
    """Upgrade committed lot evidence using only this database's saved reports."""
    if not db.conn.execute("""SELECT 1 FROM broker_source_records WHERE account_id=?
            AND source='flex_closed_lot' AND execution_key='' LIMIT 1""", (account,)).fetchone():
        return
    key = sha256(f'{account}:{query_id}'.encode()).hexdigest()[:24]
    for path in sorted((db.path.parent / 'flex-reports').glob(f'{key}-*.xml')):
        if path.name.endswith('.invalid.xml'):
            continue
        retain_lots(db, account, path.read_bytes(), existing_only=True)


def reconcile_transferred_lots(db, account):
    """Split synthetic transfer rows only when broker lots prove quantity AND basis."""
    conn = db.conn
    snapshots = defaultdict(list)
    for row in conn.execute("SELECT raw_json FROM broker_source_records WHERE account_id=? AND source='flex_open_lot'", (account,)):
        raw = json.loads(row[0])
        if (raw.get('levelOfDetail') == 'LOT' and raw.get('assetCategory') == 'STK'
                and not raw.get('originatingTransactionID') and raw.get('openDateTime')):
            snapshots[raw.get('conid'), raw.get('reportDate')].append(raw)
    changed = set()
    for event in conn.execute("""SELECT trade_row_id,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex_event' AND execution_key LIKE 'L:%'""", (account,)).fetchall():
        raw = json.loads(event['raw_json'])
        trade = conn.execute('SELECT * FROM trades WHERE id=?', (event['trade_row_id'],)).fetchone()
        if not trade or trade['source'] != 'IBKR legacy transfer' or raw.get('assetCategory') != 'STK':
            continue
        if conn.execute("SELECT 1 FROM trades WHERE trade_id LIKE ? LIMIT 1", (trade['trade_id'] + ':LOT:%',)).fetchone():
            continue
        protected = conn.execute("""SELECT 1 FROM trade_matches WHERE origin NOT IN ('auto','auto_short')
            AND (buy_trade_id=? OR sell_trade_id=?) LIMIT 1""", (trade['id'], trade['id'])).fetchone()
        if protected:
            continue
        target_qty, target_cost = float(raw['quantity']), float(raw['cost'])
        chosen = None
        for (conid, day), lots in sorted(snapshots.items()):
            if conid != raw.get('conid') or day < trade['trade_date'].replace('-', ''):
                continue
            candidates = [lot for lot in lots if lot['openDateTime'][:8] <= trade['trade_date'].replace('-', '')
                          and lot.get('currency') == trade['currency']]
            candidates = [lot for lot in candidates if math.isfinite(float(lot['position']))
                          and float(lot['position']) > 0 and math.isfinite(float(lot['costBasisMoney']))
                          and float(lot['costBasisMoney']) >= 0]
            # Large or ambiguous sets are evidence for review, not a guessing exercise.
            if len(candidates) > 12:
                continue
            fits = []
            for size in range(1, len(candidates) + 1):
                for subset in combinations(candidates, size):
                    qty = sum(float(lot['position']) for lot in subset)
                    basis = sum(float(lot['costBasisMoney']) for lot in subset)
                    if abs(qty - target_qty) < 1e-8 and abs(basis - target_cost) < 0.005:
                        fits.append(subset)
            if len(fits) == 1:
                chosen = sorted(fits[0], key=lambda lot: (lot['openDateTime'], lot['costBasisMoney']))
                break
        if not chosen or len(chosen) < 2:
            continue
        conn.execute("DELETE FROM trade_matches WHERE origin IN ('auto','auto_short') AND (buy_trade_id=? OR sell_trade_id=?)",
                     (trade['id'], trade['id']))
        for index, lot in enumerate(chosen):
            qty = float(lot['position'])
            if not math.isfinite(qty) or qty <= 0:
                raise ValueError('Transferred broker lot has invalid quantity')
            price = float(lot['costBasisMoney']) / qty / trade['multiplier']
            note = f"Broker transferred tax lot acquired {lot['openDateTime']}; transfer date retained."
            if index == 0:
                conn.execute('UPDATE trades SET quantity=?,price=?,notes=? WHERE id=?', (qty, price, note, trade['id']))
            else:
                columns = ('portfolio_id','position_id','trade_id','trade_date','executed_at','display_ticker','ib_symbol',
                           'side','quantity','price','security_type','multiplier','currency','fx_to_usd','commission_usd','source','notes')
                values = dict(trade)
                values.update(trade_id=trade['trade_id'] + f':LOT:{index}', quantity=qty, price=price, notes=note)
                conn.execute(f"INSERT INTO trades ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                             tuple(values[key] for key in columns))
        changed.add(trade['position_id'])
    return changed


def check_open_lots(db, account, portfolio_id=1):
    """Compare latest broker snapshots; never invent allocations from a snapshot."""
    conn = db.conn
    latest = {}
    records = conn.execute("""SELECT source_id,raw_json FROM broker_source_records
        WHERE account_id=? AND source='flex_open_lot'""", (account,)).fetchall()
    for record in records:
        raw = json.loads(record['raw_json'])
        latest[raw.get('conid')] = max(latest.get(raw.get('conid'), ''), raw.get('reportDate', ''))
    latest_report = max(latest.values(), default='')
    by_position = defaultdict(list)
    for record in records:
        raw = json.loads(record['raw_json'])
        if raw.get('reportDate') != latest_report:
            conn.execute("""UPDATE broker_source_records SET status='superseded',issue=''
                WHERE account_id=? AND source='flex_open_lot' AND source_id=?""",
                (account, record['source_id']))
        elif raw.get('levelOfDetail') == 'SUMMARY':
            by_position[raw.get('conid')].append((record['source_id'], raw))
    trades = db.rows('trades', portfolio_id)
    remaining = MatchingService(db, portfolio_id).remaining()
    for conid, group in by_position.items():
        position = conn.execute('SELECT id FROM positions WHERE portfolio_id=? AND con_id=?', (portfolio_id, conid)).fetchone()
        for source_id, raw in group:
            issue = ''
            status = 'complete'
            rows = [t for t in trades if position and t['position_id'] == position[0]]
            report_day = raw['reportDate'][:8]
            if any(t['trade_date'].replace('-', '') > report_day for t in rows):
                conn.execute("""UPDATE broker_source_records SET status='superseded',issue=''
                    WHERE account_id=? AND source='flex_open_lot' AND source_id=?""",
                    (account, source_id))
                continue  # Snapshot precedes current ledger; it cannot validate today's holdings.
            quantity = sum(t['quantity'] * (1 if t['side'] == 'BUY' else -1) for t in rows)
            reported_qty, reported_basis = float(raw['position']), float(raw['costBasisMoney'])
            if not math.isfinite(reported_qty) or not math.isfinite(reported_basis):
                issue = 'Broker snapshot has invalid quantity or basis'
            elif abs(quantity - reported_qty) > 1e-8:
                opened = account_open_day(db, account)
                through = date.fromisoformat(f'{report_day[:4]}-{report_day[4:6]}-{report_day[6:8]}')
                complete = opened and not missing_windows(db, account, 'trades', opened, through, 14, portfolio_id)
                if complete:
                    issue = 'Broker open quantity differs from imported history'
                else:
                    status = 'pending'
                    issue = 'Waiting for earlier execution history to verify broker open quantity'
            elif reported_qty > 0:
                open_buys = [t for t in rows if t['side'] == 'BUY' and remaining[t['id']] > 1e-8]
                if any(trade_cost_usd(t) is None for t in open_buys):
                    status = 'pending'
                    issue = 'Waiting for purchase transaction tax or commission evidence'
                else:
                    basis = sum(remaining[t['id']] * (t['price'] * t['multiplier'] +
                        trade_cost_usd(t) / t['quantity'] / t['fx_to_usd']) for t in open_buys
                        if t['fx_to_usd'] and t['quantity'])
                    if abs(basis - reported_basis) > 0.02:
                        issue = 'Broker open basis differs; closed lots or transferred basis need review'
            conn.execute("""UPDATE broker_source_records SET status=?,issue=?
                WHERE account_id=? AND source='flex_open_lot' AND source_id=?""",
                ('pending' if status == 'pending' else 'review' if issue else 'complete', issue, account, source_id))


def _transferred_purchase_ids(conn, account, sell, raw):
    """Resolve a legacy transfer only with matching broker acquisition/basis evidence."""
    opened = raw.get('openDateTime')
    if not opened or raw.get('assetCategory') != 'STK':
        return set()
    qty, cost = float(raw['quantity']), float(raw['cost'])
    if not math.isfinite(qty) or qty <= 0 or not math.isfinite(cost):
        return set()
    proof_days = []
    for row in conn.execute("""SELECT raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex_open_lot'""", (account,)):
        lot = json.loads(row[0])
        if (lot.get('levelOfDetail') != 'LOT' or lot.get('conid') != raw.get('conid')
                or lot.get('currency') != raw.get('currency') or lot.get('openDateTime') != opened
                or lot.get('originatingTransactionID')):
            continue
        position, basis = float(lot['position']), float(lot['costBasisMoney'])
        if (math.isfinite(position) and position > 0 and math.isfinite(basis)
                and abs(cost - qty * basis / position) < 0.02):
            proof_days.append(lot.get('reportDate', ''))
    candidates = set()
    # Statement recovery retains the exact broker acquisition and basis proof,
    # including lots sold before the earliest surviving open-position snapshot.
    for row in conn.execute('''SELECT t.*,r.raw_json FROM trades t
            JOIN broker_source_records r ON r.trade_row_id=t.id
            WHERE t.portfolio_id=? AND t.position_id=? AND t.side='BUY'
              AND r.account_id=? AND r.source='activity_statement_transfer_lot'
              AND r.status='complete' ''', (sell['portfolio_id'], sell['position_id'], account)):
        proof = json.loads(row['raw_json'])
        if (proof.get('acquired') == opened and str(proof.get('con_id')) == str(raw.get('conid'))
                and row['currency'] == raw.get('currency') == proof.get('currency')
                and row['trade_date'] <= sell['trade_date'] and row['quantity'] >= qty
                and abs(cost - qty * row['price'] * row['multiplier']) < 0.02):
            candidates.add(row['id'])
    for buy in conn.execute("""SELECT * FROM trades WHERE portfolio_id=? AND position_id=?
            AND side='BUY' AND source='IBKR legacy transfer' AND currency=?""",
            (sell['portfolio_id'], sell['position_id'], raw.get('currency'))):
        transferred = buy['trade_date'].replace('-', '')
        if (buy['quantity'] >= qty and opened[:8] <= transferred
                and any(transferred <= day < raw.get('dateTime', '')[:8] for day in proof_days)
                and abs(cost - qty * buy['price'] * buy['multiplier']) < 0.02):
            candidates.add(buy['id'])
    return candidates


def _current_execution_sources(conn, account, portfolio_id):
    """Only unchanged source records from the canonical execution revision qualify."""
    sources = defaultdict(list)
    records = conn.execute('''SELECT t.*,p.con_id,p.multiplier AS contract_multiplier,
        p.security_type AS contract_type,e.execution_key AS current_key,
        e.revision AS current_revision,r.execution_key AS source_key,r.source AS source_kind,r.source_id,r.raw_json
        FROM trades t JOIN positions p ON p.id=t.position_id
        JOIN broker_executions e ON e.trade_row_id=t.id AND e.account_id=?
        JOIN broker_source_records r ON r.trade_row_id=t.id AND r.account_id=e.account_id
        WHERE t.portfolio_id=? AND r.source IN ('flex','activity_statement')
          AND r.status NOT IN ('review','superseded')''', (account, portfolio_id))
    for row in records:
        try:
            raw = json.loads(row['raw_json'])
            if row['source_kind'] == 'activity_statement':
                # Older CSV recovery stores its immutable F:AS identity on the
                # linked source record, not inside the raw statement fields.
                if (not row['source_id'].startswith('AS:') or row['current_revision'] != 0
                        or row['current_key'] != 'F:' + row['source_id']
                        or raw.get('source') != 'IBKR Activity Statement CSV'
                        or raw.get('account', account) != account
                        or row['security_type'] != row['contract_type']
                        or row['multiplier'] != row['contract_multiplier']):
                    continue
                day = _day(raw.get('trade_date'))
                clock = _clock(raw.get('time'))
                raw = dict(raw, accountId=account, tradeID=row['source_id'], ibExecID='',
                           conid=str(raw.get('con_id')), buySell=raw.get('side'),
                           assetCategory=row['security_type'], multiplier=row['multiplier'],
                           tradePrice=raw.get('price'), tradeDate=day.replace('-', ''),
                           dateTime=day.replace('-', '') + (';' + clock if clock else ''),
                           ibCommission=raw.get('signed_commission'),
                           ibCommissionCurrency=raw.get('commission_currency'), cost=raw.get('basis'))
            key, revision = execution_identity({'ib_exec_id': raw.get('ibExecID'),
                                               'flex_trade_id': raw.get('tradeID')})
            if (key != row['current_key'] or key != row['source_key']
                    or revision != row['current_revision'] or raw.get('accountId') != account
                    or raw.get('buySell') != row['side'] or int(raw.get('conid')) != row['con_id']
                    or raw.get('currency') != row['currency']
                    or raw.get('assetCategory') != row['security_type']
                    or _day(raw.get('tradeDate') or raw.get('dateTime')) != row['trade_date']
                    or _clock(raw.get('tradeTime') or raw.get('dateTime')) != row['executed_at']):
                continue
            numbers = (abs(float(raw['quantity'])), float(raw['tradePrice']),
                       _effective_multiplier(row['security_type'], raw.get('multiplier') or
                                             ('1' if row['security_type'] == 'STK' else None)))
            if (any(value is None or not math.isfinite(value) for value in numbers)
                    or abs(numbers[0] - row['quantity']) > 1e-8
                    or abs(numbers[1] - row['price']) > 1e-8
                    or abs(numbers[2] - row['multiplier']) > 1e-8):
                continue
            if raw.get('proceeds') not in (None, ''):
                proceeds = float(raw['proceeds'])
                gross = row['quantity'] * row['price'] * row['multiplier']
                if (not math.isfinite(proceeds) or
                        abs(proceeds - gross * (1 if row['side'] == 'SELL' else -1)) > 0.02):
                    continue
            sources[row['id']].append(raw)
        except (ValueError, TypeError, KeyError):
            continue
    return sources


def _verified_raw_costs(trade, sources):
    """Prove local fees/tax and unadjusted cash/basis for the narrow exception."""
    for raw in sources:
        try:
            commission_currency = raw['ibCommissionCurrency']
            commission = -float(raw['ibCommission'])
            if commission_currency == 'USD':
                commission /= trade['fx_to_usd']
            elif commission_currency != trade['currency']:
                continue
            tax = -float(raw['taxes'])
            proceeds, cost = float(raw['proceeds']), float(raw['cost'])
            gross = trade['quantity'] * trade['price'] * trade['multiplier']
            signed_gross = gross * (-1 if trade['side'] == 'BUY' else 1)
            expected_cost = (-proceeds + commission + tax)
            if (not all(math.isfinite(v) for v in (commission, tax, proceeds, cost))
                    or abs(commission * trade['fx_to_usd'] - trade['commission_usd']) > 1e-8
                    or trade['tax_usd'] is None
                    or abs(tax * trade['fx_to_usd'] - trade['tax_usd']) > 1e-8
                    or abs(proceeds - signed_gross) > 1e-5
                    or abs(cost - expected_cost) > 1e-5):
                continue
            return commission, tax
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return None


def _split_purchase(conn, account, opening, closing):
    """Prove one complete 2-for-1 event and its zero-basis ledger entitlement."""
    groups = defaultdict(list)
    for row in conn.execute('''SELECT trade_row_id,raw_json,status,execution_key
        FROM broker_source_records WHERE account_id=? AND source='flex_event'
          AND execution_key LIKE 'CA:FS:%' ''', (account,)):
        raw = json.loads(row['raw_json'])
        if (str(raw.get('conid')) == str(conn.execute('SELECT con_id FROM positions WHERE id=?',
                (opening['position_id'],)).fetchone()[0])
                and opening['trade_date'] < _day(raw.get('dateTime')) <= closing['trade_date']):
            groups[row['trade_row_id']].append((row, raw))
    if not groups:
        return None
    if len(groups) != 1:
        raise ValueError('Broker closed lot requires review of multiple split events')
    split_id, events = next(iter(groups.items()))
    split = conn.execute('SELECT * FROM trades WHERE id=?', (split_id,)).fetchone()
    if (not split or split['portfolio_id'] != opening['portfolio_id']
            or split['position_id'] != opening['position_id'] or split['side'] != 'BUY'
            or split['source'] != 'IBKR corporate action' or split['price'] != 0
            or split['multiplier'] != opening['multiplier'] or split['currency'] != opening['currency']
            or trade_cost_usd(dict(split)) != 0
            or not MatchingService._precedes(dict(split), dict(closing))):
        raise ValueError('Broker split ledger proof is incomplete')
    quantity = 0.0
    for record, raw in events:
        match = re.search(r'\bSPLIT\s+(\d+)\s+FOR\s+(\d+)\b', raw.get('actionDescription', ''), re.I)
        if (record['status'] != 'complete' or raw.get('accountId') != account
                or raw.get('type') != 'FS' or raw.get('assetCategory') != 'STK'
                or raw.get('currency') != split['currency']
                or _day(raw.get('dateTime')) != split['trade_date']
                or not match or int(match[1]) != 2 or int(match[2]) != 1):
            raise ValueError('Broker split event proof is incomplete')
        event_qty = float(raw['quantity'])
        if not math.isfinite(event_qty):
            raise ValueError('Broker split event quantity is invalid')
        quantity += event_qty
    before = conn.execute('''SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END),0)
        FROM trades WHERE position_id=? AND trade_date<?''',
        (split['position_id'], split['trade_date'])).fetchone()[0]
    if quantity <= 0 or abs(quantity - split['quantity']) > 1e-8 or abs(quantity - before) > 1e-8:
        raise ValueError('Broker split shares differ from pre-split entitlement')
    # Earlier sales consume original-share entitlement. Protected later links
    # consume it too; automatic guesses will be removed atomically below.
    consumed = conn.execute('''SELECT COALESCE(SUM(m.matched_qty),0) FROM trade_matches m
        JOIN trades s ON s.id=m.sell_trade_id WHERE m.buy_trade_id=? AND s.id<>?
        AND (s.trade_date<? OR m.origin NOT IN ('auto','auto_short'))''',
        (opening['id'], closing['id'], split['trade_date'])).fetchone()[0]
    return split, opening['quantity'] - consumed


def reconcile_closed_lots(db, account, portfolio_id=1):
    conn = db.conn
    transactions, closings = defaultdict(set), defaultdict(set)
    executions = {row['execution_key']: row['trade_row_id'] for row in conn.execute('''
        SELECT execution_key,trade_row_id FROM broker_executions WHERE account_id=?''', (account,))}
    sources = _current_execution_sources(conn, account, portfolio_id)
    for trade_id, raws in sources.items():
        for raw in raws:
            if raw.get('transactionID'):
                transactions[raw['transactionID']].add(trade_id)
            closings[raw.get('conid'), raw.get('dateTime'), raw.get('buySell')].add(trade_id)
    groups = defaultdict(list)
    for row in conn.execute("""SELECT source_id,execution_key,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex_closed_lot'""", (account,)):
        raw = json.loads(row['raw_json'])
        key = row['execution_key'] or (raw.get('conid'), raw.get('dateTime'))
        groups[key].append((row['source_id'], raw))
    changed = set()
    matcher = MatchingService(db, portfolio_id)
    # Stable chronological order gives earlier closing groups first claim on
    # capacities while replaying the same evidence produces the same links.
    for key, lots in sorted(groups.items(), key=lambda item: (item[1][0][1].get('dateTime', ''), str(item[0]))):
        issue = ''
        status = 'complete'
        sell_id = None
        diagnostics = set()
        try:
            short = all(float(raw['quantity']) < 0 for _, raw in lots)
            closing_side, opening_side = ('BUY', 'SELL') if short else ('SELL', 'BUY')
            if isinstance(key, str):
                sell_id = executions.get(key)
                if sell_id is None:
                    status = 'pending'
                    raise ValueError('Waiting for the closed-lot closing execution')
            else:
                candidates = closings[key[0], key[1], closing_side]
                if len(candidates) != 1:
                    raise ValueError('Closed lots require one unambiguous closing execution')
                sell_id = next(iter(candidates))
            sell = conn.execute('SELECT * FROM trades WHERE id=? AND portfolio_id=?',
                                (sell_id, portfolio_id)).fetchone()
            if sell is None:
                raise ValueError('Closed lot closing trade is outside the selected portfolio')
            if sell['side'] != closing_side or sell_id not in sources:
                raise ValueError('Closed lot closing execution provenance differs from source')
            desired = defaultdict(float)
            for _source_id, raw in lots:
                ids = {trade_id for trade_id in transactions[raw.get('transactionID')]
                       if conn.execute('SELECT side,position_id FROM trades WHERE id=?', (trade_id,)).fetchone()[:]
                       == (opening_side, sell['position_id'])}
                if not ids:
                    # Opening clock and exact contract can identify an execution
                    # even when the lot omits a transaction identifier.
                    opened = raw.get('openDateTime')
                    ids = {trade_id for trade_id, raws in sources.items() if any(
                        evidence.get('buySell') == opening_side
                        and evidence.get('conid') == raw.get('conid')
                        and evidence.get('currency') == raw.get('currency')
                        and evidence.get('assetCategory') == raw.get('assetCategory')
                        and opened and (evidence.get('dateTime') == opened if ';' in opened
                                        else evidence.get('dateTime', '')[:8] == opened[:8])
                        for evidence in raws)}
                if not ids and not short:
                    ids = _transferred_purchase_ids(conn, account, sell, raw)
                if not ids:
                    status = 'pending'
                    raise ValueError('Waiting for earlier opening trades referenced by broker closed lots')
                if len(ids) != 1:
                    raise ValueError('Closed lot opening history is missing or ambiguous')
                buy_id = next(iter(ids))
                buy = conn.execute('SELECT * FROM trades WHERE id=?', (buy_id,)).fetchone()
                signed_qty, cost = float(raw['quantity']), float(raw['cost'])
                qty = abs(signed_qty)
                if (not math.isfinite(qty) or qty <= 0 or not math.isfinite(cost)
                        or (signed_qty < 0) != short):
                    raise ValueError('Invalid broker closed-lot quantity or basis')
                con_id = conn.execute('SELECT con_id FROM positions WHERE id=?', (buy['position_id'],)).fetchone()[0]
                if (buy['side'] != opening_side or buy['position_id'] != sell['position_id']
                        or buy['portfolio_id'] != portfolio_id or str(con_id) != str(raw.get('conid'))
                        or buy['currency'] != raw.get('currency') or buy['currency'] != sell['currency']
                        or buy['security_type'] != sell['security_type'] or buy['multiplier'] <= 0
                        or buy['multiplier'] != sell['multiplier']
                        or not math.isfinite(buy['fx_to_usd']) or buy['fx_to_usd'] <= 0
                        or (raw.get('assetCategory') and raw['assetCategory'] != buy['security_type'])
                        or (raw.get('multiplier') and _effective_multiplier(buy['security_type'],
                            raw['multiplier']) != buy['multiplier'])
                        or not MatchingService._precedes(dict(buy), dict(sell))
                        or (raw.get('tradeDate') and _day(raw['tradeDate']) != sell['trade_date'])
                        or not any(evidence.get('dateTime') == raw.get('dateTime')
                                   for evidence in sources[sell_id])):
                    raise ValueError('Closed lot instrument or currency differs from purchase')
                state = conn.execute('SELECT status FROM broker_executions WHERE trade_row_id=?', (buy_id,)).fetchone()
                fees = trade_cost_usd(dict(buy, commission_complete=not state or 'commission' not in state[0]))
                if fees is None:
                    status = 'pending'
                    raise ValueError('Waiting for purchase transaction tax or commission evidence')
                split = _split_purchase(conn, account, buy, sell) if not short else None
                opening_qty = qty / 2 if split else qty
                expected = opening_qty * (buy['price'] * buy['multiplier'] * (-1 if short else 1) +
                    fees / buy['quantity'] / buy['fx_to_usd'])
                if short:
                    proof = _verified_raw_costs(buy, sources.get(buy_id, []))
                    if proof is None:
                        raise ValueError('Broker short lot requires verified opening proceeds and costs')
                if abs(expected - cost) > 0.02:
                    proof = (_verified_raw_costs(buy, sources.get(buy_id, []))
                             if not short and not split and buy['security_type'] == 'STK' else None)
                    # Some observed stock closed lots omit commission. Taxes
                    # remain included and every raw economic value must agree.
                    if (proof is None or abs(expected - cost - proof[0] * qty / buy['quantity']) > 1e-5):
                        raise ValueError('Broker closed-lot basis differs from source execution')
                    diagnostics.add('Observed commission-excluded closed-lot basis; execution fees and taxes retained')
                if split:
                    split_trade, entitlement = split
                    if desired[buy_id] + opening_qty > entitlement + 1e-8:
                        raise ValueError('Broker split lot exceeds original-share entitlement')
                    desired[split_trade['id']] += opening_qty
                    diagnostics.add('Validated 2-for-1 split allocation; original execution basis and costs retained')
                desired[buy_id] += opening_qty
            if abs(sum(desired.values()) - sell['quantity']) > 1e-8:
                raise ValueError('Closed-lot quantities do not cover the complete closing trade')
            endpoint = 'buy_trade_id' if short else 'sell_trade_id'
            opening_column = 'sell_trade_id' if short else 'buy_trade_id'
            origin = 'broker_closed_lot_short' if short else 'broker_closed_lot'
            old = conn.execute(f'SELECT {opening_column} AS opening_id,matched_qty,origin FROM trade_matches WHERE {endpoint}=?',
                               (sell_id,)).fetchall()
            actual = defaultdict(float)
            for row in old:
                actual[row['opening_id']] += row['matched_qty']
            identical = (set(actual) == set(desired)
                         and all(abs(actual[k] - desired[k]) <= 1e-8 for k in actual))
            for opening_id, allocation in desired.items():
                protected_used = conn.execute(f'''SELECT COALESCE(SUM(matched_qty),0)
                    FROM trade_matches WHERE (buy_trade_id=? OR sell_trade_id=?)
                      AND {endpoint}<>? AND origin NOT IN ('auto','auto_short')''',
                    (opening_id, opening_id, sell_id)).fetchone()[0]
                capacity = conn.execute('SELECT quantity FROM trades WHERE id=?', (opening_id,)).fetchone()[0]
                if allocation + protected_used > capacity + 1e-8:
                    raise ValueError('Broker lot exceeds remaining protected opening quantity')
            if any(row['origin'] not in {'auto', 'auto_short', origin} for row in old):
                if not identical:
                    raise ValueError('Broker lots conflict with reviewed or manual allocations')
            elif not identical or any(row['origin'] != origin for row in old):
                # Clearing automatic links frees purchases consumed by other
                # inferred sales; reviewed links remain protected by match().
                conn.execute('SAVEPOINT closed_lots')
                try:
                    conn.execute("""DELETE FROM trade_matches WHERE origin IN ('auto','auto_short')
                        AND sell_trade_id IN (SELECT id FROM trades WHERE position_id=?)""", (sell['position_id'],))
                    conn.execute(f'DELETE FROM trade_matches WHERE {endpoint}=? AND origin=?', (sell_id, origin))
                    if short:
                        matcher.match_closed_short(sell_id, sorted(desired.items()), transaction=False)
                    else:
                        matcher.match(sell_id, sorted(desired.items()), origin=origin, transaction=False)
                except ValueError:
                    conn.execute('ROLLBACK TO closed_lots')
                    conn.execute('RELEASE closed_lots')
                    raise
                conn.execute('RELEASE closed_lots')
                changed.add(sell['position_id'])
        except (ValueError, KeyError, TypeError) as exc:
            issue = str(exc)
        for source_id, _raw in lots:
            conn.execute('''UPDATE broker_source_records SET status=?,issue=?,trade_row_id=?
                WHERE account_id=? AND source='flex_closed_lot' AND source_id=?''',
                ('pending' if status == 'pending' else 'review' if issue else 'complete',
                 issue or '; '.join(sorted(diagnostics)), sell_id, account, source_id))
    return changed
