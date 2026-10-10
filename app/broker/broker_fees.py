"""Link delayed Flex commission adjustments using retained broker identifiers."""
from decimal import Decimal
import json


def reconcile_fee_adjustments(db, account, portfolio_id=1):
    conn = db.conn
    by_id = {}
    for row in conn.execute("""SELECT trade_row_id,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex' AND trade_row_id IS NOT NULL""", (account,)):
        raw = json.loads(row['raw_json'])
        if raw.get('tradeID'):
            by_id.setdefault(raw['tradeID'], set()).add(row['trade_row_id'])
    for record in conn.execute("""SELECT source_id,raw_json FROM broker_source_records
            WHERE account_id=? AND source='flex_fee'""", (account,)).fetchall():
        raw = json.loads(record['raw_json'])
        ids = by_id.get(raw.get('tradeID'), set())
        issue = 'Commission adjustment requires a unique broker execution'
        row_id = None
        if len(ids) == 1:
            row_id = next(iter(ids))
            trade = conn.execute('SELECT * FROM trades WHERE id=? AND portfolio_id=?',
                                 (row_id, portfolio_id)).fetchone()
            refund = Decimal(raw['amount'])
            # Cash Report commissions already include adjustments; never add
            # these to non-trade cash a second time.
            fx = Decimal(1) if raw.get('currency') == 'USD' else db.fx_rate(
                raw.get('currency'), raw['reported_day'])
            if fx is not None:
                refund *= Decimal(str(fx))
            if trade and fx and refund >= 0:
                other = sum((Decimal(row[0]) for row in conn.execute('''SELECT refund_usd
                    FROM trade_fee_adjustments WHERE portfolio_id=? AND account_id=?
                    AND trade_row_id=? AND source_key<>?''',
                    (portfolio_id, account, row_id, record['source_id']))), Decimal(0))
                if other + refund <= Decimal(str(trade['commission_usd'])) + Decimal('0.0001'):
                    conn.execute('''INSERT INTO trade_fee_adjustments
                        (portfolio_id,account_id,source_key,trade_row_id,reported_day,refund_usd,description,source_hash)
                        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(portfolio_id,account_id,source_key)
                        DO UPDATE SET trade_row_id=excluded.trade_row_id,reported_day=excluded.reported_day,
                        refund_usd=excluded.refund_usd,description=excluded.description,source_hash=excluded.source_hash''',
                        (portfolio_id, account, record['source_id'], row_id, raw['reported_day'],
                         str(refund), raw.get('description', ''), raw['source_hash']))
                    issue = ''
                else:
                    issue = 'Commission adjustments exceed the original execution fee'
            else:
                issue = 'Commission adjustment needs valid currency conversion or refund amount'
        conn.execute('''UPDATE broker_source_records SET status=?,issue=?,trade_row_id=?
            WHERE account_id=? AND source='flex_fee' AND source_id=?''',
            ('review' if issue else 'complete', issue, row_id, account, record['source_id']))
