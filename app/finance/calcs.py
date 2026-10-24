"""Read-only portfolio target calculations."""

import math


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def scale_target_weights(targets, cash_target):
    """Scale nonnegative targets to the entered cash weight in 0.01% units."""
    cash = _finite(cash_target)
    if cash is None:
        raise ValueError("Enter a valid cash target.")
    position_points = round((1 - cash) * 10000)
    if position_points < 0:
        raise ValueError("Cash above 100% would require negative position weights.")
    keys = list(targets)
    bases = [_finite(targets[key]) for key in keys]
    if any(value is None or value < 0 for value in bases):
        raise ValueError("Position targets cannot be negative.")
    total = sum(bases)
    if total <= 1e-12:
        if position_points:
            raise ValueError("Zero position targets cannot be scaled to this cash weight.")
        return {key: 0.0 for key in keys}
    raw = [position_points * value / total for value in bases]
    points = [math.floor(value) for value in raw]
    for index in sorted(range(len(keys)), key=lambda i: (-(raw[i] - points[i]), i))[:position_points - sum(points)]:
        points[index] += 1
    return {key: value / 10000 for key, value in zip(keys, points)}


def calculate_targets(rows, nav, cash, *, mode="individual", targets=None,
                      cash_target=None):
    """Return a long-only allocation scenario, or an explicit reason it is unavailable.

    Targets and cash_target are fractions of NAV. Signed gaps are positive buys.
    """
    targets = targets or {}
    nav = _finite(nav)
    cash = _finite(cash)
    if nav is None or nav <= 0:
        return {"error": "A positive USD NAV is required."}
    if cash is None:
        return {"error": "A USD cash balance is required."}
    active = [row for row in rows if row.get("hypothetical") or
              abs(_finite(row.get("position_qty")) or 0) > 1e-9]
    if any((_finite(row.get("position_qty")) or 0) < 0 for row in active):
        return {"error": "Short positions are outside this long-only calculator."}
    if any(not row.get("hypothetical") and row.get("reconciliation") == "DIFFERENCE" for row in active):
        return {"error": "A position differs from the broker snapshot."}
    if any(_finite(row.get("value")) is None or
           (_finite(row.get("value")) <= 0 and not row.get("hypothetical")) for row in active):
        return {"error": "A current position needs a price, FX rate, or multiplier."}
    holdings = sum(float(row["value"]) for row in active)
    difference = nav - holdings - cash
    if abs(difference) > max(1.0, nav * 0.001):
        return {"error": f"NAV differs from holdings plus cash by ${difference:,.2f}. Review account values before sizing trades."}
    current_cash_weight = cash / nav
    if mode == "proportional":
        cash_weight = current_cash_weight if cash_target is None else _finite(cash_target)
        if cash_weight is None or cash_weight > 1:
            return {"error": "Cash above 100% would require negative position weights."}
        base_weights = {}
        for row in active:
            value = _finite(targets.get(str(row["id"]), row["value"] / nav))
            if value is None or value < 0:
                return {"error": "Position targets cannot be negative."}
            base_weights[row["id"]] = value
        base_total = sum(base_weights.values())
        if base_total <= 1e-12:
            if cash_weight < 1 - 1e-9:
                return {"error": "Zero position targets cannot be scaled to this cash weight."}
            position_targets = {row["id"]: 0 for row in active}
        else:
            position_targets = {row["id"]: (1 - cash_weight) * base_weights[row["id"]] / base_total
                                for row in active}
    elif mode == "individual":
        position_targets = {}
        for row in active:
            value = _finite(targets.get(str(row["id"]), row["value"] / nav))
            if value is None or value < 0:
                return {"error": "Position targets cannot be negative."}
            position_targets[row["id"]] = value
        cash_weight = 1 - sum(position_targets.values())
    else:
        return {"error": "Unknown target mode."}

    result_rows = []
    for row in active:
        quantity = float(row["position_qty"])
        unit_usd = (_finite(row.get("unit_price_usd")) if row.get("hypothetical") else
                    row["value"] / quantity)
        gap = nav * position_targets[row["id"]] - row["value"]
        result_rows.append({**row, "current_weight": row["value"] / nav,
                            "target_weight": position_targets[row["id"]],
                            "gap_usd": gap, "gap_quantity": gap / unit_usd if unit_usd and unit_usd > 0 else None})
    cash_gap = nav * cash_weight - cash
    return {"error": None, "rows": result_rows, "nav": nav, "cash": cash,
            "cash_weight": current_cash_weight, "cash_target": cash_weight,
            "cash_gap": cash_gap, "holdings": holdings, "difference": difference,
            "buy_usd": sum(max(0, row["gap_usd"]) for row in result_rows),
            "sell_usd": sum(max(0, -row["gap_usd"]) for row in result_rows)}


def scenario_metrics(current_price, up_mode=None, up_value=None,
                     down_mode=None, down_value=None):
    """Return gain/loss magnitudes as fractions and their positive risk/reward ratio.

    A price uses the instrument's quoted currency. A percentage input is a
    positive gain or loss magnitude and does not require a current mark.
    """
    current = _finite(current_price)

    def case(mode, raw, side):
        value = _finite(raw)
        if value is None or value <= 0:
            return None, f"Enter a positive {side} case."
        if mode == "percent":
            if side == "downside" and value > 100:
                return None, "Downside loss cannot exceed 100% for a long position."
            return value / 100, None
        if mode != "price":
            return None, f"Choose Price or % for {side}."
        if current is None or current <= 0:
            return None, "Current price is unavailable for a price case."
        change = value / current - 1 if side == "upside" else 1 - value / current
        if change <= 0:
            direction = "above" if side == "upside" else "below"
            return None, f"{side.capitalize()} price must be {direction} the current price."
        return change, None

    up, up_reason = case(up_mode, up_value, "upside")
    down, down_reason = case(down_mode, down_value, "downside")
    return {"upside_pct": up, "downside_pct": down,
            "upside_price": current * (1 + up) if current is not None and current > 0 and up is not None else None,
            "downside_price": current * (1 - down) if current is not None and current > 0 and down is not None else None,
            "risk_reward": up / down if up is not None and down is not None and down > 0 else None,
            "upside_reason": up_reason, "downside_reason": down_reason}


def weighted_risk_reward(weighted_ratios):
    """Average position ratios by target weight, excluding missing ratios."""
    weighted_sum = 0.0
    included_weight = 0.0
    for weight, ratio in weighted_ratios:
        weight, ratio = _finite(weight), _finite(ratio)
        if weight is not None and weight > 0 and ratio is not None and ratio >= 0:
            weighted_sum += weight * ratio
            included_weight += weight
    return weighted_sum / included_weight if included_weight else None
