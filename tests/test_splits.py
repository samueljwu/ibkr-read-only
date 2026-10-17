"""Synthetic split factors used only for chart presentation."""

from app.finance.splits import adjusted_execution_price, ledger_split_events


def test_dated_splits_adjust_execution_price_without_changing_trade():
    events = ledger_split_events([
        {"source": "IBKR corporate action", "price": 0.0,
         "trade_date": "2024-06-12", "notes": "SPLIT 2 FOR 1; additional shares"},
        {"source": "IBKR corporate action", "price": 0.0,
         "trade_date": "2026-09-03", "notes": "SPLIT 2 FOR 1; additional shares"},
        {"source": "manual", "price": 0.0,
         "trade_date": "2025-01-01", "notes": "SPLIT 10 FOR 1"},
    ])
    assert adjusted_execution_price(120, "2024-05-01", events) == (30, 4)
    assert adjusted_execution_price(60, "2024-08-01", events) == (30, 2)
    assert adjusted_execution_price(80, "2026-09-03", events) == (80, 1)
