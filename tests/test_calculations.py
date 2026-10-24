from datetime import date

import pytest

from app.finance.calculations import (adv20, advx, choose_price, closed_lot, flow_adjusted_daily_pnl, historical_returns,
                              open_lot, position_metrics, prepare_history)




def test_daily_pnl_accounts_for_same_day_buy_and_sale():
    bought = {"side": "BUY", "quantity": 10, "price": 105, "fx_to_usd": 1,
              "multiplier": 1, "commission_usd": 1}
    sold = {"side": "SELL", "quantity": 5, "price": 107, "fx_to_usd": 1,
            "multiplier": 1, "commission_usd": 1}
    assert flow_adjusted_daily_pnl(10, 107, 1, 100, 1, 1, [bought]) == 19
    assert flow_adjusted_daily_pnl(5, 108, 1, 100, 1, 1, [sold]) == 74


def test_daily_pnl_for_closed_positions_needs_only_prices_with_nonzero_exposure():
    sold = {"side": "SELL", "quantity": 10, "price": 110, "fx_to_usd": 1,
            "multiplier": 1, "commission_usd": 1}
    assert flow_adjusted_daily_pnl(0, None, None, 100, 1, 1, [sold]) == 99
    bought = {"side": "BUY", "quantity": 10, "price": 100, "fx_to_usd": 1,
              "multiplier": 1, "commission_usd": 1}
    assert flow_adjusted_daily_pnl(0, None, None, None, None, 1, [bought, sold]) == 98
    sold["commission_complete"] = False
    assert flow_adjusted_daily_pnl(0, None, None, 100, 1, 1, [sold]) == 100










def test_history_uses_previous_sessions_for_month_and_year():
    history = [
        {"day": "2025-12-31", "close": 100, "volume": 1000},
        {"day": "2026-01-30", "close": 102, "volume": 1000},
        {"day": "2026-09-30", "close": 100, "volume": 1000},
        {"day": "2026-10-05", "close": 104, "volume": 1000},
    ]
    returns = historical_returns(105, history, date(2026, 10, 6))
    assert returns["mtd"] == pytest.approx(0.05)
    assert historical_returns(120, history, date(2026, 10, 6))["ytd"] == pytest.approx(0.20)
    assert returns["1m"] == pytest.approx(105 / 102 - 1)  # closest prior completed session


@pytest.mark.parametrize("as_of, expected", [
    (date(2026, 3, 31), (date(2026, 2, 28), date(2025, 12, 31), date(2025, 9, 30))),
    (date(2024, 3, 31), (date(2024, 2, 29), date(2023, 12, 31), date(2023, 9, 30))),
    (date(2026, 1, 30), (date(2025, 12, 30), date(2025, 10, 30), date(2025, 7, 30))),
])
def test_history_month_offsets_clamp_at_month_end(as_of, expected):
    _, targets = prepare_history([], as_of)
    assert tuple(targets[window] for window in ("1m", "3m", "6m")) == expected
