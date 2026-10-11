"""Conservative recovery of pre-Flex stock transfers from statement evidence."""
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json


class MissingTransferEvidence(ValueError):
    """A valid transfer can be saved pending without inventing an opening."""


def section_rows(rows, section):
    columns = None
    for row in rows:
        if len(row) < 2 or row[0] != section:
            continue
        if row[1] == 'Header':
            columns = [name.strip().casefold() for name in row[2:]]
        elif row[1] == 'Data':
            if columns is None:
                raise ValueError(f'{section} is missing its header')
            yield dict(zip(columns, (value.strip() for value in row[2:])))


def stock_instruments(rows):
    """Use the report's contract IDs; a ticker alone never creates a contract."""
    from app.imports.activity_statement_trades import _number
    result = {}
    for row in section_rows(rows, 'Financial Instrument Information'):
        if row.get('asset category', '').casefold() != 'stocks':
            continue
        symbol = row.get('symbol', '')
        try:
            conid = int(row.get('conid', ''))
        except ValueError:
            raise ValueError(f'The statement is missing a valid IBKR contract ID for {symbol}') from None
        multiplier = _number(row.get('multiplier', ''), 'stock multiplier')
        if conid <= 0 or not symbol or multiplier != 1:
            raise ValueError(f'Invalid stock contract information for {symbol}')
        contract = dict(con_id=conid, ib_symbol=symbol, local_symbol='', multiplier=1,
                        security_type='STK')
        key = symbol.casefold()
        if key in result and result[key] != contract:
            raise ValueError(f'The statement has conflicting contract IDs for {symbol}')
        result[key] = contract
    return result


def validate_contract(db, contract, currency, portfolio_id):
    known = db.conn.execute('''SELECT security_type,currency,multiplier FROM positions
        WHERE portfolio_id=? AND con_id=?''', (portfolio_id, contract['con_id'])).fetchall()
    if len(known) > 1 or any(row['security_type'] != contract['security_type'] or
                           row['currency'] != currency or row['multiplier'] != contract['multiplier']
                           for row in known):
        raise ValueError(f"Statement contract conflicts with saved instrument: {contract.get('ib_symbol') or contract.get('symbol')}")


def _broker_opening_lots(db, account, conid, currency, acquired_before, last, quantity, basis, basis_ceiling=None):
    """Undo subsequent sales against ONE snapshot, never add repeated snapshots."""
    from app.imports.activity_statement_trades import _number
    evidence = []
    latest_report = ''
    for row in db.conn.execute('''SELECT source,source_id,raw_json FROM broker_source_records
            WHERE account_id=? AND source IN ('flex_open_lot','flex_closed_lot')''', (account,)):
        raw = json.loads(row['raw_json'])
        latest_report = max(latest_report, raw.get('reportDate', ''))
        if (str(raw.get('conid')) == str(conid) and raw.get('assetCategory') == 'STK'
                and raw.get('currency') == currency and raw.get('openDateTime')
                and raw['openDateTime'][:8] < acquired_before.replace('-', '')
                and not raw.get('originatingTransactionID')):
            evidence.append((row['source'], row['source_id'], raw))
    days = sorted({raw['reportDate'] for source, _, raw in evidence
                   if source == 'flex_open_lot' and raw.get('levelOfDetail') == 'LOT'
                   and raw.get('reportDate', '') > last.replace('-', '')})
    # Fully sold transfers can be proven from closed lots through the latest report.
    if latest_report > last.replace('-', ''):
        days = sorted(set(days + [latest_report]))
    conflicting_basis = False
    for snapshot in days:
        lots = defaultdict(lambda: dict(quantity=Decimal(0), basis=Decimal(0), evidence=[]))
        for source, source_id, raw in evidence:
            if source == 'flex_open_lot':
                if raw.get('levelOfDetail') != 'LOT' or raw.get('reportDate') != snapshot:
                    continue
                qty = _number(raw.get('position', ''), 'broker opening quantity')
                cost = _number(raw.get('costBasisMoney', ''), 'broker opening basis')
            else:
                if not last.replace('-', '') < raw.get('dateTime', '')[:8] <= snapshot:
                    continue
                qty = _number(raw.get('quantity', ''), 'broker closed quantity')
                cost = _number(raw.get('cost', ''), 'broker closed basis')
            if qty <= 0 or cost < 0:
                raise ValueError('Broker transfer lot quantity or basis is invalid')
            # Partial sales of one original lot retain the same acquisition and unit basis.
            key = (raw['openDateTime'], str((cost / qty).quantize(Decimal('0.000001'))))
            lot = lots[key]
            lot['quantity'] += qty
            lot['basis'] += cost
            lot['evidence'].append((source, source_id))
        if abs(sum((lot['quantity'] for lot in lots.values()), Decimal(0)) - quantity) < Decimal('0.00000001'):
            total_basis = sum((lot['basis'] for lot in lots.values()), Decimal(0))
            if ((basis is not None and abs(total_basis - basis) > Decimal('0.02'))
                    or (basis_ceiling is not None and total_basis - basis_ceiling > Decimal('0.02'))):
                conflicting_basis = True
                continue
            return [dict(acquired=key[0], quantity=str(lot['quantity']), basis=str(lot['basis']),
                         evidence=sorted(lot['evidence']), snapshot=snapshot)
                    for key, lot in sorted(lots.items())]
    if conflicting_basis:
        raise ValueError('Transferred stock basis conflicts with saved IBKR lots; transfer could not be verified')
    raise MissingTransferEvidence('Transferred stock lots could not be verified against saved IBKR history. '
                     'Run Import history with the Trades Flex query including Open Positions (Lots) '
                     'and Trades (Closed Lots), then choose this CSV again. No transfer basis was guessed.')


def _already_saved(db, account, portfolio_id, group):
    """Skip proven full recovery; reject partial overlap rather than top up by guess."""
    conid, dates = group['con_id'], {item['date'] for item in group['transfers']}
    for row in db.conn.execute('''SELECT source_id,status,raw_json FROM broker_source_records
            WHERE account_id=? AND source='activity_statement_transfer' ''', (account,)):
        prior = json.loads(row['raw_json'])
        if prior['con_id'] != conid or not dates.intersection(item['date'] for item in prior['transfers']):
            continue
        if row['source_id'] != group['source_id']:
            raise ValueError(f"{group['symbol']}: this transfer overlaps a previously imported statement; review it before importing")
        if prior.get('portfolio_id', portfolio_id) != portfolio_id:
            raise ValueError(f"{group['symbol']}: this transfer belongs to a different saved portfolio")
        prior_ceiling = prior.get('max_transfer_basis')
        if prior_ceiling is not None and (group.get('max_transfer_basis') is None
                or Decimal(prior_ceiling) < Decimal(group['max_transfer_basis'])):
            group['max_transfer_basis'] = prior_ceiling
            group['basis_ceiling_evidence'] = prior.get('basis_ceiling_evidence') or {
                key: prior[key] for key in ('max_transfer_basis', 'statement_rows', 'statement_hash', 'period')}
        if row['status'] == 'pending':
            if (prior.get('basis') is not None and group['basis'] is not None
                    and abs(Decimal(prior['basis']) - Decimal(group['basis'])) > Decimal('0.02')):
                raise ValueError(f"{group['symbol']}: saved pending transfer basis conflicts with this statement")
            if prior.get('basis') is not None and group['basis'] is None:
                group['basis'] = prior['basis']
                group['basis_evidence'] = prior.get('basis_evidence') or {
                    key: prior[key] for key in ('basis', 'statement_rows', 'statement_hash', 'period')}
            continue
        expected_basis = group['basis'] if group['basis'] is not None else prior['basis']
        trades = db.conn.execute('''SELECT t.*,p.con_id,r.raw_json FROM broker_source_records r
            JOIN trades t ON t.id=r.trade_row_id JOIN positions p ON p.id=t.position_id WHERE r.account_id=?
              AND r.source='activity_statement_transfer_lot' AND r.source_id LIKE ?
              AND t.portfolio_id=?''', (account, group['source_id'] + ':%', portfolio_id)).fetchall()
        if (not trades or any(t['con_id'] != conid or t['side'] != 'BUY' or t['currency'] != group['currency'] or
                              t['security_type'] != 'STK' or t['multiplier'] != 1 or t['commission_usd'] != 0
                              or abs(t['quantity'] - float(json.loads(t['raw_json'])['quantity'])) > 1e-8
                              or abs(t['quantity'] * t['price'] - float(json.loads(t['raw_json'])['basis'])) > 0.02
                              for t in trades)
                or abs(sum(t['quantity'] for t in trades) - float(group['quantity'])) > 1e-8
                or abs(sum(t['quantity'] * t['price'] + t['commission_usd'] for t in trades)
                       - float(expected_basis)) > 0.02):
            raise ValueError(f"{group['symbol']}: saved transfer holdings conflict with this statement")
        group['basis'] = expected_basis
        return True
    legacy = db.conn.execute('''SELECT t.* FROM trades t JOIN positions p ON p.id=t.position_id
        WHERE t.portfolio_id=? AND p.con_id=? AND t.source='IBKR legacy transfer' ''',
                             (portfolio_id, conid)).fetchall()
    if legacy:
        if group['basis'] is None:
            raise ValueError(f"{group['symbol']}: existing transferred holdings need review; no duplicate opening was added")
        if (any(t['trade_date'] not in dates or t['side'] != 'BUY' or t['currency'] != group['currency']
                or t['security_type'] != 'STK' or t['multiplier'] != 1 for t in legacy)
                or abs(sum(t['quantity'] for t in legacy) - float(group['quantity'])) > 1e-8
                or abs(sum(t['quantity'] * t['price'] * t['multiplier'] + t['commission_usd'] for t in legacy)
                       - float(group['basis'])) > 0.02):
            raise ValueError(f"{group['symbol']}: existing transferred holdings need review; no duplicate opening was added")
        return True
    return False


def preview_transfers(db, rows, executions, account, period, portfolio_id=1):
    from app.imports.activity_statement_trades import _number, _execution_time, _optional_number, purchase_basis
    instruments = stock_instruments(rows)
    groups = {}
    for row in section_rows(rows, 'Transfers'):
        if row.get('asset category', '').casefold() in {'total', ''}:
            continue
        if row.get('asset category', '').casefold() != 'stocks':
            continue  # Cash transfers belong to Cash history.
        symbol, currency = row.get('symbol', ''), row.get('currency', '').upper()
        if row.get('direction', '').casefold() != 'in' or row.get('type', '').upper() != 'ACATS' or currency != 'USD':
            raise ValueError(f'{symbol}: recovery supports inbound USD stock ACATS transfers only')
        contract = instruments.get(symbol.casefold())
        if not contract:
            raise ValueError(f'{symbol}: include Financial Instrument Information in the Activity Statement CSV')
        validate_contract(db, contract, currency, portfolio_id)
        day, _ = _execution_time(row.get('date', ''))
        if not period[0] <= day <= period[1]:
            raise ValueError('Stock transfer is outside the statement period')
        qty = _number(row.get('qty', ''), 'transfer quantity')
        if qty <= 0:
            raise ValueError('Inbound stock transfer quantity must be positive')
        group = groups.setdefault(contract['con_id'], dict(contract, symbol=symbol, currency=currency,
                                                          quantity=Decimal(0), transfers=[]))
        group['quantity'] += qty
        group['transfers'].append(dict(date=day, quantity=str(qty), type='ACATS',
                                      from_account=row.get('xfer account', ''), company=row.get('xfer company', '')))
    if not groups:
        return []
    from datetime import date
    if period[1] >= date(date.today().year - 4, 1, 1).isoformat():
        raise ValueError('Use an Activity Statement ending before the Flex history range for transfer recovery')
    prior_stock = [r for r in section_rows(rows, 'Net Asset Value')
                   if r.get('asset class', '').casefold() in {'stock', 'stocks'}]
    if len(prior_stock) != 1 or _number(prior_stock[0].get('prior total', ''), 'prior stock value') != 0:
        raise ValueError('To recover transfers, choose a statement starting at account opening, with zero prior stock value')
    for row in section_rows(rows, 'Mark-to-Market Performance Summary'):
        if (row.get('asset category', '').casefold() == 'stocks'
                and row.get('symbol', '').casefold() in {g['symbol'].casefold() for g in groups.values()}
                and _number(row.get('prior quantity', ''), 'prior stock quantity') != 0):
            raise ValueError('Transferred holdings require zero prior stock quantity in the statement')
    if any(r.get('asset category', '').casefold() not in {'total', ''}
           for r in section_rows(rows, 'Corporate Actions')):
        raise ValueError('This statement includes corporate actions; transferred holdings need review before import')
    endings = defaultdict(list)
    for row in section_rows(rows, 'Open Positions'):
        if row.get('asset category', '').casefold() == 'stocks' and row.get('datadiscriminator', '').casefold() == 'summary':
            endings[row.get('symbol', '').casefold()].append(row)
    for conid, group in groups.items():
        group['quantity'] = str(group['quantity'])
        group['transfers'].sort(key=lambda item: json.dumps(item, sort_keys=True))
        identity = json.dumps((account, conid, group['currency'], group['transfers']), sort_keys=True)
        group.update(source_id='AST:' + sha256(identity.encode()).hexdigest(), period=period,
                     portfolio_id=portfolio_id, basis=None, already_saved=False, status='verified',
                     max_transfer_basis=None,
                     statement_hash=sha256(json.dumps(rows).encode()).hexdigest(), statement_rows=rows)
        summary = endings[group['symbol'].casefold()]
        buys = [r for r in executions if r['con_id'] == conid]
        if len(summary) > 1:
            raise ValueError(f"{group['symbol']}: conflicting ending holdings summaries")
        eligible = bool(summary) and all(r['side'] == 'BUY' and r['currency'] == 'USD' for r in buys)
        if eligible:
            end = summary[0]
            if end.get('currency') != 'USD' or _number(end.get('mult', ''), 'stock multiplier') != 1:
                raise ValueError('Ending stock currency or multiplier conflicts with transfer')
            quantity = _number(end.get('quantity', ''), 'ending stock quantity')
            if abs(quantity - Decimal(group['quantity']) - sum((Decimal(r['raw']['quantity']) for r in buys), Decimal(0))) > Decimal('0.00000001'):
                raise ValueError(f"{group['symbol']}: transfer quantities do not reconcile to ending holdings")
            costs = []
            for record in buys:
                cost = purchase_basis(record, opening_context=True)
                if cost is None and record['taxes'] is not None:
                    cost = (Decimal(record['raw']['quantity']) * Decimal(record['raw']['price'])
                            + Decimal(record['raw']['commission']) - Decimal(record['raw']['taxes']))
                costs.append(cost)
            ending_basis = _optional_number(end.get('cost basis'), 'ending stock basis')
            if ending_basis is not None and ending_basis < 0:
                raise ValueError('Ending stock basis cannot be negative')
            minimum_purchases = sum((cost if cost is not None else
                Decimal(record['raw']['quantity']) * Decimal(record['raw']['price'])
                + Decimal(record['raw']['commission']) for record, cost in zip(buys, costs)), Decimal(0))
            group['minimum_purchase_cost'] = str(minimum_purchases)
            group['ending_basis'] = str(ending_basis) if ending_basis is not None else None
            group['max_transfer_basis'] = str(ending_basis - minimum_purchases) if ending_basis is not None else None
            if ending_basis is not None and all(cost is not None for cost in costs):
                basis = ending_basis - sum(costs, Decimal(0))
                if basis < 0:
                    raise ValueError('Transferred stock basis cannot be negative')
                group['basis'] = str(basis)
        group['already_saved'] = _already_saved(db, account, portfolio_id, group)
        if group['max_transfer_basis'] is not None and Decimal(group['max_transfer_basis']) < Decimal('-0.02'):
            raise ValueError(f"{group['symbol']}: purchase costs exceed ending stock basis; transfer basis cannot be negative")
        if (group['max_transfer_basis'] is not None and group['basis'] is not None
                and Decimal(group['basis']) - Decimal(group['max_transfer_basis']) > Decimal('0.02')):
            raise ValueError(f"{group['symbol']}: transfer basis and purchase costs conflict with ending stock basis")
        if group['already_saved']:
            group['status'] = 'already_saved'
        else:
            # Unreported earlier purchases could be the same holdings under another source.
            saved = db.conn.execute('''SELECT t.* FROM trades t JOIN positions p ON p.id=t.position_id
                WHERE t.portfolio_id=? AND p.con_id=? AND t.trade_date<=?''', (portfolio_id, conid, period[1])).fetchall()
            for trade in saved:
                if not any(trade['side'] == r['side'] and trade['trade_date'] == r['trade_date']
                           and abs(trade['quantity'] - r['quantity']) < 1e-8
                           and abs(trade['price'] - r['price']) < 1e-8 for r in buys):
                    raise ValueError(f"{group['symbol']}: other saved opening history needs review before adding transfers")
            try:
                if not eligible:
                    raise MissingTransferEvidence('Ending holdings and buy-only history are unavailable to verify transferred holdings.')
                group['lots'] = _broker_opening_lots(db, account, conid, 'USD', min(d['date'] for d in group['transfers']), period[1],
                    Decimal(group['quantity']), Decimal(group['basis']) if group['basis'] is not None else None,
                    Decimal(group['max_transfer_basis']) if group['max_transfer_basis'] is not None else None)
                broker_basis = sum((Decimal(lot['basis']) for lot in group['lots']), Decimal(0))
                group['basis'] = str(broker_basis)
            except MissingTransferEvidence as exc:
                group.update(status='pending', reason=str(exc), lots=[])
    return list(groups.values())


def import_transfers(db, groups, account, portfolio_id=1):
    """Caller owns the transaction, including executions and reconciliation."""
    from app.broker.broker_sync import BrokerIngestor
    ingestor = BrokerIngestor(db, account, portfolio_id)
    changed, added, skipped = set(), 0, 0
    for group in groups:
        if group['status'] == 'pending':
            db.conn.execute('''INSERT INTO broker_source_records
                (account_id,source,source_id,status,issue,raw_json)
                VALUES (?,'activity_statement_transfer',?,'pending',?,?)
                ON CONFLICT(account_id,source,source_id) DO UPDATE SET
                    status=excluded.status,issue=excluded.issue,raw_json=excluded.raw_json''',
                (account, group['source_id'], group['reason'], json.dumps(group, sort_keys=True)))
            continue
        if group['already_saved']:
            skipped += 1
            continue
        position_id = ingestor._position(dict(group, security_type='STK'))
        ticker = db.conn.execute('SELECT display_ticker FROM positions WHERE id=?', (position_id,)).fetchone()[0]
        for index, lot in enumerate(group['lots']):
            source_id = f"{group['source_id']}:{index}"
            qty, basis = float(lot['quantity']), float(lot['basis'])
            note = (f"Transferred holding as of {group['period'][1]}; original acquisition {lot['acquired']}. "
                    'Quantity and basis verified from Activity Statement and broker lots; not a buy execution.')
            cur = db.conn.execute('''INSERT INTO trades
                (portfolio_id,position_id,trade_id,trade_date,display_ticker,ib_symbol,side,
                 quantity,price,security_type,multiplier,currency,fx_to_usd,commission_usd,source,notes)
                VALUES (?,?,?,?,?,?,'BUY',?,?,'STK',1,'USD',1,0,'IBKR statement transfer',?)''',
                (portfolio_id, position_id, f'IBKR:{account}:{source_id}', group['period'][1], ticker,
                 group['symbol'], qty, basis / qty, note))
            raw = dict(lot, con_id=group['con_id'], currency='USD', period=group['period'], group_id=group['source_id'])
            db.conn.execute('''INSERT INTO broker_source_records
                (account_id,source,source_id,trade_row_id,status,raw_json)
                VALUES (?,'activity_statement_transfer_lot',?,?,'complete',?)''',
                (account, source_id, cur.lastrowid, json.dumps(raw, sort_keys=True)))
        db.conn.execute('''INSERT INTO broker_source_records
            (account_id,source,source_id,status,raw_json)
            VALUES (?,'activity_statement_transfer',?,'complete',?)
            ON CONFLICT(account_id,source,source_id) DO UPDATE SET
                status='complete',issue='',raw_json=excluded.raw_json''',
            (account, group['source_id'], json.dumps(group, sort_keys=True)))
        changed.add(position_id)
        added += 1
    return changed, added, skipped
