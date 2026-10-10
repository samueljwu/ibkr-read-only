"""Preview and import pre-Flex executions and verified transferred holdings."""

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

from scripts.audit_activity_cash import one_value, statement_rows


ASSET_TYPES = {
    "stocks": "STK",
    "equity and index options": "OPT",
    "futures": "FUT",
    "bonds": "BOND",
}


def _value(row, columns, *names, required=True):
    for name in names:
        index = columns.get(name.casefold())
        if index is not None and index < len(row):
            value = row[index].strip()
            if value:
                return value
    if required:
        raise ValueError(f"Activity Statement Trades section is missing {names[0]}")
    return ""


def _number(value, label):
    try:
        result = Decimal(value.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"Activity Statement has an invalid {label}") from None
    if not result.is_finite():
        raise ValueError(f"Activity Statement has an invalid {label}")
    return result


def _decimal_key(value):
    return format(value.normalize(), "f") if value else "0"


def _execution_time(value):
    value = value.strip().replace(";", " ").replace(",", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d %H%M%S",
                    "%Y%m%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, pattern)
            return parsed.date().isoformat(), parsed.strftime("%H%M%S") if "%H" in pattern else ""
        except ValueError:
            pass
    raise ValueError(f"Unsupported Activity Statement trade date/time: {value}")


def preview_activity_statement_trades(db, path, portfolio_id=1):
    """Validate the entire CSV and return normalized rows without writing."""
    path = Path(path)
    raw_bytes = path.read_bytes()
    rows = statement_rows(path)
    from app.imports.activity_statement_transfers import stock_instruments, validate_contract, preview_transfers
    instruments = stock_instruments(rows)
    account = one_value(rows, "Account Information", "Account").strip()
    if not account or account != db.setting("ib_account_id").strip():
        raise ValueError("This Activity Statement is for a different IBKR account")
    if one_value(rows, "Account Information", "Base Currency").strip().upper() != "USD":
        raise ValueError("This import requires an Activity Statement with USD base currency")
    period = one_value(rows, "Statement", "Period")
    period_start_text, period_end_text = period.split(" - ", 1)
    period_start = datetime.strptime(period_start_text.strip(), "%B %d, %Y").date()
    period_end = datetime.strptime(period_end_text.strip(), "%B %d, %Y").date()
    if period_start > period_end:
        raise ValueError("Activity Statement period is reversed")

    positions = db.conn.execute("""SELECT id,con_id,display_ticker,ib_symbol,security_type,
        currency,local_symbol,multiplier FROM positions WHERE portfolio_id=? AND con_id IS NOT NULL""",
                                (portfolio_id,)).fetchall()
    by_identity = {}
    for position in positions:
        aliases = {name.strip().casefold() for name in
                   (position["display_ticker"], position["ib_symbol"], position["local_symbol"]) if name}
        for alias in aliases:
            by_identity.setdefault((alias, position["security_type"].upper(),
                                    position["currency"].upper()), []).append(position)

    parsed = []
    ignored_forex = 0
    repeated = Counter()
    flex_start = datetime(datetime.now().year - 4, 1, 1).date()
    columns = None
    required_columns = {"asset category", "currency", "symbol", "quantity"}
    for row in rows:
        if len(row) < 2 or row[0] != "Trades":
            continue
        if row[1] == "Header":
            columns = {name.strip().casefold(): index + 2
                       for index, name in enumerate(row[2:]) if name.strip()}
            if (any(name not in columns for name in required_columns) or
                    not {"date/time", "trade date"}.intersection(columns) or
                    not {"t. price", "price"}.intersection(columns)):
                raise ValueError("Trades section needs asset category, currency, symbol, date/time, quantity and price columns")
            continue
        if row[1] != "Data" or columns is None:
            continue
        if len(row) < 3 or row[2].strip().casefold() not in {"order", "trade"}:
            continue
        asset = _value(row, columns, "Asset Category").casefold()
        if asset == "forex":
            # Currency conversions affect cash balances, not the securities and
            # lot history this recovery import promises to restore.
            ignored_forex += 1
            continue
        security_type = ASSET_TYPES.get(asset)
        if not security_type:
            raise ValueError(f"Unsupported Activity Statement asset category: {asset}")
        currency = _value(row, columns, "Currency").upper()
        symbol = _value(row, columns, "Symbol")
        day, clock = _execution_time(_value(row, columns, "Date/Time", "Trade Date"))
        execution_day = datetime.strptime(day, "%Y-%m-%d").date()
        if not period_start <= execution_day <= period_end:
            raise ValueError("Activity Statement contains a trade outside its stated period")
        if execution_day >= flex_start:
            raise ValueError(f"This statement includes trades from {flex_start.isoformat()} or later. Use Import history for those dates; use this import only for older trades to avoid duplicates.")
        quantity_signed = _number(_value(row, columns, "Quantity"), "quantity")
        if not quantity_signed:
            continue
        side_text = _value(row, columns, "Buy/Sell", "Side", required=False).upper()
        side = ({"BUY": "BUY", "BOT": "BUY", "B": "BUY",
                 "SELL": "SELL", "SLD": "SELL", "S": "SELL"}.get(side_text)
                if side_text else None)
        proceeds_text = _value(row, columns, "Proceeds", required=False)
        if not side and proceeds_text:
            proceeds = _number(proceeds_text, "proceeds")
            if proceeds:
                side = "BUY" if proceeds < 0 else "SELL"
                if quantity_signed < 0 and side != "SELL":
                    raise ValueError("Activity Statement quantity and proceeds disagree about trade side")
        if not side and quantity_signed < 0:
            side = "SELL"
        if not side:
            raise ValueError("Activity Statement does not identify whether a trade was bought or sold")
        if quantity_signed < 0 and side != 'SELL':
            raise ValueError('Activity Statement quantity and trade side disagree')
        if proceeds_text:
            proceeds = _number(proceeds_text, 'proceeds')
            if proceeds and side != ('BUY' if proceeds < 0 else 'SELL'):
                raise ValueError('Activity Statement proceeds and trade side disagree')
        price = _number(_value(row, columns, "T. Price", "Price"), "trade price")
        if price < 0:
            raise ValueError("Activity Statement contains a negative trade price")
        commission_name = next((name for name in ("Comm/Fee", "Total Commission", "Commission",
                                                   "Comm in USD") if name.casefold() in columns), None)
        commission_text = _value(row, columns, commission_name, required=False) if commission_name else ""
        commission = abs(_number(commission_text, "commission")) if commission_text else Decimal(0)
        tax_text = _value(row, columns, "Taxes", "Tax", required=False)
        taxes = _number(tax_text, "taxes") if tax_text else None
        commission_currency = "USD" if commission_name and commission_name.casefold() == "comm in usd" else currency

        candidates = by_identity.get((symbol.strip().casefold(), security_type, currency), [])
        instrument = instruments.get(symbol.casefold()) if security_type == 'STK' else None
        if instrument:
            validate_contract(db, instrument, currency, portfolio_id)
            if candidates and any(p['con_id'] != instrument['con_id'] for p in candidates):
                raise ValueError(f'Statement contract conflicts with the saved ticker: {symbol}')
            position = dict(instrument, currency=currency)
        elif len(candidates) == 1:
            position = candidates[0]
        else:
            raise ValueError(f"Cannot uniquely match {symbol} ({security_type}, {currency}) to an existing IBKR contract")
        if not position["multiplier"] or position["multiplier"] <= 0:
            raise ValueError(f"IBKR contract multiplier is missing for {symbol}")
        fx = 1.0 if currency == "USD" else db.fx_rate(currency, day)
        if not fx or fx <= 0:
            raise ValueError(f"No saved USD exchange rate is available for {currency} on {day}")
        if taxes is None:
            net_cash_text = _value(row, columns, "Net Cash", required=False)
            if net_cash_text and proceeds_text and commission_text:
                commission_lc = (commission / Decimal(str(fx)) if commission_currency == 'USD' and currency != 'USD'
                                 else commission)
                taxes = _number(net_cash_text, "net cash") - _number(proceeds_text, "proceeds") + commission_lc
        marker = (position["con_id"], day, clock, side, _decimal_key(abs(quantity_signed)),
                  _decimal_key(price), _decimal_key(commission), currency)
        repeated[marker] += 1
        identity = sha256(json.dumps((account, marker, repeated[marker]),
                                     separators=(",", ":")).encode()).hexdigest()
        source_id = f"AS:{identity}"
        parsed.append({
            "account_id": account, "source": "activity_statement", "source_id": source_id,
            "flex_trade_id": source_id, "trade_date": day, "executed_at": clock,
            "side": side, "quantity": float(abs(quantity_signed)), "price": float(price),
            "con_id": position["con_id"], "symbol": position["ib_symbol"],
            "security_type": security_type, "currency": currency,
            "multiplier": position["multiplier"], "fx_to_usd": fx,
            "taxes": float(taxes) if taxes is not None else None,
            "commission": float(commission), "commission_currency": commission_currency,
            "local_symbol": position["local_symbol"],
            "raw": {"source": "IBKR Activity Statement CSV", "account": account,
                    "period": period, "source_hash": sha256(raw_bytes).hexdigest(),
                    "statement_symbol": symbol, "trade_date": day, "time": clock,
                    "side": side, "quantity": str(abs(quantity_signed)),
                    "price": str(price), "commission": str(commission), "taxes": str(taxes) if taxes is not None else None,
                    "commission_currency": commission_currency,
                    "currency": currency, "con_id": position["con_id"]},
        })
    period_days = (period_start.isoformat(), period_end.isoformat())
    transfers = preview_transfers(db, rows, parsed, account, period_days, portfolio_id)
    if not parsed and not transfers:
        raise ValueError("No supported execution rows were found in the Activity Statement")
    return {"account": account, "period": period_days,
            "rows": parsed, "ignored_forex": ignored_forex,
            "transfers": transfers, "_statement_rows": rows,
            "statement_hash": sha256(raw_bytes).hexdigest()}


def import_activity_statement_trades(db, preview, portfolio_id=1):
    """Atomically ingest a previously validated preview using replay-safe IDs."""
    from app.broker.broker_sync import BrokerIngestor
    from app.broker.broker_lots import check_open_lots, reconcile_closed_lots
    from app.finance.services import MatchingService
    from app.imports.activity_statement_transfers import validate_contract, preview_transfers, import_transfers

    with db.conn:
        if not db.conn.in_transaction:
            db.conn.execute('BEGIN IMMEDIATE')
        account = db.setting("ib_account_id").strip()
        if account != preview["account"]:
            raise ValueError("The configured IBKR account changed after the preview")
        records, duplicates = [], 0
        for record in preview["rows"]:
            validate_contract(db, record, record['currency'], portfolio_id)
            prior = db.conn.execute("""SELECT t.*,p.con_id FROM broker_source_records r
                JOIN trades t ON t.id=r.trade_row_id JOIN positions p ON p.id=t.position_id
                WHERE r.account_id=? AND r.source='activity_statement' AND r.source_id=?
                  AND r.status='complete'""", (account, record["source_id"])).fetchone()
            if prior and prior['tax_usd'] is None:
                # Legacy imports already own this execution identity. Enrich its
                # costs in place instead of skipping it or creating another trade.
                if (prior['con_id'] != record['con_id'] or prior['trade_date'] != record['trade_date']
                        or prior['side'] != record['side'] or prior['currency'] != record['currency']
                        or abs(prior['quantity'] - record['quantity']) > 1e-7
                        or abs(prior['price'] - record['price']) > 1e-8):
                    raise ValueError("A saved Activity Statement execution changed; review it before enriching transaction tax")
                records.append(record)
                continue
            if prior:
                duplicates += 1
                continue
            sql = """SELECT t.id,t.commission_usd,t.tax_usd FROM trades t
                JOIN positions p ON p.id=t.position_id WHERE t.portfolio_id=? AND p.con_id=?
                  AND t.trade_date=? AND t.side=? AND ABS(t.quantity-?)<0.0000001
                  AND ABS(t.price-?)<0.00000001"""
            params = [portfolio_id, record["con_id"], record["trade_date"], record["side"],
                      record["quantity"], record["price"]]
            if record["executed_at"]:
                sql += " AND t.executed_at=?"
                params.append(record["executed_at"])
            matches = db.conn.execute(sql, params).fetchall()
            if len(matches) == 1:
                commission_currency = record.get("commission_currency") or record["currency"]
                commission_fx = (1.0 if commission_currency == "USD" else
                                 record["fx_to_usd"] if commission_currency == record["currency"] else
                                 db.fx_rate(commission_currency, record["trade_date"]))
                if commission_fx is None or commission_fx <= 0:
                    raise ValueError("No saved USD exchange rate is available for the statement commission")
                expected_fee = record["commission"] * commission_fx
                if (abs(matches[0]["commission_usd"] - expected_fee) <= 0.01 and
                        matches[0]["tax_usd"] is not None and record["taxes"] is not None and
                        abs(matches[0]["tax_usd"] + record["taxes"] * record["fx_to_usd"]) <= 0.01):
                    duplicates += 1
                    continue
                raise ValueError("A matching execution already exists with different commission or transaction tax; review it before importing")
            if len(matches) > 1:
                raise ValueError("An Activity Statement execution matches multiple saved trades")
            records.append(record)
        result = (BrokerIngestor(db, account, portfolio_id).ingest_many(
            records, "activity_statement", transaction=False) if records else None)
        if result and result.execution_review:
            raise ValueError("Some rows could not be safely matched. No Activity Statement trades were imported. "
                             + '; '.join(result.issues))
        changed = set(result.changed_position_ids if result else ())
        # Recheck against the current ledger/evidence. A preview never authorizes
        # topping up a partially imported or subsequently edited opening.
        transfers = preview_transfers(db, preview.get('_statement_rows', []), preview['rows'],
                                     account, preview['period'], portfolio_id)
        transfer_changed, transfers_added, transfers_skipped = import_transfers(db, transfers, account, portfolio_id)
        changed.update(transfer_changed)
        # Missing openings previously produced inferred short allocations. Free
        # only those automatic links before applying the now-complete lot proof.
        for position_id in transfer_changed:
            db.conn.execute('''DELETE FROM trade_matches WHERE origin IN ('auto','auto_short')
                AND sell_trade_id IN (SELECT id FROM trades WHERE portfolio_id=? AND position_id=?)''',
                (portfolio_id, position_id))
        changed.update(reconcile_closed_lots(db, account, portfolio_id))
        if changed:
            MatchingService(db, portfolio_id).rebuild_auto(changed, transaction=False)
        check_open_lots(db, account, portfolio_id)
    return {"ingest": result, "duplicates": duplicates, "attempted": len(records),
            "transfers_added": transfers_added, "transfers_skipped": transfers_skipped,
            "transfer_quantity": sum(float(g['quantity']) for g in transfers if not g['already_saved'])}
