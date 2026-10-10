"""Lifetime trading P&L by instrument, retaining missing-data flags."""

import math
import re
from collections import defaultdict

from app.finance.calculations import valid_multiplier, valid_price
from app.finance.trade_costs import trade_cost_usd


def _number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _sum(values):
    values = list(values)
    return sum(values) if all(value is not None for value in values) else None


def open_short_rows(trades, positions):
    """Value unmatched sales only when the broker confirms the remaining short.

    Shared by lifetime returns and the execution table. Each result belongs to
    one SELL execution; its opening fee is allocated to the remaining quantity.
    """
    positions = {row["id"]: row for row in positions}
    grouped = defaultdict(list)
    for trade in trades:
        if trade.get("security_type") != "CASH":
            grouped[(trade.get("position_id"), trade.get("security_type", "STK"),
                     trade.get("currency", "USD"), trade.get("multiplier", 1))].append(trade)
    rows = []
    for (position_id, *_), executions in grouped.items():
        sells = [trade for trade in executions
                 if trade["side"] == "SELL" and (trade.get("remaining_qty") or 0) > 1e-9]
        if not sells:
            continue
        position = positions.get(position_id, {})
        remaining_buys = sum(trade.get("remaining_qty") or 0 for trade in executions
                             if trade["side"] == "BUY")
        short_qty = sum(trade["remaining_qty"] for trade in sells)
        verified = (position.get("reconciliation") == "OK" and not remaining_buys
                    and abs((position.get("broker_qty") or 0) + short_qty) < 1e-9)
        for sell in sells:
            qty = sell["remaining_qty"]
            price, fx = valid_price(position.get("last_px")), valid_price(position.get("fx_to_usd"))
            multiplier = valid_multiplier(sell.get("multiplier"))
            proceeds, fee = _number(sell.get("gross_usd")), trade_cost_usd(sell)
            usable = (verified and price is not None and fx is not None
                      and multiplier is not None and multiplier == position.get("multiplier")
                      and sell.get("security_type") == position.get("security_type")
                      and proceeds is not None and fee is not None)
            gain = ((proceeds - fee) * qty / sell["quantity"] - qty * price * multiplier * fx
                    if usable else None)
            rows.append({"lot_state": "Open short" if verified else "Unmatched sale",
                         "sell_trade_id": sell["id"], "verified_short": verified,
                         "buy_date": "", "sell_date": sell.get("trade_date", ""),
                         "buy_qty": qty, "realized_net": None, "net_unrealized": gain,
                         "buy_value_usd": None, "buy_commission": None,
                         "lot_note": "Return on cost unavailable for short histories" if usable
                         else "Unmatched sale" if not verified else "Short valuation incomplete"})
    return rows


def _option_root(value):
    """Read a ticker or a supported option name without treating a full contract as a root."""
    value = str(value or "").strip().upper()
    for pattern in (
            r"^([A-Z0-9][A-Z0-9./_-]*?)\s*\d{6}[CP]\d{8}(?:\s*\[\d+\])?$",
            r"^([A-Z0-9][A-Z0-9./_-]*)\s+\d{1,2}/\d{1,2}/\d{2,4}\s+[CP]\d+(?:\.\d+)?$"):
        match = re.fullmatch(pattern, value)
        if match:
            return match[1]
    return value if re.fullmatch(r"[A-Z0-9][A-Z0-9./_-]*", value) else ""


def _group_option_rows(rows, positions, facts):
    """Combine already-valued contracts so opposite legs never net during reconciliation."""
    aliases = defaultdict(set)
    option_rows = []
    others = []
    by_contract = {position.get("con_id"): position for position in positions.values()
                   if position.get("con_id") and position.get("security_type") != "OPT"}
    for row in rows:
        if row["key"][2] != "OPT":
            others.append(row)
            continue
        position = positions.get(row["position_ids"][0], {}) if row["position_ids"] else {}
        fact = facts.get(position.get("con_id"), {})
        underlying = by_contract.get(fact.get("under_con_id"), {})
        broker_symbol = (str(fact.get("under_symbol") or "").strip().upper()
                         or _option_root(underlying.get("ib_symbol"))
                         or _option_root(underlying.get("display_ticker")))
        root = (_option_root(position.get("ib_symbol"))
                or _option_root(position.get("local_symbol"))
                or _option_root(position.get("raw_display_ticker"))
                or _option_root(row["name"]))
        if broker_symbol and root:
            aliases[root].add(broker_symbol)
        option_rows.append((row, broker_symbol, root))

    groups = defaultdict(list)
    for row, broker_symbol, root in option_rows:
        symbol = broker_symbol or (next(iter(aliases[root])) if len(aliases[root]) == 1 else root)
        if not symbol:
            row["notes"] = "; ".join(part for part in (row["notes"], "Underlying ticker unavailable") if part)
            others.append(row)
            continue
        groups[symbol].append(row)
    for symbol, members in groups.items():
        totals = returns_summary(members)
        contracts = sorted({member["name"] for member in members}, key=str.casefold)
        lots = [{**lot, "contract": member["name"]} for member in members for lot in member["lots"]]
        others.append({**totals, "key": ("option_underlying", symbol), "name": f"{symbol} options",
                       "status": "Held" if any(member["status"] == "Held" for member in members) else "Closed",
                       "short_history": any(member["short_history"] for member in members),
                       "trade_count": sum(member["trade_count"] for member in members),
                       "lots": lots, "contracts": contracts,
                       "search_terms": sorted({term for member in members for term in member.get("search_terms", ())}),
                       "position_ids": [pid for member in members for pid in member["position_ids"]],
                       "first_trade": min((member["first_trade"] for member in members if member["first_trade"]), default=""),
                       "notes": "; ".join(sorted({note for member in members for note in member["notes"].split("; ") if note})),
                       "valuation": "; ".join(sorted({member["valuation"] for member in members if member["valuation"]})),
                       "instrument": f"OPT · {symbol} · {len(members)} contract{'s' if len(members) != 1 else ''}"})
    return others


def returns_rows(trades, lots, positions, *, underlying_facts=None):
    """Aggregate existing lot P&L; count each BUY's cost exactly once.

    Percentages are cumulative return on purchase cost for long-only histories.
    Short histories retain dollar P&L but have no purchase-cost percentage.
    Unmatched sales are only valued as shorts when broker quantity verifies them.
    """
    positions = {row["id"]: row for row in positions}
    by_id = {row["id"]: row for row in trades}
    grouped_trades, grouped_lots, grouped_positions = defaultdict(list), defaultdict(list), defaultdict(dict)

    def key(trade):
        position = positions.get(trade.get("position_id"), {})
        identity = ("contract", position["con_id"]) if position.get("con_id") else (
            "position", trade.get("position_id"))
        return (*identity, trade.get("security_type", "STK"),
                trade.get("currency", "USD"), trade.get("multiplier", 1))

    for trade in trades:
        if trade.get("security_type") == "CASH":
            continue
        group = key(trade)
        grouped_trades[group].append(trade)
        position = positions.get(trade.get("position_id"))
        if position:
            grouped_positions[group][position["id"]] = position
    for lot in lots:
        trade = by_id.get(lot.get("buy_trade_id"))
        if trade and trade.get("security_type") != "CASH":
            grouped_lots[key(trade)].append(dict(lot))
    # Broker holdings without acquisition history still need a visible row.
    for position in positions.values():
        if position.get("security_type") == "CASH":
            continue
        if (abs(position.get("broker_qty") or position.get("position_qty") or 0) > 1e-9
                and not any(position["id"] in group for group in grouped_positions.values())):
            group = key({**position, "position_id": position["id"]})
            grouped_trades[group]  # Include even when no executions exist.
            grouped_positions[group][position["id"]] = position

    result = []
    for group, executions in grouped_trades.items():
        valuations = list(grouped_positions[group].values())
        details = grouped_lots[group]
        closed = [lot for lot in details if lot["lot_state"].startswith("Closed")]
        opened = [lot for lot in details if lot["lot_state"] == "Open"]
        notes = set()
        realized = _sum(_number(lot.get("realized_net")) for lot in closed)
        unrealized = _sum(_number(lot.get("net_unrealized")) for lot in opened)
        short_history = any(lot["lot_state"] == "Closed short" for lot in closed)
        costs = []
        for trade in executions:
            if trade["side"] == "BUY":
                amount, fee = _number(trade.get("gross_usd")), trade_cost_usd(trade)
                costs.append(amount + fee if amount is not None and fee is not None else None)
        cost = _sum(costs)
        for lot in details:
            value = lot.get("realized_net") if lot["lot_state"].startswith("Closed") else lot.get("net_unrealized")
            if value is None:
                notes.add(lot.get("lot_note") or "Lot valuation incomplete")
        if cost is None:
            notes.add("Purchase cost or fees unavailable")
        if any(trade.get("valuation_status") == "VERIFY SECURITY TYPE" for trade in executions):
            realized = unrealized = None
            notes.add("Verify security type")

        for detail in open_short_rows(executions, positions.values()):
            if detail["verified_short"]:
                short_history = True
                unrealized = _sum((unrealized, detail["net_unrealized"]))
                if detail["net_unrealized"] is None:
                    notes.add("Short price, FX, multiplier or fees unavailable")
            else:
                realized = unrealized = None
                notes.add("Unmatched sale / short quantity unverified")
            details.append(detail)

        if not executions:
            realized = unrealized = cost = None
            notes.add("Acquisition history unavailable")
        if any(position.get("reconciliation") == "DIFFERENCE" for position in valuations):
            unrealized = None
            notes.add("IBKR quantity differs; history may be incomplete")
        total = _sum((realized, unrealized))
        return_pct = total / cost * 100 if total is not None and cost is not None and cost > 0 and not short_history else None
        if short_history:
            notes.add("Return on cost unavailable for short histories")
        held = (any(abs(position.get("broker_qty") or position.get("position_qty") or 0) > 1e-9
                    for position in valuations)
                or any((trade.get("remaining_qty") or 0) > 1e-9 for trade in executions))
        sources = sorted({" · ".join(part for part in
                                    (position.get("price_source", ""), position.get("last_updated", "")) if part)
                          for position in valuations if held and position.get("price_source")})
        ticker = ((valuations[0].get("display_ticker") if valuations else None)
                  or (executions[0].get("display_ticker") if executions else None)
                  or "Unnamed instrument")
        instrument = " · ".join(str(part) for part in (group[2], group[3],
                                f"IBKR {group[1]}" if group[0] == "contract" else "") if part)
        result.append({"key": group, "name": ticker, "status": "Held" if held else "Closed",
                       "realized": realized, "unrealized": unrealized, "total": total,
                       "cost": cost, "return_pct": return_pct, "short_history": short_history,
                       "trade_count": len(executions), "lots": details,
                       "first_trade": min((trade.get("trade_date") for trade in executions if trade.get("trade_date")), default=""),
                       "notes": "; ".join(sorted(notes)), "valuation": "; ".join(sources),
                       "instrument": instrument, "position_ids": list(grouped_positions[group]),
                       "search_terms": sorted({str(row[field]) for row in (*valuations, *executions)
                                               for field in ("raw_display_ticker", "local_symbol", "ib_symbol")
                                               if row.get(field)})})
    result = _group_option_rows(result, positions, underlying_facts or {})
    return sorted(result, key=lambda row: (row["total"] is None, -(row["total"] or 0), row["name"]))


def returns_summary(rows):
    """Aggregate displayed rows; percentages use total profit / total cost."""
    totals = {field: _sum(_number(row[field]) for row in rows)
              for field in ("realized", "unrealized", "total", "cost")}
    totals["return_pct"] = (totals["total"] / totals["cost"] * 100
                            if totals["total"] is not None and totals["cost"] is not None
                            and totals["cost"] > 0 and not any(row["short_history"] for row in rows) else None)
    return totals
