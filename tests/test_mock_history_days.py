from datetime import date

from app.market.market_data import _mock_history_days


def test_mock_history_ends_on_last_business_day():
    days = _mock_history_days(date(2026, 10, 19))  # Monday
    assert len(days) == 270
    assert days[-1] == date(2026, 10, 16)
    assert days == sorted(set(days))
    assert all(day.weekday() < 5 for day in days)
