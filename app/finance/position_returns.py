"""Cumulative, auditable USD results for every recorded security position."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from app.finance.calculations import valid_multiplier, valid_price


INCOME_KINDS = {"Dividend", "Payment in lieu", "Withholding tax", "Bond interest"}


def _usd_amount(db, movement, fx_cache):
    try:
        amount = Decimal(movement["amount"])
        if movement["fx_to_usd"]:
            fx = Decimal(movement["fx_to_usd"])
        elif movement["currency"] == "USD":
            fx = Decimal("1")
        else:
            key = (movement["currency"], movement["reported_day"])
            if key not in fx_cache:
                fx_cache[key] = db.fx_rate(*key)
            fx = Decimal(str(fx_cache[key]))
        return float(amount * fx) if amount.is_finite() and fx.is_finite() and fx > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def position_return_rows(db, portfolio_id, portfolio_rows, lots):
    """Return (position rows, unattributed income rows), scoped to one portfolio."""
    positions = {p["id"]: p for p in db.rows("positions", portfolio_id)
                 if p["security_type"] != "CASH"}
    valuations = {p["id"]: p for p in portfolio_rows}
    trades = defaultdict(list)
    for trade in db.rows("trades", portfolio_id):
        if trade["position_id"] in positions and trade["security_type"] != "CASH":
            trades[trade["position_id"]].append(trade)
    lots_by_position = defaultdict(list)
    for lot in lots:
        lots_by_position[lot["position_id"]].append(lot)
    sell_by_match = {match["match_id"]: match["sell_trade_id"]
                     for match in db.rows("trade_matches", portfolio_id)}
    fx_cache = {}

    by_con_id, by_symbol = defaultdict(set), defaultdict(set)
    for position in positions.values():
        if position["con_id"]:
            by_con_id[position["con_id"]].add(position["id"])
        for symbol in (position["ib_symbol"], position["display_ticker"]):
            if symbol:
                by_symbol[symbol.strip().upper()].add(position["id"])

    movements = [m for m in db.cash_movement_rows(portfolio_id)
                 if m["income_type"] != "Transaction tax" and
                 (m["kind"] in INCOME_KINDS or m["income_type"] == "Bond interest")]
    assigned, unattributed = defaultdict(list), []
    transaction_positions = defaultdict(set)

    def direct_position(movement):
        if movement["con_id"]:
            candidates = by_con_id[movement["con_id"]]
        else:
            candidates = by_symbol[movement["symbol"].strip().upper()] if movement["symbol"] else set()
        return next(iter(candidates)) if len(candidates) == 1 else None

    for movement in movements:
        if movement["kind"] == "Withholding tax":
            continue
        position_id = direct_position(movement)
        if position_id is not None:
            assigned[position_id].append(movement)
            if movement["transaction_id"]:
                transaction_positions[movement["transaction_id"]].add(position_id)
        else:
            unattributed.append(movement)
    for movement in movements:
        if movement["kind"] != "Withholding tax":
            continue
        position_id = direct_position(movement)
        if position_id is None and movement["transaction_id"]:
            candidates = transaction_positions[movement["transaction_id"]]
            position_id = next(iter(candidates)) if len(candidates) == 1 else None
        (assigned[position_id] if position_id is not None else unattributed).append(movement)

    rows = []
    for position_id, position in positions.items():
        position_trades = trades[position_id]
        if not position_trades and not assigned[position_id] and not float(
                valuations.get(position_id, {}).get("position_qty") or position["position_qty"] or 0):
            continue
        position_lots = lots_by_position[position_id]
        valuation = valuations.get(position_id, {})
        quantity = float(valuation.get("position_qty") or 0)
        open_position = abs(quantity) > 1e-9
        short = quantity < -1e-9 or any(lot["lot_state"] == "Closed short"
                                         for lot in position_lots)
        closed = [lot for lot in position_lots if lot["lot_state"].startswith("Closed")]
        open_lots = [lot for lot in position_lots if lot["lot_state"] == "Open"]
        matched_sells = defaultdict(float)
        for lot in closed:
            sell_id = sell_by_match.get(lot["match_id"])
            if sell_id is not None:
                matched_sells[sell_id] += float(lot["sell_qty"])
        unmatched = any(t["side"] == "SELL" and
                        matched_sells[t["id"]] + 1e-9 < float(t["quantity"])
                        for t in position_trades)
        issues = []
        if unmatched:
            issues.append("Unmatched sale")
        if not position_trades:
            issues.append("Trade history missing")
        if position["con_id"] and len(by_con_id[position["con_id"]]) > 1:
            issues.append("Duplicate contract identity")
        if valuation.get("reconciliation") == "DIFFERENCE":
            issues.append("Position difference")
        if any(t["security_type"] != position["security_type"] for t in position_trades):
            issues.append("Security type mismatch")

        realized = (sum(float(l["realized_net"]) for l in closed)
                    if all(l.get("realized_net") is not None for l in closed) and not unmatched
                    else None)
        if any(l.get("realized_net") is None for l in closed):
            issues.append("Closed lot valuation missing")
        if open_position and not short:
            open_qty = sum(float(l["buy_qty"]) for l in open_lots)
            unrealized = (sum(float(l["net_unrealized"]) for l in open_lots)
                          if abs(open_qty - quantity) < 1e-9 and open_lots and
                          all(l.get("net_unrealized") is not None for l in open_lots) and
                          valuation.get("reconciliation") != "DIFFERENCE" else None)
            if unrealized is None:
                issues.append("Open lot valuation missing")
        else:
            unrealized = 0.0 if not open_position else None
            if open_position:
                issues.append("Open short valuation unavailable")

        income_details = []
        for movement in sorted(assigned[position_id], key=lambda item: (item["reported_day"], item["source_key"])):
            detail = dict(movement)
            detail["amount_usd"] = _usd_amount(db, movement, fx_cache)
            income_details.append(detail)
        income = (sum(d["amount_usd"] for d in income_details)
                  if all(d["amount_usd"] is not None for d in income_details) else None)
        if income is None:
            issues.append("Income FX missing")
        first_trade = min((t["trade_date"] for t in position_trades if t["trade_date"]), default=None)
        last_trade = max((t["trade_date"] for t in position_trades if t["trade_date"]), default=None)
        if first_trade and any(m["reported_day"] < first_trade for m in income_details):
            issues.append("Income predates recorded trades")
        if unattributed:
            issues.append("Unattributed income")
        total = (realized + unrealized + income
                 if realized is not None and unrealized is not None and income is not None and
                 not unattributed and not issues else None)
        cost = 0.0
        cost_valid = True
        for trade in position_trades:
            if trade["side"] != "BUY":
                continue
            multiplier = valid_multiplier(trade["multiplier"])
            fx = valid_price(trade["fx_to_usd"])
            if multiplier is None or fx is None or not trade["commission_complete"]:
                cost_valid = False
                break
            cost += float(trade["quantity"]) * float(trade["price"]) * multiplier * fx
            cost += float(trade["commission_usd"])
        percent = total / cost * 100 if total is not None and cost_valid and cost > 0 and not short else None
        if not cost_valid:
            issues.append("Purchase cost missing")
            total = percent = None
        rows.append({"position_id": position_id, "name": position["display_ticker"],
                     "symbol": position["ib_symbol"], "con_id": position["con_id"],
                     "status": "Short" if quantity < -1e-9 else "Open" if open_position else "Closed",
                     "realized": realized, "unrealized": unrealized, "income": income,
                     "total": total, "percent": percent, "cost": cost if cost_valid else None,
                     "issues": issues, "lots": position_lots, "income_details": income_details,
                     "first_trade": first_trade, "last_trade": last_trade,
                     "price_source": valuation.get("price_source") or "",
                     "last_updated": valuation.get("last_updated") or ""})
    return rows, unattributed
