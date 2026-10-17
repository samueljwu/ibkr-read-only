"""Cumulative, auditable USD results for every recorded security position."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite

from app.finance.calculations import valid_multiplier, valid_price


INCOME_KINDS = {"Dividend", "Payment in lieu", "Withholding tax", "Bond interest"}


def _cash_covered(windows, first, last):
    """Whether complete Cash Flex windows cover every calendar day in the period."""
    cursor = date.fromisoformat(first)
    end = date.fromisoformat(last)
    for start, stop in windows:
        left, right = date.fromisoformat(start), date.fromisoformat(stop)
        if right < cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right + timedelta(days=1))
        if cursor > end:
            return True
    return cursor > end


def _trade_accrued_cash(movement):
    """The Trade accruedInt already represents these Cash Transaction entries."""
    description = (movement["description"] or "").strip().casefold()
    return (movement["income_type"] == "Bond interest" and
            description.startswith(("accrued interest paid", "accrued interest received")))


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
    account_id = db.setting("ib_account_id")
    cash_windows = db.completed_flex_windows(account_id)["cash"]

    by_con_id, by_symbol = defaultdict(set), defaultdict(set)
    for position in positions.values():
        if position["con_id"]:
            by_con_id[position["con_id"]].add(position["id"])
        for symbol in (position["ib_symbol"], position["display_ticker"]):
            if symbol:
                by_symbol[symbol.strip().upper()].add(position["id"])

    movements = [m for m in db.cash_movement_rows(portfolio_id)
                 if m["income_type"] != "Transaction tax" and not _trade_accrued_cash(m) and
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
        first_trade = min((t["trade_date"] for t in position_trades if t["trade_date"]), default=None)
        estimated_baseline = next((t["trade_date"] for t in position_trades
                                   if t["source"] == "CSV market baseline"), None)
        last_trade = max((t["trade_date"] for t in position_trades if t["trade_date"]), default=None)
        last_income = max((m["reported_day"] for m in assigned[position_id]), default=None)
        cash_through = ((date.today() - timedelta(days=1)).isoformat() if open_position else
                        max((day for day in (last_trade, last_income) if day), default=None))
        cash_from = ((date.fromisoformat(estimated_baseline) + timedelta(days=1)).isoformat()
                     if estimated_baseline else first_trade)
        cash_complete = bool(cash_from and cash_through and
                             _cash_covered(cash_windows, cash_from, cash_through))
        if not cash_complete:
            issues.append("Cash history incomplete")

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
        if position["security_type"] == "BOND":
            for trade in position_trades:
                accrued = float(trade["accrued_interest_usd"] or 0)
                if accrued:
                    income_details.append({"source_key": f"trade-accrued:{trade['id']}",
                        "reported_day": trade["trade_date"], "kind": "Bond interest",
                        "income_type": "Accrued interest", "amount": str(accrued),
                        "amount_usd": accrued, "currency": "USD", "fx_to_usd": "1",
                        "description": "Accrued interest paid" if accrued < 0 else "Accrued interest received",
                        "transaction_id": trade["trade_id"]})
            if open_position:
                accrued = valuation.get("bond_accrued_interest_usd")
                try:
                    accrued = float(accrued)
                except (TypeError, ValueError):
                    accrued = None
                if accrued is None or not isfinite(accrued):
                    issues.append("Bond accrued interest unavailable")
                else:
                    income_details.append({"source_key": f"open-accrued:{position_id}",
                        "reported_day": date.today().isoformat(), "kind": "Bond interest",
                        "income_type": "Unpaid accrued interest", "amount": str(accrued),
                        "amount_usd": accrued, "currency": "USD", "fx_to_usd": "1",
                        "description": "Current accrued interest receivable", "transaction_id": ""})
        income_details.sort(key=lambda item: (item["reported_day"], item["source_key"]))
        income = (sum(d["amount_usd"] for d in income_details)
                  if all(d["amount_usd"] is not None for d in income_details) else None)
        if income is None:
            issues.append("Income FX missing")
        if first_trade and any(m["reported_day"] < first_trade for m in assigned[position_id]):
            issues.append("Income predates recorded trades")
        if unattributed:
            issues.append("Unattributed income")
        # Add the components that have a verified USD value. Default zeroes for
        # absent lots or income do not, by themselves, establish a return.
        has_component = ((bool(closed) and realized is not None) or
                         (bool(open_lots) and unrealized is not None) or
                         (bool(income_details) and income is not None))
        components = (realized, unrealized, income)
        total = sum(value for value in components if value is not None) if has_component else None
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
            # accrued_interest_usd is negative on a BUY (cash paid out), so subtracting
            # it increases cost, mirroring the closed_lot/open_lot proration convention.
            cost -= float(trade["accrued_interest_usd"] or 0)
        if not cost_valid:
            issues.append("Purchase cost missing")
        percent = total / cost * 100 if total is not None and cost_valid and cost > 0 and not short else None
        partial = total is not None and (bool(issues) or any(value is None for value in components))
        return_note = ""
        if partial:
            missing = [label for label, value in zip(
                ("Realized P&L", "Unrealized P&L", "Income/accrual amount"), components)
                if value is None]
            reasons = [("Cash history is incomplete; dividends, interest, or withholding "
                        "may be missing" if issue == "Cash history incomplete" else
                        "Some income or tax is unattributed and may belong to this position"
                        if issue == "Unattributed income" else issue)
                       for issue in issues]
            reasons.extend(f"{label} is unavailable" for label in missing)
            return_note = ("Partial return: sum of the available USD components. "
                           "It may be incomplete because " + "; ".join(reasons) + ".")
            if estimated_baseline:
                return_note += (f" The {estimated_baseline} CSV closing price is an estimated "
                                "opening value, not original purchase cost or tax basis; "
                                "earlier gains and losses are excluded.")
        rows.append({"position_id": position_id, "name": position["display_ticker"],
                     "symbol": position["ib_symbol"], "con_id": position["con_id"],
                     "security_type": position["security_type"],
                     "status": "Short" if quantity < -1e-9 else "Open" if open_position else "Closed",
                     "realized": realized, "unrealized": unrealized, "income": income,
                     "total": total, "percent": percent, "cost": cost if cost_valid else None,
                     "partial": partial, "return_note": return_note,
                     "issues": issues, "lots": position_lots, "income_details": income_details,
                     "first_trade": first_trade, "last_trade": last_trade,
                     "estimated_baseline": estimated_baseline,
                     "price_source": valuation.get("price_source") or "",
                     "last_updated": valuation.get("last_updated") or ""})
    return rows, unattributed
