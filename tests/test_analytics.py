from datetime import date, timedelta

import pytest

from app.finance.analytics import (capital_flow_rows, cash_share_rows, daily_pnl_summary,
                                   performance_window_rows)
from app.storage.database import Database
from app.market.market_data import IBKRMarketDataProvider


def test_cash_share_uses_valid_completed_cash_and_nav_observations():
    snapshots = [
        {"day": "2026-01-02", "cash_usd": 0, "nav_usd": 1000, "source": "flex"},
        {"day": "2026-01-05", "cash_usd": -100, "nav_usd": 1000, "source": "flex"},
        {"day": "2026-01-06", "cash_usd": None, "nav_usd": 1000, "source": "flex"},
        {"day": "2026-01-07", "cash_usd": 100, "nav_usd": 0, "source": "flex"},
        {"day": "2026-01-08", "cash_usd": 500, "nav_usd": 1000, "source": "live"},
    ]
    rows = cash_share_rows(snapshots, date(2026, 1, 2), date(2026, 1, 8))
    assert [row["percent"] for row in rows] == [0, -10]
    assert cash_share_rows(snapshots, date(2026, 1, 8), date(2026, 1, 8))[0]["percent"] == 50


def test_selected_period_uses_prior_close_and_removes_both_flow_directions():
    snapshots = [
        {"day": "2026-01-02", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-05", "nav_usd": 1510, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-06", "nav_usd": 1300, "source": "flex", "flow_covered": 1},
    ]
    flows = [{"day": "2026-01-05", "amount_usd": 500},
             {"day": "2026-01-06", "amount_usd": -200}]
    rows, complete = performance_window_rows(
        snapshots, flows, date(2026, 1, 5), date(2026, 1, 6))
    assert complete
    assert [row["nav_change"] for row in rows] == [510, 300]
    assert [row["adjusted_change"] for row in rows] == [10, 0]
    assert [row["performance"] for row in rows] == pytest.approx(
        [10 / 1500 * 100, ((1 + 10 / 1500) * (1 - 10 / 1310) - 1) * 100])


def test_missing_prior_close_does_not_silently_rebase_selected_period():
    snapshots = [
        {"day": "2026-01-02", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-12", "nav_usd": 1200, "source": "flex", "flow_covered": 1},
    ]
    rows, complete = performance_window_rows(snapshots, [], date(2026, 1, 9), date(2026, 1, 12))
    assert not complete
    assert rows[0]["nav_change"] is None
    assert rows[0]["adjusted_change"] is None
    assert rows[0]["performance"] is None


def test_weekend_flow_keeps_its_date_and_does_not_count_as_gain():
    snapshots = [
        {"day": "2026-01-02", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-05", "nav_usd": 1500, "source": "flex", "flow_covered": 1},
    ]
    rows, complete = performance_window_rows(
        snapshots, [{"day": "2026-01-03", "amount_usd": 500}],
        date(2026, 1, 2), date(2026, 1, 5))
    assert complete
    assert [(row["day"], row["external_flow"]) for row in rows] == [
        ("2026-01-02", 0), ("2026-01-03", 500), ("2026-01-05", 0)]
    assert rows[-1]["adjusted_change"] == 0


def test_live_nav_does_not_overwrite_imported_close(tmp_path):
    db = Database(tmp_path / "account.sqlite3")
    db.save_account_history(1, "2026-01-02", 1000, 200, "imported", True)
    db.set_account_value(1, "NetLiquidation", 900, "USD", "2026-01-02T12:00:00")
    db.set_account_value(1, "NetLiquidation", 1100, "USD", "2026-01-05T12:00:00")
    snapshots, flows = db.analytics_history(1)
    assert snapshots[0]["nav_usd"] == 1000
    assert snapshots[1]["source"] == "live"
    db.conn.close()


def test_ibkr_daily_pnl_is_saved_with_live_nav(tmp_path):
    db = Database(tmp_path / "account.sqlite3")
    db.set_daily_pnl(1, 12.5, "2026-01-05T09:00:00")
    db.set_account_value(1, "NetLiquidation", 1012.5, "USD", "2026-01-05T09:01:00")
    snapshots, flows = db.analytics_history(1)
    assert snapshots[0]["daily_pnl_usd"] == 12.5
    assert snapshots[0]["source"] == "live"
    db.conn.close()


def test_flow_day_return_matches_ibkr_published_twr_formula():
    # IBKR's published account TWR divides ending NAV by beginning NAV plus flow.
    snapshots = [
        {"day": "2026-01-01", "nav_usd": 100000, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-02", "nav_usd": 160000, "source": "flex", "flow_covered": 1},
    ]
    flows = [{"day": "2026-01-02", "amount_usd": 50000}]
    rows, complete = performance_window_rows(snapshots, flows, date(2026, 1, 1), date(2026, 1, 2))
    assert complete
    assert rows[-1]["daily_pnl"] == 10000
    assert rows[-1]["daily_return"] == pytest.approx(10000 / 150000)


def test_ibkr_published_twr_example_matches_daily_and_linked_returns():
    # Public illustrative NAV and flow example in IBKR's PortfolioAnalyst white paper.
    navs = [4549863.44, 4629129.14, 4197829.64, 4278627.55, 4249124.71, 4417916.19]
    days = ["2011-09-30", *[f"2011-10-{day:02d}" for day in range(3, 8)]]
    snapshots = [{"day": day, "nav_usd": nav, "source": "flex", "flow_covered": 1}
                 for day, nav in zip(days, navs)]
    flows = [{"day": "2011-10-04", "amount_usd": -225000},
             {"day": "2011-10-07", "amount_usd": 81500}]
    rows, complete = performance_window_rows(snapshots, flows, date(2011, 9, 30), date(2011, 10, 7))
    assert complete
    assert [row["daily_return"] * 100 for row in rows[1:]] == pytest.approx(
        [1.74, -4.68, 1.92, -0.69, 2.02], abs=0.005)
    assert rows[-1]["performance"] == pytest.approx(0.14, abs=0.005)


def test_first_observation_shows_its_flow_without_counting_it_twice():
    snapshots = [
        {"day": "2026-01-02", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-01-05", "nav_usd": 1100, "source": "flex", "flow_covered": 1},
    ]
    flows = [{"day": "2026-01-02", "amount_usd": 500}]
    rows, complete = performance_window_rows(
        snapshots, flows, date(2026, 1, 2), date(2026, 1, 5))
    assert complete
    assert rows[0]["external_flow"] == 500
    assert rows[-1]["daily_pnl"] == 100
    assert rows[-1]["adjusted_change"] == 100


def test_daily_pnl_summary_uses_completed_covered_days_and_sample_sharpe():
    rows = [
        {"day": "2026-01-02", "daily_pnl": 100, "daily_return": .01, "source": "flex"},
        {"day": "2026-01-05", "daily_pnl": 80, "daily_return": .04, "source": "flex"},
        {"day": "2026-01-06", "daily_pnl": -50, "daily_return": -.02, "source": "flex"},
        {"day": "2026-01-07", "daily_pnl": -200, "daily_return": -.01, "source": "flex"},
        {"day": "2026-01-08", "daily_pnl": 999, "daily_return": .99, "source": "live"},
    ]
    summary = daily_pnl_summary(rows, True)
    assert summary["max_usd_gain"]["day"] == "2026-01-02"
    assert summary["max_usd_loss"]["day"] == "2026-01-07"
    assert summary["max_pct_gain"]["day"] == "2026-01-05"
    assert summary["max_pct_loss"]["day"] == "2026-01-06"
    assert summary["return_count"] == 4
    returns = [.01, .04, -.02, -.01]
    average = sum(returns) / len(returns)
    sample_variance = sum((value - average) ** 2 for value in returns) / 3
    assert summary["sharpe"] == pytest.approx(average / sample_variance ** .5 * 252 ** .5)
    assert daily_pnl_summary(rows, False)["max_usd_gain"] is None
    assert daily_pnl_summary(rows[:1], True)["sharpe"] is None


def test_ibkr_pnl_callback_is_scoped_to_its_account_request():
    provider = IBKRMarketDataProvider("127.0.0.1", 4001, 1, "Live", account_id="DU123")
    provider.client.pnl_request_id = 1001
    provider.client.pnl_account = "DU123"
    provider.client.pnl(1002, 99, 0, 0)
    provider.client.pnl(1001, 12.5, 0, 0)
    provider.drain()
    assert provider.other_events == [("daily_pnl", "DU123", 12.5)]


def test_spx_history_request_and_cache_survive_offline_restart(tmp_path):
    provider = IBKRMarketDataProvider("127.0.0.1", 4001, 1)
    calls = []
    provider.client.reqHistoricalData = lambda *args: calls.append(args)
    provider._request_benchmark_history()
    assert len(calls) == 1
    request_id, contract = calls[0][:2]
    assert (contract.symbol, contract.secType, contract.exchange, contract.currency) == (
        "SPX", "IND", "CBOE", "USD")
    assert calls[0][4:7] == ("1 day", "TRADES", 1)
    bar = type("Bar", (), {"date": "20260105", "close": 5050.0})()
    provider.client.historicalData(request_id, bar)
    provider.client.historicalDataEnd(request_id, "", "")
    provider.drain()
    assert provider.other_events == [
        ("benchmark_history", [{"day": "2026-01-05", "close": 5050.0}])]
    db = Database(tmp_path / "benchmark.sqlite3")
    assert db.save_benchmark_history(provider.other_events[0][1]) == 1
    db.conn.close()
    reopened = Database(tmp_path / "benchmark.sqlite3")
    assert reopened.benchmark_latest_day() == "2026-01-05"
    assert reopened.benchmark_history() == [{"day": "2026-01-05", "close": 5050.0}]
    reopened.conn.close()


def test_spx_overlay_rebases_pnl_comparison_without_changing_capital_charts(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from app.views.analytics_ui import _benchmark_window
    import app.ui as ui

    prices = [{"day": "2026-01-02", "close": 5000.0},
              {"day": "2026-01-05", "close": 5050.0},
              {"day": "2026-01-06", "close": 5100.0}]
    baseline, points = _benchmark_window(prices, date(2026, 1, 5), date(2026, 1, 6))
    assert baseline == (date(2026, 1, 2), 5000.0)
    assert [point[2] for point in points] == pytest.approx([1.0, 2.0])
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "spx-ui.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.save_benchmark_history(prices)
        for day, nav, cash in (("2026-01-02", 1000, 100),
                               ("2026-01-05", 1100, 250),
                               ("2026-01-06", 1210, 300)):
            window.db.save_account_history(1, day, nav, cash, flow_covered=True)
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"trade_date": "2026-01-05", "security_type": "STK", "side": "BUY",
             "gross_usd": 1000, "remaining_qty": 0},
            {"trade_date": "2026-01-06", "security_type": "STK", "side": "SELL",
             "gross_usd": 900, "remaining_qty": 0},
        ])
        tab = window.analytics_tab
        tab._set_dates(date(2026, 1, 5), date(2026, 1, 6))
        tab.range_combo.setCurrentText("Custom")
        tab.refresh()
        for view in ("Net deployed", "Buy / sell activity", "Cash / portfolio"):
            tab.capital_combo.setCurrentText(view)
            chart = tab.capital_chart.chart()
            assert tab.capital_chart._hover_primary
            assert tab.capital_chart._hover_benchmark
            assert "S&P 500" in tab.capital_chart._hover_benchmark[-1][1]
            assert ("Cash / NAV" if view == "Cash / portfolio" else
                    "Bought:" if view == "Buy / sell activity" else "Net deployed:") in (
                        tab.capital_chart._hover_primary[-1][1])
            assert len(chart.axes(Qt.Vertical)) == 2
            assert chart.axes(Qt.Vertical)[1].titleText() == "S&P 500 price change (%)"
            benchmark = next(series for series in chart.series()
                             if series.name() == "S&P 500 price change")
            assert [point.y() for point in benchmark.points()] == pytest.approx([1.0, 2.0])
            if view == "Cash / portfolio":
                cash = next(series for series in chart.series()
                            if series.name() == "Cash / portfolio")
                assert [point.y() for point in cash.points()] == pytest.approx(
                    [250 / 1100 * 100, 300 / 1210 * 100])
                assert chart.axes(Qt.Vertical)[0].titleText() == "Cash / portfolio NAV (%)"
                assert tab.capital_tables.currentWidget() is tab.cash_share_table
                assert tab.cash_share_table.rowCount() == 2
                assert tab.cash_share_table.item(0, 3).raw_value == pytest.approx(300 / 1210 * 100)
            else:
                assert tab.capital_tables.currentWidget() is tab.capital_table
        tab.sections.setCurrentWidget(tab.pnl_page)
        assert not tab.benchmark_toggle.isHidden()
        for metric in ("NAV", "NAV change", "Adjusted NAV change", "Adjusted performance"):
            tab.metric_combo.setCurrentText(metric)
            chart = tab.pnl_chart.chart()
            assert metric in tab.pnl_chart._hover_primary[-1][1]
            assert "S&P 500" in tab.pnl_chart._hover_benchmark[-1][1]
            assert len(chart.axes(Qt.Vertical)) == 2
            account = next(series for series in chart.series() if series.name() == metric)
            assert [point.y() for point in account.points()] == pytest.approx([0, 10])
            benchmark = next(series for series in chart.series()
                             if series.name() == "S&P 500 price change")
            assert [point.y() for point in benchmark.points()] == pytest.approx(
                [0, (5100 / 5050 - 1) * 100])
            assert chart.axes(Qt.Vertical)[0].min() == chart.axes(Qt.Vertical)[1].min()
            assert chart.axes(Qt.Vertical)[0].max() == chart.axes(Qt.Vertical)[1].max()
        assert "2026" in tab.capital_navigator._selection_label()
        assert "2026" in tab.pnl_navigator.toolTip()
        tab.benchmark_toggle.setChecked(False)
        assert len(tab.capital_chart.chart().axes(Qt.Vertical)) == 1
        assert len(tab.pnl_chart.chart().axes(Qt.Vertical)) == 1
        assert [point.y() for point in tab.pnl_chart.chart().series()[0].points()] == pytest.approx(
            [10, 21])
        assert all(series.name() != "S&P 500 price change"
                   for series in tab.capital_chart.chart().series())
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [])
        tab.refresh()
        assert [series.name() for series in tab.capital_chart.chart().series()] == [
            "Cash / portfolio"]
    finally:
        window.close()


def test_pnl_rebasing_uses_first_valid_observations():
    from app.views.analytics_ui import _rebased_pnl_points, _rebased_benchmark_window

    prices = [{"day": "2026-01-05", "close": 5050.0},
              {"day": "2026-01-06", "close": 5100.0}]
    baseline, points = _rebased_benchmark_window(
        prices, date(2026, 1, 5), date(2026, 1, 6))
    assert baseline == (date(2026, 1, 5), 5050.0)
    assert [point[2] for point in points] == pytest.approx([0, (5100 / 5050 - 1) * 100])

    rows = [{"day": "2026-01-05", "nav": 1100.0, "nav_change": 100.0,
             "adjusted_change": 80.0, "performance": 10.0},
            {"day": "2026-01-06", "nav": 1210.0, "nav_change": 210.0,
             "adjusted_change": 190.0, "performance": 21.0}]
    for field in ("nav", "nav_change", "adjusted_change", "performance"):
        assert [point[1] for point in _rebased_pnl_points(rows, field)] == pytest.approx([0, 10])
    rows[0]["nav"] = 0
    assert _rebased_pnl_points(rows, "nav") == []


def test_benchmark_history_event_updates_offline_chart_cache(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "spx-event.sqlite3"))
    window = ui.MainWindow()
    previous_market = window.market
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 4001, 7)
        window.market = provider
        provider.events.put(("benchmark_history", [
            {"day": "2026-01-02", "close": 5000.0},
            {"day": "2026-01-05", "close": 5050.0},
        ]))
        window._poll()
        assert window.db.benchmark_latest_day() == "2026-01-05"
        provider.events.put(("benchmark_error", "IBKR ERROR 354: no permission"))
        window._poll()
        assert "no permission" in window.db.metadata("ibkr_benchmark_error")
        assert window.db.benchmark_history()[0]["close"] == 5000.0
    finally:
        window.market = previous_market
        window.close()


def test_analytics_tab_uses_ibkr_observations_without_csv(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "live-ui.sqlite3"))
    window = ui.MainWindow()
    try:
        window.db.set_account_value(1, "NetLiquidation", 1025, "USD", "2026-01-05T12:00:00")
        window.db.set_daily_pnl(1, 25, "2026-01-05T12:01:00")
        window.analytics_tab.range_combo.setCurrentText("All")
        window.tabs.setCurrentWidget(window.analytics_tab)
        window.analytics_tab.refresh()
        assert not hasattr(window.analytics_tab, "import_button")
        assert [window.analytics_tab.sections.tabText(i) for i in range(4)] == [
            "P&L", "Return", "Capital deployment", "Exposure"]
        assert window.analytics_tab.pnl_table.rowCount() == 1
        assert window.analytics_tab.stats["NAV change"].text() == "$0.00"
        assert window.analytics_tab.stats["Adjusted performance"].text() == "—"
        assert window.analytics_tab.max_daily_value.text() == "—"
        assert window.analytics_tab.sharpe_value.text() == "—"
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"trade_date": "2026-01-05", "security_type": "STK", "side": "BUY",
             "gross_usd": 1000, "remaining_qty": 0},
            {"trade_date": "2026-01-06", "security_type": "STK", "side": "SELL",
             "gross_usd": 900, "remaining_qty": 0},
        ])
        window.analytics_tab.refresh()
        assert window.analytics_tab.capital_stats["Bought"].text() == "$1,000.00"
        assert window.analytics_tab.capital_stats["Sold"].text() == "$900.00"
        assert window.analytics_tab.capital_stats["Net deployed"].text() == "$100.00"
        assert window.analytics_tab.capital_table.rowCount() == 2
        chart = window.analytics_tab.capital_chart.chart()
        assert [series.name() for series in chart.series()] == ["Net deployed", "Zero"]
        assert [point.y() for point in chart.series()[0].points()] == [
            0, 0, 1000, 1000, 100, 100]
        window.analytics_tab.capital_combo.setCurrentText("Buy / sell activity")
        chart = window.analytics_tab.capital_chart.chart()
        assert sorted(series.points()[-1].y() for series in chart.series()[:-1]) == [-900, 1000]
        assert "Bought above zero" in window.analytics_tab.capital_caption.text()
        window.analytics_tab.capital_combo.setCurrentText("Net deployed")
        today = date.today()
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"trade_date": (today - timedelta(days=2)).isoformat(), "security_type": "STK",
             "side": "BUY", "gross_usd": 100, "remaining_qty": 0},
            {"trade_date": (today - timedelta(days=20)).isoformat(), "security_type": "STK",
             "side": "SELL", "gross_usd": 50, "remaining_qty": 0},
            {"trade_date": (today - timedelta(days=70)).isoformat(), "security_type": "STK",
             "side": "BUY", "gross_usd": 200, "remaining_qty": 0},
        ])
        for timeframe, expected in (("5 days", "$100.00"), ("1 month", "$50.00"),
                                    ("3 months", "$250.00")):
            window.analytics_tab.range_combo.setCurrentText(timeframe)
            assert window.analytics_tab.capital_stats["Net deployed"].text() == expected
        window.analytics_tab.range_combo.setCurrentText("5 days")
        assert window.analytics_tab.capital_stats["Net deployed"].text() == "$100.00"
        for theme in ("dark", "light"):
            window._apply_theme(theme)
            assert window.analytics_tab.pnl_chart.chart().backgroundBrush().color().isValid()
    finally:
        window.close()


def test_capital_deployed_uses_purchase_value_less_sale_proceeds_in_selected_range():
    trades = [
        {"trade_date": "2026-01-01", "security_type": "STK", "side": "BUY", "gross_usd": 5000},
        {"trade_date": "2026-01-05", "security_type": "STK", "side": "BUY", "gross_usd": 1000},
        {"trade_date": "2026-01-07", "security_type": "OPT", "side": "SELL", "gross_usd": 900},
        {"trade_date": "2026-01-07", "security_type": "CASH", "side": "BUY", "gross_usd": 6000},
        {"trade_date": "2026-01-07", "security_type": "FUT", "side": "BUY", "gross_usd": 5000},
    ]
    rows, totals = capital_flow_rows(trades, date(2026, 1, 5), date(2026, 1, 9))
    assert totals == {"bought": 1000, "sold": 900, "net": 100}
    assert [row["cumulative_net"] for row in rows] == [1000, 100]
    trades.append({"trade_date": "2026-01-08", "security_type": "STK", "side": "BUY", "gross_usd": None})
    _, totals = capital_flow_rows(trades, date(2026, 1, 5), date(2026, 1, 9))
    assert totals is None


def test_capital_activity_groups_trade_values_without_changing_net():
    from app.views.analytics_ui import _capital_activity_buckets

    rows = [
        {"day": "2026-01-05", "bought": 1000, "sold": 0},
        {"day": "2026-01-07", "bought": 0, "sold": 900},
    ]
    period, buckets = _capital_activity_buckets(rows, date(2026, 1, 5), date(2026, 1, 9))
    assert period == "day"
    assert [(bucket[3], bucket[4]) for bucket in buckets] == [(1000, 0), (0, 900)]
    period, buckets = _capital_activity_buckets(rows, date(2026, 1, 1), date(2026, 3, 1))
    assert period == "week"
    assert len(buckets) == 1
    assert buckets[0][3:] == (1000, 900)
    assert sum(bucket[3] - bucket[4] for bucket in buckets) == 100


def test_analytics_tab_shows_imported_history(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "ui.sqlite3"))
    window = ui.MainWindow()
    try:
        # Imported history as (day, NAV, flow, cash); Flex supersedes history-csv rows.
        for day, nav, flow, cash in (("2026-01-02", 1000, 0, 100),
                                     ("2026-01-05", 1510, 500, 600),
                                     ("2026-01-06", 1310, -200, 400)):
            window.db.save_account_history(1, day, nav, cash, "imported", True)
            window.db.save_external_flow(1, f"history-csv:{day}", day, flow, "net external")
        window.analytics_tab.range_combo.setCurrentText("All")
        window.tabs.setCurrentWidget(window.analytics_tab)
        window.analytics_tab.refresh()
        assert window.tabs.tabText(window.tabs.indexOf(window.analytics_tab)) == "Analytics"
        assert window.analytics_tab.pnl_table.rowCount() == 3
        assert window.analytics_tab.stats["NAV"].text() == "$1,310.00"
        assert window.analytics_tab.stats["Adjusted NAV change"].text() == "$10.00"
        assert window.analytics_tab.stats["NAV change"].text() == "$310.00"
        assert window.analytics_tab.max_daily_combo.currentText() == "Max USD gain"
        assert window.analytics_tab.max_daily_value.text() == "$10.00"
        assert window.analytics_tab.max_daily_detail.text() == "+0.67% · 05 Jan 2026"
        assert window.analytics_tab.sharpe_detail.text() == "Annualized · 2 daily returns"
        window.analytics_tab.max_daily_combo.setCurrentText("Max % gain")
        assert window.analytics_tab.max_daily_value.text() == "+0.67%"
        assert window.analytics_tab.max_daily_detail.text() == "$10.00 · 05 Jan 2026"
        window.analytics_tab.max_daily_combo.setCurrentText("Max USD loss")
        assert window.analytics_tab.max_daily_value.text() == "—"
        window.analytics_tab.max_daily_combo.setCurrentText("Max % loss")
        assert window.analytics_tab.max_daily_value.text() == "—"
        saved_view = window.analytics_tab.view_state()
        window.analytics_tab.max_daily_combo.setCurrentText("Max USD gain")
        assert window.analytics_tab.apply_view_state(saved_view)
        assert window.analytics_tab.max_daily_combo.currentText() == "Max % loss"
        window.analytics_tab.metric_combo.setCurrentText("NAV")
        chart = window.analytics_tab.pnl_chart.chart()
        assert chart.series()[0].name() == "NAV"
        assert [point.y() for point in chart.series()[0].points()] == [1000, 1510, 1310]
        assert len(chart.series()) == 3  # Deposits and withdrawals mark the raw NAV chart.
        assert chart.axes(Qt.Vertical)[0].min() > 0
        window.analytics_tab.metric_combo.setCurrentText("NAV change")
        chart = window.analytics_tab.pnl_chart.chart()
        assert len(chart.series()) == 3
        for marker, day in zip(chart.series()[1:], ("2026-01-05", "2026-01-06")):
            assert day in marker.name()
            assert marker.points()[0].x() == marker.points()[1].x()
            assert marker.points()[0].y() < marker.points()[1].y()
        window.analytics_tab.metric_combo.setCurrentText("Adjusted NAV change")
        chart = window.analytics_tab.pnl_chart.chart()
        assert [series.name() for series in chart.series()] == ["Adjusted NAV change"]
        assert len(chart.axes(Qt.Vertical)) == 1
        assert chart.localizeNumbers()
        assert chart.locale().toString(10000.0, "f", 0) == "10,000"
        assert chart.axes(Qt.Vertical)[0].titleText() == "USD"
        window.analytics_tab.metric_combo.setCurrentText("Adjusted performance")
        chart = window.analytics_tab.pnl_chart.chart()
        assert [series.name() for series in chart.series()] == ["Adjusted performance"]
        assert len(chart.axes(Qt.Vertical)) == 1
        assert chart.axes(Qt.Vertical)[0].labelFormat().endswith("%%")
        assert chart.series()[0].points()[-1].y() == pytest.approx(
            window.analytics_tab.pnl_table.item(0, 4).raw_value)
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        splitter = window.analytics_tab.pnl_splitter
        splitter.moveSplitter(400, 1)
        assert window.db.setting("analytics_pnl_splitter") == (
            splitter.saveState().toBase64().data().decode("ascii"))
    finally:
        window.close()


def test_analytics_chart_table_layouts_are_independent_and_saved(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "layouts.sqlite3"))
    window = ui.MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        window.tabs.setCurrentWidget(window.analytics_tab)
        app.processEvents()
        tab = window.analytics_tab
        assert tab.pnl_splitter.orientation() == Qt.Vertical
        assert tab.capital_splitter.orientation() == Qt.Vertical

        tab.pnl_layout_combo.setCurrentText("Side by side")
        app.processEvents()
        assert tab.pnl_splitter.orientation() == Qt.Horizontal
        assert tab.capital_splitter.orientation() == Qt.Vertical
        tab.pnl_splitter.moveSplitter(320, 1)
        horizontal_state = window.db.setting("analytics_pnl_splitter_horizontal")
        assert horizontal_state
        assert window.db.setting("analytics_pnl_splitter_orientation") == "horizontal"

        tab.pnl_layout_combo.setCurrentText("Top and bottom")
        assert tab.pnl_splitter.orientation() == Qt.Vertical
        tab.pnl_layout_combo.setCurrentText("Side by side")
        assert tab.pnl_splitter.saveState().toBase64().data().decode("ascii") == horizontal_state

        tab.sections.setCurrentWidget(tab.capital_page)
        tab.capital_layout_combo.setCurrentText("Side by side")
        assert tab.capital_splitter.orientation() == Qt.Horizontal
        tab.capital_layout_combo.setCurrentText("Top and bottom")
        assert window.db.setting("analytics_capital_splitter_orientation") == "vertical"
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        assert reopened.analytics_tab.pnl_layout_combo.currentText() == "Side by side"
        assert reopened.analytics_tab.pnl_splitter.orientation() == Qt.Horizontal
        assert reopened.analytics_tab.pnl_splitter.saveState().toBase64().data().decode("ascii") == horizontal_state
        assert reopened.analytics_tab.capital_layout_combo.currentText() == "Top and bottom"
        assert reopened.analytics_tab.capital_splitter.orientation() == Qt.Vertical
    finally:
        reopened.close()


def test_analytics_tab_opens_promptly_with_account_sized_history(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QHeaderView
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "large-ui.sqlite3"))
    window = ui.MainWindow()
    try:
        start_day = date(2024, 1, 1)
        with window.db.conn:
            window.db.conn.executemany("""INSERT INTO account_history
                (portfolio_id,day,nav_usd,source,flow_covered) VALUES (1,?,?,'imported',1)""",
                (((start_day + timedelta(days=offset)).isoformat(), 100000 + offset * 10)
                 for offset in range(500)))
        window.analytics_tab.range_combo.setCurrentText("All")
        window.tabs.setCurrentWidget(window.analytics_tab)
        app.processEvents()
        assert window.analytics_tab.pnl_table.rowCount() == 500
        assert window.analytics_tab.pnl_table.horizontalHeader().sectionResizeMode(0) == QHeaderView.Interactive
    finally:
        window.close()


def test_provisional_live_nav_does_not_change_completed_pnl_summary(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "live-after-close.sqlite3"))
    window = ui.MainWindow()
    try:
        for day, nav, source, covered in (
                ("2026-01-02", 1000, "flex", True),
                ("2026-01-05", 1010, "flex", True),
                ("2026-01-06", 1200, "live", False)):
            window.db.save_account_history(1, day, nav, source=source, flow_covered=covered)
        window.analytics_tab.range_combo.setCurrentText("All")
        window.analytics_tab.refresh()
        assert window.analytics_tab.stats["NAV change"].text() == "$10.00"
        assert window.analytics_tab.stats["Adjusted NAV change"].text() == "$10.00"
        window.analytics_tab.metric_combo.setCurrentText("NAV change")
        assert len(window.analytics_tab.pnl_chart.chart().series()[0].points()) == 2
        assert "provisional" in window.analytics_tab.pnl_table.item(0, 0).text()
    finally:
        window.close()


def test_analytics_compact_controls_and_default_columns(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "compact.sqlite3"))
    window = ui.MainWindow()
    try:
        tab = window.analytics_tab
        assert tab.date_summary.text()
        assert tab.start_date.isHidden() and tab.end_date.isHidden()
        assert [column for column in range(tab.pnl_table.columnCount())
                if tab.pnl_table.isColumnHidden(column)] == [2, 5, 6]
        assert tab.metric_combo.currentText() == "Adjusted NAV change"
        assert tab.stats["Adjusted NAV change"].property("analyticsSelected") is True
        tab.range_combo.setCurrentText("Custom")
        assert tab.date_summary.isHidden()
        assert not tab.start_date.isHidden() and not tab.end_date.isHidden()
        tab.range_combo.setCurrentText("3 months")
        assert tab.start_date.isHidden() and not tab.date_summary.isHidden()
        tab.sections.setCurrentWidget(tab.capital_page)
        assert tab.metric_combo.isHidden()
        assert not tab.capital_combo.isHidden()
        tab.sections.setCurrentWidget(tab.pnl_page)
        assert not tab.metric_combo.isHidden()
        assert tab.capital_combo.isHidden()
        window.tabs.setCurrentWidget(tab)
        for width in (800, 1200):
            window.resize(width, 750)
            window.show()
            tab.pnl_hint.show()
            app.processEvents()
            assert tab.controls_panel.parentWidget() is tab.pnl_page
            assert tab.controls_panel.isVisible()
            tab_bar_bottom = tab.sections.tabBar().mapToGlobal(
                QPoint(0, tab.sections.tabBar().height())).y()
            assert tab.controls_panel.mapToGlobal(QPoint()).y() >= tab_bar_bottom
            assert abs(tab.pnl_hint.geometry().center().y() -
                       tab.pnl_layout_combo.geometry().center().y()) < 20
            tab.sections.setCurrentWidget(tab.capital_page)
            app.processEvents()
            assert tab.controls_panel.parentWidget() is tab.capital_page
            assert tab.controls_panel.isVisible()
            assert tab.controls_panel.mapToGlobal(QPoint()).y() >= tab_bar_bottom
            assert abs(tab.capital_caption.geometry().center().y() -
                       tab.capital_layout_combo.geometry().center().y()) < 20
            tab.sections.setCurrentWidget(tab.pnl_page)
        tab.sections.setCurrentWidget(tab.exposure_page)
        assert tab.controls_panel.isHidden()
        tab.sections.setCurrentWidget(tab.pnl_page)
        assert tab.range_combo.currentText() == "3 months"
        assert tab.metric_combo.currentText() == "Adjusted NAV change"
        tab.pnl_table._show_all_columns()
        assert all(not tab.pnl_table.isColumnHidden(column)
                   for column in range(tab.pnl_table.columnCount()))
    finally:
        window.close()


def test_chart_axes_use_round_value_intervals():
    from app.views.analytics_ui import _nice_scale

    assert _nice_scale([0, 165000]) == (0, 200000, 5, 0)
    assert _nice_scale([0, .67]) == pytest.approx((0, .8, 5, 1))
    assert _nice_scale([0, 12]) == pytest.approx((0, 12.5, 6, 1))
    assert _nice_scale([1000, 1510, 1310], zero_based=False) == (1000, 1600, 4, 0)


def test_chart_drag_updates_shared_dates_and_both_tables(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "drag.sqlite3"))
    window = ui.MainWindow()
    try:
        today = date.today()
        first = today - timedelta(days=40)
        for offset in range(41):
            day = first + timedelta(days=offset)
            if day.weekday() < 5:
                window.db.save_account_history(1, day.isoformat(), 1000 + offset * 10,
                                               100, "flex", True)
        window.resize(1200, 800)
        window.tabs.setCurrentWidget(window.analytics_tab)
        window.analytics_tab.range_combo.setCurrentText("All")
        window.analytics_tab.metric_combo.setCurrentText("NAV change")
        window.show()
        app.processEvents()
        view = window.analytics_tab.pnl_chart
        area = view.chart().plotArea()
        assert area.width() > 100
        start = QPoint(int(area.left() + area.width() * .2), int(area.center().y()))
        end = QPoint(int(area.left() + area.width() * .7), int(area.center().y()))
        QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, end)
        app.processEvents()
        assert window.analytics_tab.range_combo.currentText() == "Custom"
        assert window.analytics_tab.start_date.date().toPython() > first
        assert window.analytics_tab.end_date.date().toPython() < today
        assert window.analytics_tab.pnl_table.rowCount() < 29
    finally:
        window.close()


def test_chart_hover_shows_nearest_values_without_hitting_the_line():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCharts import QChart, QDateTimeAxis, QLineSeries, QValueAxis
    from PySide6.QtCore import QDateTime, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.views.analytics_ui import RangeChartView, _timestamp

    app = QApplication.instance() or QApplication([])
    view = RangeChartView()
    chart = QChart()
    series = QLineSeries()
    first, last = _timestamp("2026-10-05"), _timestamp("2026-10-07")
    series.append(first, 10)
    series.append(last, 20)
    chart.addSeries(series)
    x_axis = QDateTimeAxis()
    x_axis.setRange(QDateTime.fromMSecsSinceEpoch(first),
                    QDateTime.fromMSecsSinceEpoch(last))
    y_axis = QValueAxis()
    y_axis.setRange(0, 25)
    chart.addAxis(x_axis, Qt.AlignBottom)
    chart.addAxis(y_axis, Qt.AlignLeft)
    series.attachAxis(x_axis)
    series.attachAxis(y_axis)
    view.setChart(chart)
    view.set_hover_data([(first, "2026-10-05\nNAV: $10.00"),
                         (last, "2026-10-07\nNAV: $20.00")],
                        [(first, "S&P 500 · 2026-10-05\nPrice change: +1.00%")])
    view.resize(800, 400)
    view.show()
    try:
        app.processEvents()
        area = chart.plotArea()
        position = QPoint(round(area.left() + area.width() * .9), round(area.top() + 12))
        QTest.mouseMove(view.viewport(), position)
        app.processEvents()
        assert view.toolTip() == ""
        assert view._hover_label.isVisible()
        assert "NAV: $20.00" in view._hover_label.text()
        assert "S&P 500" in view._hover_label.text()
        assert view._hover_line.isVisible()
        view.set_hover_data([(first, "2026-10-05\nNAV: $10.00"),
                             (last, "2026-10-07\nNAV: $20.00")],
                            primary_mode="previous")
        QTest.mouseMove(view.viewport(), QPoint(round(area.center().x()), position.y()))
        assert "NAV: $10.00" in view._hover_label.text()
        QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, position)
        assert not view._hover_label.isVisible()
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, position)
        QTest.mouseMove(view.viewport(), QPoint(2, 2))
        assert not view._hover_label.isVisible()
    finally:
        view.close()


def test_timeline_handles_resize_and_move_shared_timeframe(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QDate, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "timeline.sqlite3"))
    window = ui.MainWindow()
    try:
        today = date.today()
        first = today - timedelta(days=40)
        for offset in range(41):
            day = first + timedelta(days=offset)
            if day.weekday() < 5:
                window.db.save_account_history(1, day.isoformat(), 1000 + offset * 10,
                                               100, "flex", True)
        window.resize(1200, 800)
        window.tabs.setCurrentWidget(window.analytics_tab)
        window.show()
        window.analytics_tab._select_chart_range(
            QDate((first + timedelta(days=10)).year, (first + timedelta(days=10)).month,
                  (first + timedelta(days=10)).day),
            QDate((first + timedelta(days=20)).year, (first + timedelta(days=20)).month,
                  (first + timedelta(days=20)).day))
        app.processEvents()
        navigator = window.analytics_tab.pnl_navigator
        assert navigator.width() > 100
        old_start = window.analytics_tab.start_date.date().toPython()
        left, _ = navigator._bar()
        QTest.mousePress(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(left), 18))
        QTest.mouseMove(navigator, QPoint(round(left - 70), 18))
        QTest.mouseRelease(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(left - 70), 18))
        app.processEvents()
        assert window.analytics_tab.start_date.date().toPython() < old_start
        assert window.analytics_tab.end_date.date().toPython() == first + timedelta(days=20)
        assert window.analytics_tab.capital_navigator.start == navigator.start
        old_end = navigator.end
        _, right = navigator._bar()
        QTest.mousePress(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(right), 18))
        QTest.mouseMove(navigator, QPoint(round(right + 70), 18))
        QTest.mouseRelease(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(right + 70), 18))
        app.processEvents()
        assert navigator.end > old_end
        old_start, old_end = navigator.start, navigator.end
        left, right = navigator._bar()
        middle = round((left + right) / 2)
        QTest.mousePress(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(middle, 18))
        QTest.mouseMove(navigator, QPoint(middle + 40, 18))
        QTest.mouseRelease(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(middle + 40, 18))
        app.processEvents()
        assert navigator.start > old_start
        assert (navigator.end - navigator.start) == (old_end - old_start)
        assert window.analytics_tab.range_combo.currentText() == "Custom"
    finally:
        window.close()


def test_short_range_handles_stay_grabbable_on_long_history():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.views.analytics_ui import TimeRangeNavigator

    app = QApplication.instance() or QApplication([])
    navigator = TimeRangeNavigator()
    navigator.resize(800, 68)
    navigator.set_range(date(2022, 1, 1), date(2026, 10, 7),
                        date(2026, 10, 3), date(2026, 10, 7), "light")
    navigator.show()
    app.processEvents()
    try:
        left, right = navigator._bar()
        assert right - left >= 36
        QTest.mousePress(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(left), 18))
        QTest.mouseMove(navigator, QPoint(round(left - 20), 18))
        QTest.mouseRelease(navigator, Qt.LeftButton, Qt.NoModifier, QPoint(round(left - 20), 18))
        assert navigator.start < date(2026, 10, 3)
        assert navigator.end == date(2026, 10, 7)
    finally:
        navigator.close()
