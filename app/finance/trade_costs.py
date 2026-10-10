"""Trade taxes stay separate from commissions and their later refunds."""

import json
import math


def trade_cost_usd(trade):
    """Total execution costs, positive for charges and negative for rebates."""
    if not trade.get('commission_complete', True):
        return None
    fee, tax = trade.get('commission_usd'), trade.get('tax_usd', 0)
    if fee is None or tax is None:
        return None
    value = float(fee) + float(tax)
    return value if math.isfinite(value) else None


def upgrade_saved_trade_taxes(db):
    """Recover tax from linked, unchanged Flex executions without downloading."""
    from app.broker.broker_sync import execution_identity
    changed = set()
    records = db.conn.execute('''SELECT t.*, p.con_id, r.account_id, r.raw_json, e.revision AS current_revision,
               e.execution_key AS current_key
        FROM trades t JOIN positions p ON p.id=t.position_id
        JOIN broker_executions e ON e.trade_row_id=t.id
        JOIN broker_source_records r ON r.trade_row_id=t.id
          AND r.account_id=e.account_id AND r.execution_key=e.execution_key
        WHERE r.source='flex' AND r.status NOT IN ('review','superseded')''').fetchall()
    for row in records:
        raw = json.loads(row['raw_json'])
        key, revision = execution_identity({'ib_exec_id': raw.get('ibExecID'), 'flex_trade_id': raw.get('tradeID')})
        if key != row['current_key'] or revision != row['current_revision']:
            continue
        if 'taxes' not in raw:
            continue
        try:
            # A stale corrected execution must never enrich its replacement.
            if (raw.get('buySell') != row['side'] or raw.get('currency') != row['currency']
                    or int(raw.get('conid')) != row['con_id']
                    or abs(abs(float(raw['quantity'])) - row['quantity']) > 1e-8
                    or abs(float(raw['tradePrice']) - row['price']) > 1e-8
                    or raw.get('tradeDate', raw.get('dateTime', '')[:8]) != row['trade_date'].replace('-', '')):
                continue
            tax = -float(raw['taxes'])
            fx = row['fx_to_usd']
            if not math.isfinite(tax) or not math.isfinite(fx) or fx <= 0:
                continue
            usd = tax * fx
        except (ValueError, TypeError, KeyError):
            continue
        if row['tax_usd'] is None or abs(row['tax_usd'] - usd) > 1e-9 or row['tax_lc'] != tax:
            db.conn.execute('UPDATE trades SET tax_lc=?,tax_usd=? WHERE id=?', (tax, usd, row['id']))
            changed.add(row['position_id'])
    return changed
