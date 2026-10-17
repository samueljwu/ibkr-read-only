"""Dated stock split factors for comparing executions with split-adjusted bars."""

import re
from datetime import date
from decimal import Decimal


_LEDGER_SPLIT = re.compile(r"\bSPLIT\s+(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)\b", re.I)


def _factor(numerator, denominator):
    top, bottom = Decimal(numerator), Decimal(denominator)
    if top <= 0 or bottom <= 0 or top == bottom:
        raise ValueError("Split ratio must contain two different positive numbers")
    return top / bottom


def ledger_split_events(trades):
    """Read imported corporate actions without rewriting their zero-cost trade rows."""
    events = {}
    for row in trades:
        if row.get("source") != "IBKR corporate action" or row.get("price") != 0:
            continue
        match = _LEDGER_SPLIT.search(row.get("notes") or "")
        if match:
            day = date.fromisoformat(row["trade_date"]).isoformat()
            events[day] = _factor(match[1], match[2])
    return events


def adjusted_execution_price(price, trade_day, events):
    """Return chart price and cumulative factor for splits after the execution."""
    factor = Decimal(1)
    for day, ratio in events.items():
        if day > trade_day:
            factor *= ratio
    return float(Decimal(str(price)) / factor), float(factor)
