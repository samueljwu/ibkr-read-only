"""All financial conventions used by the pilot live here."""

from calendar import monthrange
from datetime import date, timedelta
from math import fsum, isfinite
from decimal import Decimal, InvalidOperation
import re


def looks_like_option_ticker(display_ticker):
    """Safety check only; this is not a Bloomberg-to-IBKR contract converter."""
    return bool(re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\s+[CP]\d+(?:\.\d+)?\b",
                          str(display_ticker or ""), re.IGNORECASE))


def valid_price(value):
    try:
        value = float(value)
        return value if isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def valid_multiplier(value):
    return valid_price(value)


def choose_price(ticks, mode="Live"):
    """Return (price, source); a missing price is never converted to zero."""
    if mode == "Delayed":
        candidates = [("delayed_last", "DELAYED LAST"), ("delayed_close", "CLOSE")]
    else:
        label = "FROZEN LAST" if mode == "Frozen" else "LIVE LAST"
        candidates = [("last", label), ("close", "CLOSE"),
                      ("delayed_last", "DELAYED LAST"), ("delayed_close", "CLOSE")]
    for key, source in candidates:
        price = valid_price(ticks.get(key))
        if price is not None:
            return price, source
    return None, ""


def ratio(current, reference):
    current, reference = valid_price(current), valid_price(reference)
    return current / reference - 1 if current and reference else None


def position_metrics(qty, average_cost, fx, current, prior_close, nav, multiplier=1):
    qty, average_cost = float(qty or 0), float(average_cost or 0)
    fx = valid_price(fx)
    multiplier = valid_multiplier(multiplier)
    current = valid_price(current)
    prior_close = valid_price(prior_close)
    usable = current is not None and fx is not None and multiplier is not None
    value = qty * current * fx * multiplier if usable else None
    return {
        "daily_move": current - prior_close if current and prior_close else None,
        "daily_pnl": qty * (current - prior_close) * fx * multiplier
        if usable and prior_close else None,
        "one_day": ratio(current, prior_close),
        "value": value,
        "portfolio_pct": value / float(nav) if value is not None and nav and float(nav) != 0 else None,
        "unrealized_pnl": qty * (current - average_cost) * fx * multiplier if usable else None,
        "unrealized_pct": (current / average_cost - 1) * (1 if qty >= 0 else -1)
        if current and average_cost > 0 else None,
    }


def adv20(history):
    volumes = [float(row["volume"]) for row in history[-20:] if row.get("volume") is not None]
    return fsum(volumes) / 20 if len(volumes) == 20 else None


def advx(qty, adv):
    return abs(float(qty or 0)) / adv if adv and adv > 0 else None


def prepare_history(history, as_of):
    """Parse completed daily bars and the comparison dates once for one instrument.

    Reusing the result across the rows or lots of the same instrument avoids
    re-parsing identical bars on every lookup.
    """
    bars = []
    for row in history:
        day = date.fromisoformat(str(row["day"]))
        close = row["close"]
        if day < as_of and valid_price(close):
            bars.append((day, float(close)))
    bars.sort()
    targets = {
        "1m": _months_before(as_of, 1),
        "3m": _months_before(as_of, 3),
        "6m": _months_before(as_of, 6),
        "mtd": as_of.replace(day=1) - timedelta(days=1),
        "ytd": date(as_of.year - 1, 12, 31),
    }
    return bars, targets


def _months_before(day, months):
    month_index = day.year * 12 + day.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def window_returns(current, bars, targets):
    """Return the 5d/1m/3m/6m/mtd/ytd changes for a history prepared once."""
    if not bars:
        return {key: None for key in ("5d", "1m", "3m", "6m", "mtd", "ytd")}

    def on_or_before(target):
        return next((close for day, close in reversed(bars) if day <= target), None)

    reference = {
        "5d": bars[-5][1] if len(bars) >= 5 else None,
        "1m": on_or_before(targets["1m"]),
        "3m": on_or_before(targets["3m"]),
        "6m": on_or_before(targets["6m"]),
        "mtd": on_or_before(targets["mtd"]),
        "ytd": on_or_before(targets["ytd"]),
    }
    return {key: ratio(current, value) for key, value in reference.items()}


def historical_returns(current, history, as_of=None):
    """History contains completed daily bars only; reference dates use prior sessions."""
    as_of = as_of or date.today()
    bars, targets = prepare_history(history, as_of)
    return window_returns(current, bars, targets)


def closed_lot(buy, sell, qty):
    """Realized clean-price P&L; report bond interest transfers separately."""
    qty = float(qty)
    buy_multiplier = valid_multiplier(buy.get("multiplier", 1))
    sell_multiplier = valid_multiplier(sell.get("multiplier", 1))
    buy_fx, sell_fx = valid_price(buy.get("fx_to_usd")), valid_price(sell.get("fx_to_usd"))
    if (buy_multiplier is None or sell_multiplier is None or buy_multiplier != sell_multiplier or
        buy_fx is None or sell_fx is None):
        return {key: None for key in ("buy_value_usd", "sell_value_usd", "buy_commission",
                                      "sell_commission", "buy_accrued_interest", "sell_accrued_interest",
                                      "realized_gross", "realized_net", "realized_pct")}
    buy_value = qty * float(buy["price"]) * buy_multiplier * buy_fx
    sell_value = qty * float(sell["price"]) * sell_multiplier * sell_fx
    buy_comm = float(buy["commission_usd"] or 0) * qty / float(buy["quantity"])
    sell_comm = float(sell["commission_usd"] or 0) * qty / float(sell["quantity"])
    buy_accrued = float(buy.get("accrued_interest_usd") or 0) * qty / float(buy["quantity"])
    sell_accrued = float(sell.get("accrued_interest_usd") or 0) * qty / float(sell["quantity"])
    gross = sell_value - buy_value
    net = gross - buy_comm - sell_comm
    return {"buy_value_usd": buy_value, "sell_value_usd": sell_value,
            "buy_commission": buy_comm, "sell_commission": sell_comm,
            "buy_accrued_interest": buy_accrued, "sell_accrued_interest": sell_accrued,
            "realized_gross": gross, "realized_net": net,
            "realized_pct": net / (buy_value + buy_comm)
            if buy_value + buy_comm else None}


def open_lot(buy, qty, current, current_fx):
    """Open clean-price P&L; a bond's accrued receivable is separate."""
    qty = float(qty)
    current = valid_price(current)
    multiplier = valid_multiplier(buy.get("multiplier", 1))
    buy_fx, current_fx = valid_price(buy.get("fx_to_usd")), valid_price(current_fx)
    if multiplier is None or buy_fx is None or current_fx is None:
        return {key: None for key in ("buy_value_usd", "buy_commission", "buy_accrued_interest",
                                      "current_value", "net_unrealized", "unrealized_pct")}
    buy_value = qty * float(buy["price"]) * multiplier * buy_fx
    buy_comm = float(buy["commission_usd"] or 0) * qty / float(buy["quantity"])
    buy_accrued = float(buy.get("accrued_interest_usd") or 0) * qty / float(buy["quantity"])
    gross = qty * multiplier * (current * current_fx - float(buy["price"]) * buy_fx) if current else None
    net = gross - buy_comm if gross is not None else None
    return {"buy_value_usd": buy_value, "buy_commission": buy_comm,
            "buy_accrued_interest": buy_accrued,
            "current_value": qty * multiplier * current * current_fx if current else None,
            "net_unrealized": net,
            "unrealized_pct": net / (buy_value + buy_comm)
            if net is not None and buy_value + buy_comm else None}


def flow_adjusted_daily_pnl(current_qty, current_price, current_fx, prior_price, prior_fx,
                            multiplier, today_trades):
    """Today's market-value change plus net trade cash flow, in USD."""
    try:
        current_price, prior_price = valid_price(current_price), valid_price(prior_price)
        current_fx, prior_fx = valid_price(current_fx), valid_price(prior_fx)
        multiplier = valid_multiplier(multiplier)
        if multiplier is None:
            return None
        prior_qty = Decimal(str(current_qty))
        trade_cash = Decimal(0)
        for trade in today_trades:
            if not trade.get("commission_complete", True):
                return None
            fx = valid_price(trade.get("fx_to_usd"))
            trade_mult = valid_multiplier(trade.get("multiplier"))
            if fx is None or trade_mult != multiplier:
                return None
            units = Decimal(str(trade["quantity"]))
            sign = 1 if trade["side"] == "SELL" else -1
            prior_qty -= units if trade["side"] == "BUY" else -units
            trade_cash += (sign * units * Decimal(str(trade["price"])) * Decimal(str(fx)) *
                           Decimal(str(multiplier)) - Decimal(str(trade["commission_usd"] or 0)))
        current_qty = Decimal(str(current_qty))
        if current_qty and (current_price is None or current_fx is None):
            return None
        if prior_qty and (prior_price is None or prior_fx is None):
            return None
        current_value = (current_qty * Decimal(str(current_price)) * Decimal(str(current_fx)) *
                         Decimal(str(multiplier)) if current_qty else Decimal(0))
        prior_value = (prior_qty * Decimal(str(prior_price)) * Decimal(str(prior_fx)) *
                       Decimal(str(multiplier)) if prior_qty else Decimal(0))
        return float(current_value - prior_value + trade_cash)
    except (TypeError, ValueError, InvalidOperation, ArithmeticError):
        return None
