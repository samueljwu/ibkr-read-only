"""Retain broker lot evidence and apply only exact, validated closed-lot links."""
from app.finance.trade_costs import trade_cost_usd

from collections import defaultdict
from hashlib import sha256
import json
import math
from itertools import combinations
import xml.etree.ElementTree as ET

from app.broker.broker_sync import execution_identity
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
                    and execution.get('buySell') == 'SELL'
                    and execution.get('levelOfDetail') in {'EXECUTION', 'EXECUTIONS'}
                    and math.isfinite(quantity) and quantity > 0
                    and all(math.isfinite(qty) and qty > 0 for qty in quantities)
                    and abs(sum(quantities) - quantity) < 1e-8
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


def _execution_purchase_ids(conn, account, raw):
    """Find one exact broker execution when a Closed Lot omits transactionID."""
    try:
        opened = str(raw.get('openDateTime') or '').strip()
        if len(opened) < 8 or not opened[:8].isdigit():
            return set()
        day = date(int(opened[:4]), int(opened[4:6]), int(opened[6:8])).isoformat()
        qty, con_id = float(raw.get('quantity')), int(raw.get('conid'))
        if not math.isfinite(qty) or qty <= 0:
            return set()
    except (TypeError, ValueError):
        return set()
    sql = '''SELECT DISTINCT t.id FROM trades t
        JOIN positions p ON p.id=t.position_id
        JOIN broker_executions e ON e.trade_row_id=t.id AND e.account_id=?
        WHERE p.con_id=? AND p.security_type=? AND p.currency=? AND t.side='BUY'
          AND t.trade_date=? AND t.quantity>=?'''
    params = [account, con_id, str(raw.get('assetCategory') or '').upper(),
              str(raw.get('currency') or '').upper(), day, qty]
    if ';' in opened:
        clock = ''.join(char for char in opened.split(';', 1)[1] if char.isdigit())[:6]
        if clock:
            sql += ' AND t.executed_at=?'
            params.append(clock.ljust(6, '0'))
    return {row[0] for row in conn.execute(sql, params)}


def reconcile_closed_lots(db, account, portfolio_id=1):
    conn = db.conn
    transactions, sales = defaultdict(set), defaultdict(set)
    executions = {row['execution_key']: row['trade_row_id'] for row in conn.execute('''
        SELECT execution_key,trade_row_id FROM broker_executions WHERE account_id=?''', (account,))}
    for row in conn.execute("""SELECT trade_row_id,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex' AND trade_row_id IS NOT NULL""", (account,)):
        raw = json.loads(row['raw_json'])
        if raw.get('transactionID'):
            transactions[raw['transactionID']].add(row['trade_row_id'])
        if raw.get('buySell') == 'SELL':
            sales[raw.get('conid'), raw.get('dateTime')].add(row['trade_row_id'])
    groups = defaultdict(list)
    for row in conn.execute("""SELECT source_id,execution_key,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex_closed_lot'""", (account,)):
        raw = json.loads(row['raw_json'])
        key = row['execution_key'] or (raw.get('conid'), raw.get('dateTime'))
        groups[key].append((row['source_id'], raw))
    changed = set()
    matcher = MatchingService(db, portfolio_id)
    for key, lots in groups.items():
        issue = ''
        status = 'complete'
        sell_id = None
        try:
            if isinstance(key, str):
                sell_id = executions.get(key)
                if sell_id is None:
                    status = 'pending'
                    raise ValueError('Waiting for the closed-lot sale execution')
            else:
                if len(sales[key]) != 1:
                    raise ValueError('Closed lots require one unambiguous sale execution')
                sell_id = next(iter(sales[key]))
            sell = conn.execute('SELECT * FROM trades WHERE id=? AND portfolio_id=?',
                                (sell_id, portfolio_id)).fetchone()
            if sell is None:
                raise ValueError('Closed lot sale is outside the selected portfolio')
            desired = defaultdict(float)
            for _source_id, raw in lots:
                ids = transactions[raw.get('transactionID')]
                if not ids:
                    ids = _execution_purchase_ids(conn, account, raw)
                if not ids:
                    ids = _transferred_purchase_ids(conn, account, sell, raw)
                if not ids:
                    status = 'pending'
                    raise ValueError('Waiting for earlier purchases referenced by broker closed lots')
                if len(ids) != 1:
                    raise ValueError('Closed lot purchase history is missing or ambiguous')
                buy_id = next(iter(ids))
                buy = conn.execute('SELECT * FROM trades WHERE id=?', (buy_id,)).fetchone()
                qty, cost = float(raw['quantity']), float(raw['cost'])
                if not math.isfinite(qty) or qty <= 0 or not math.isfinite(cost):
                    raise ValueError('Invalid broker closed-lot quantity or basis')
                if (sell['side'] != 'SELL' or buy['side'] != 'BUY' or buy['position_id'] != sell['position_id'] or
                        buy['currency'] != raw.get('currency') or not buy['fx_to_usd']):
                    raise ValueError('Closed lot instrument or currency differs from purchase')
                state = conn.execute('SELECT status FROM broker_executions WHERE trade_row_id=?', (buy_id,)).fetchone()
                fees = trade_cost_usd(dict(buy, commission_complete=not state or 'commission' not in state[0]))
                if fees is None:
                    status = 'pending'
                    raise ValueError('Waiting for purchase transaction tax or commission evidence')
                expected = qty * (buy['price'] * buy['multiplier'] +
                    fees / buy['quantity'] / buy['fx_to_usd'])
                if abs(expected - cost) > 0.02:
                    raise ValueError('Broker closed-lot basis differs from source execution')
                desired[buy_id] += qty
            if abs(sum(desired.values()) - sell['quantity']) > 1e-8:
                raise ValueError('Closed-lot quantities do not cover the complete sale')
            old = conn.execute('SELECT buy_trade_id,matched_qty,origin FROM trade_matches WHERE sell_trade_id=?',
                               (sell_id,)).fetchall()
            if any(row['origin'] not in {'auto', 'broker_closed_lot'} for row in old):
                actual = defaultdict(float)
                for row in old:
                    actual[row['buy_trade_id']] += row['matched_qty']
                if set(actual) != set(desired) or any(abs(actual[k] - desired[k]) > 1e-8 for k in actual):
                    raise ValueError('Broker lots conflict with reviewed or manual allocations')
            else:
                # Clearing automatic links frees purchases consumed by other
                # inferred sales; reviewed links remain protected by match().
                conn.execute('SAVEPOINT closed_lots')
                try:
                    conn.execute("""DELETE FROM trade_matches WHERE origin IN ('auto','auto_short')
                        AND sell_trade_id IN (SELECT id FROM trades WHERE position_id=?)""", (sell['position_id'],))
                    conn.execute("DELETE FROM trade_matches WHERE sell_trade_id=? AND origin='broker_closed_lot'", (sell_id,))
                    matcher.match(sell_id, sorted(desired.items()), origin='broker_closed_lot', transaction=False)
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
                ('pending' if status == 'pending' else 'review' if issue else 'complete', issue, sell_id, account, source_id))
    return changed
