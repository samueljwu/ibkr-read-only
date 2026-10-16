from datetime import date

import pytest

from app.finance.calculations import (adv20, advx, choose_price, closed_lot, flow_adjusted_daily_pnl, historical_returns,
                              open_lot, position_metrics)


def test_position_daily_move_and_value():
    result = position_metrics(100, 90, 1, 102, 100, 10000)
    assert result["one_day"] == pytest.approx(0.02)
    assert result["daily_pnl"] == 200
    assert result["value"] == 10200
    assert result["portfolio_pct"] == pytest.approx(1.02)
    assert position_metrics(100, 90, 1, 102, 100, 0)["portfolio_pct"] is None


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
    assert flow_adjusted_daily_pnl(0, None, None, 100, 1, 1, [sold]) is None


def test_closed_and_open_lot_commission_allocation():
    buy = {"quantity": 100, "price": 10, "fx_to_usd": 1, "commission_usd": 5}
    sell = {"quantity": 100, "price": 12, "fx_to_usd": 1, "commission_usd": 5}
    closed = closed_lot(buy, sell, 100)
    assert closed["realized_gross"] == 200
    assert closed["realized_net"] == 190
    opened = open_lot(buy, 100, 11, 1)
    assert opened["net_unrealized"] == 95
    assert closed_lot(buy, sell, 40)["buy_commission"] == 2


def test_closed_lot_bond_accrued_interest_hand_verified_example():
    """Buy 10 bonds at clean 95 paying $30 accrued interest; sell at clean 97
    receiving $5 accrued interest. Clean-price gain is $20, interest transfer is
    -$25, and the combined cash result is -$5."""
    buy = {"quantity": 10, "price": 95, "fx_to_usd": 1, "multiplier": 1,
           "commission_usd": 0, "accrued_interest_usd": -30}
    sell = {"quantity": 10, "price": 97, "fx_to_usd": 1, "multiplier": 1,
            "commission_usd": 0, "accrued_interest_usd": 5}
    closed = closed_lot(buy, sell, 10)
    assert closed["realized_gross"] == pytest.approx(20)
    assert closed["realized_net"] == pytest.approx(20)
    assert closed["buy_accrued_interest"] + closed["sell_accrued_interest"] == pytest.approx(-25)
    assert closed["realized_net"] + closed["buy_accrued_interest"] + closed["sell_accrued_interest"] == pytest.approx(-5)
    assert closed["realized_pct"] == pytest.approx(20 / 950)


def test_open_lot_bond_accrued_interest_hand_verified_example():
    """Buy 10 bonds at clean 95 with a $2 commission and $30 accrued interest paid;
    mark at clean 97. Clean-price P&L is $18; the full return also needs the
    current accrued receivable, which is not part of a clean quote."""
    buy = {"quantity": 10, "price": 95, "fx_to_usd": 1, "multiplier": 1,
           "commission_usd": 2, "accrued_interest_usd": -30}
    opened = open_lot(buy, 10, 97, 1)
    assert opened["current_value"] == pytest.approx(970)
    assert opened["net_unrealized"] == pytest.approx(18)
    assert opened["buy_accrued_interest"] == pytest.approx(-30)
    assert opened["unrealized_pct"] == pytest.approx(18 / 952)


def test_closed_and_open_lot_default_zero_accrued_interest_when_absent():
    """Non-bond trades (and pre-migration rows) omit accrued_interest_usd entirely;
    results must be identical to the commission-only calculation."""
    buy = {"quantity": 100, "price": 10, "fx_to_usd": 1, "commission_usd": 5}
    sell = {"quantity": 100, "price": 12, "fx_to_usd": 1, "commission_usd": 5}
    assert closed_lot(buy, sell, 100)["realized_net"] == 190
    assert open_lot(buy, 100, 11, 1)["net_unrealized"] == 95


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


def test_price_fallback_and_adv():
    assert choose_price({"last": 0, "close": 100}) == (100, "CLOSE")
    assert choose_price({"delayed_last": 101}, "Delayed") == (101, "DELAYED LAST")
    assert choose_price({}) == (None, "")
    history = [{"volume": 1000} for _ in range(20)]
    assert adv20(history) == 1000
    assert advx(-100, 1000) == pytest.approx(0.1)
    assert adv20(history[:19]) is None
