from datetime import date, timedelta

import pytest

from app.finance.analytics import (capital_flow_rows, flex_monthly_history,
                                   performance_window_rows)
from app.storage.database import Database
from app.market.market_data import IBKRMarketDataProvider


def test_chart_colors_recolor_series_and_persist(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    QApplication.instance() or QApplication([])
    path = tmp_path / "chart-colors.sqlite3"
    monkeypatch.setattr(ui, "Database", lambda: Database(path))
    window = ui.MainWindow()
    try:
        for day, nav in (("2026-01-02", 1000), ("2026-01-05", 1100)):
            window.db.save_account_history(1, day, nav, 100, flow_covered=True)
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"trade_date": "2026-01-05", "security_type": "STK", "side": "BUY",
             "gross_usd": 100, "remaining_qty": 0},
            {"trade_date": "2026-01-05", "security_type": "STK", "side": "SELL",
             "gross_usd": 60, "remaining_qty": 0},
        ])
        tab = window.analytics_tab
        tab._set_dates(date(2026, 1, 2), date(2026, 1, 5))
        tab.range_combo.setCurrentText("Custom")
        tab.benchmark_toggle.setChecked(False)
        tab.sections.setCurrentWidget(tab.pnl_page)
        tab.refresh()
        tab.chart_colors.pickers["pnl"].swatch_buttons["#b87b31"].click()
        assert tab.pnl_chart.chart().series()[0].pen().color().name() == "#b87b31"

        tab.sections.setCurrentWidget(tab.capital_page)
        tab.capital_combo.setCurrentText("Buy / sell activity")
        assert set(tab.chart_colors.pickers["bought"].preset_actions) == {"default"}
        tab.chart_colors.pickers["bought"].swatch_buttons["#2a9293"].click()
        tab.chart_colors.pickers["sold"].hex_input.setText("#ab42cd")
        tab.chart_colors.pickers["sold"].hex_input.returnPressed.emit()
        bought = next(series for series in tab.capital_chart.chart().series()
                      if series.name().startswith("Bought "))
        sold = next(series for series in tab.capital_chart.chart().series()
                    if series.name().startswith("Sold "))
        assert bought.pen().color().name() == "#2a9293"
        assert sold.pen().color().name() == "#ab42cd"
        assert window.db.setting("analytics_chart_color_1_bought_palette") == "solid"
        window.db.set_setting("analytics_chart_color_1_cash_share_palette", "ocean")
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        assert reopened.analytics_tab.chart_colors._selection["pnl"] == ("solid", "#b87b31")
        assert reopened.analytics_tab.chart_colors._selection["bought"] == ("solid", "#2a9293")
        assert reopened.analytics_tab.chart_colors._selection["sold"] == ("solid", "#ab42cd")
        assert reopened.analytics_tab.chart_colors._selection["cash_share"][0] == "default"
        assert reopened.db.setting("analytics_chart_color_1_cash_share_palette") == "default"
    finally:
        reopened.close()


def test_pnl_calendar_intervals_use_covered_days_and_selected_range():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QComboBox
    from app.views.pnl_calendar import PnlCalendar

    app = QApplication.instance() or QApplication([])
    calendar = PnlCalendar()
    assert isinstance(calendar.usd_decimals.dropdown, QComboBox)
    assert calendar.usd_decimals.dropdown.objectName() == "toolbarDropdown"
    assert calendar.usd_decimals.dropdown.width() >= 80
    assert len(calendar.day_cells) == 30
    assert calendar.daily_grid.grid.columnCount() == 5
    calendar.set_data([], date(2026, 1, 1), date(2026, 10, 18), True)
    calendar.resize(750, 600)
    calendar.show()
    app.processEvents()
    assert calendar.minimumSizeHint().width() <= 750
    toolbar = calendar.toolbar
    navigator = calendar.navigator
    assert navigator.previous_button.text() == ""
    assert navigator.next_button.text() == ""
    assert navigator.previous_button.objectName() == "secondary"
    assert navigator.next_button.objectName() == "secondary"
    assert navigator.previous_button.accessibleName() == "Previous period"
    assert navigator.next_button.accessibleName() == "Next period"
    assert not navigator.previous_button.icon().isNull()
    assert not navigator.next_button.icon().isNull()
    assert toolbar.isAncestorOf(calendar.usd_decimals)
    assert toolbar.isAncestorOf(calendar.percent_decimals)
    controls = (toolbar.interval_combo, calendar.usd_decimals, calendar.percent_decimals)
    positions = [widget.mapTo(toolbar, widget.rect().topLeft()) for widget in controls]
    assert [(point.y(), point.x()) for point in positions] == sorted(
        (point.y(), point.x()) for point in positions)
    assert all(point.x() + widget.width() <= toolbar.width()
               for widget, point in zip(controls, positions))
    assert calendar.usd_decimals.caption.text() == "USD decimals"
    assert calendar.percent_decimals.caption.text() == "P&L % decimals"
    assert navigator.geometry().top() >= toolbar.geometry().bottom()
    rows = [
        {"day": "2026-01-02", "daily_pnl": 12, "daily_return": .012, "source": "flex"},
        {"day": "2026-01-05", "daily_pnl": -4, "daily_return": -.009, "source": "flex"},
        {"day": "2026-01-06", "daily_pnl": None, "daily_return": None, "source": "live"},
    ]
    calendar.set_data(rows, date(2026, 1, 2), date(2026, 1, 31), True)
    assert "+$12.00" in calendar.day_cells[4].text()
    assert "+1.20%" in calendar.day_cells[4].text()
    assert "-$4.00" in calendar.day_cells[5].text()
    assert "-0.90%" in calendar.day_cells[5].text()
    assert "Provisional" in calendar.day_cells[6].text()
    assert "2 covered days" in calendar.summary_label.text()
    assert "P&L %: +0.29%" in calendar.summary_label.text()
    for interval, cell_index in (("Weekly", 1), ("Monthly", 0),
                                 ("Quarterly", 0), ("Yearly", 0)):
        calendar.interval_combo.setCurrentText(interval)
        assert "+$8.00" in calendar.summary_label.text()
        assert "+0.29%" in calendar.summary_label.text()
        if interval == "Weekly":
            assert "-$4.00" in calendar.period_cells[cell_index].text()
            assert "-0.90%" in calendar.period_cells[cell_index].text()
        else:
            assert "+$8.00" in calendar.period_cells[cell_index].text()
            assert "+0.29%" in calendar.period_cells[cell_index].text()
    calendar.usd_decimals.setValue(0)
    calendar.percent_decimals.setValue(3)
    assert "+$8" in calendar.period_cells[0].text()
    assert "+0.289%" in calendar.period_cells[0].text()
    assert "Net +$8 · P&L %: +0.289%" in calendar.summary_label.text()
    assert "Compounded P&L %: +0.289%" in calendar.period_cells[0].toolTip()
    calendar.set_data(rows, date(2026, 1, 2), date(2026, 1, 31), False)
    assert "No covered daily P&L" in calendar.summary_label.text()
    assert "$8.00" not in calendar.period_cells[0].text()
    assert "0.29%" not in calendar.period_cells[0].text()
    calendar.close()


def test_pnl_calendar_daily_arrows_move_one_month():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.views.pnl_calendar import PnlCalendar

    app = QApplication.instance() or QApplication([])
    calendar = PnlCalendar()
    calendar.set_data([], date(2026, 7, 1), date(2026, 9, 30), True)
    assert calendar.period_label.text() == "September 2026"
    assert not calendar.next_button.isEnabled()
    calendar.previous_button.click()
    assert calendar.period_label.text() == "August 2026"
    calendar.previous_button.click()
    assert calendar.period_label.text() == "July 2026"
    assert not calendar.previous_button.isEnabled()
    calendar.next_button.click()
    assert calendar.period_label.text() == "August 2026"
    calendar.close()




def test_pnl_metrics_select_charts_and_display_control_switches_views(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import app.ui as ui
    from app.views.metric_summary import MetricSummary
    from app.views.typography import install_app_font

    app = QApplication.instance() or QApplication([])
    install_app_font(app)
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "pnl-controls.sqlite3"))
    window = ui.MainWindow()
    try:
        tab = window.analytics_tab
        window.tabs.setCurrentWidget(tab)
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        assert tab.pnl_summary_panel._columns == 6, tab.pnl_summary_panel.width()
        assert tab.pnl_hint.isVisible()
        assert (tab.pnl_hint.mapToGlobal(tab.pnl_hint.rect().topLeft()).y() ==
                tab.date_summary.mapToGlobal(tab.date_summary.rect().topLeft()).y())
        assert tab.pnl_summary_details.height() < 30
        tab.as_of_label.setText("NAV close · 22 Oct 2026")
        tab.sync_status.setText("History · 2026-10-24")
        tab.pnl_hint.setText("Later live NAV is provisional.")
        tab._show_pnl_hint(True)
        app.processEvents()
        assert len({label.mapToGlobal(label.rect().topLeft()).y() for label in (
            tab.date_summary, tab.as_of_label, tab.sync_status, tab.pnl_hint)}) == 1
        figure_to_detail_gaps = []
        summary_heights = []
        for page, detail in ((tab.pnl_page, tab.pnl_summary_details),
                             (tab.return_page, tab.return_page.scope),
                             (tab.capital_page, tab.capital_date_summary),
                             (tab.exposure_page, tab.exposure_page.status)):
            tab.sections.setCurrentWidget(page)
            app.processEvents()
            summary = page.findChild(MetricSummary)
            value = summary._blocks[0].value
            summary_heights.append(summary.height())
            figure_to_detail_gaps.append(
                detail.mapToGlobal(detail.rect().topLeft()).y() -
                value.mapToGlobal(value.rect().bottomLeft()).y())
        assert len(set(summary_heights)) == 1
        assert len(set(figure_to_detail_gaps)) == 1
        tab.sections.setCurrentWidget(tab.pnl_page)
        window.resize(760, 900)
        app.processEvents()
        assert tab.pnl_summary_panel._columns == 2
        grid = tab.pnl_summary_panel.grid
        assert grid.itemAtPosition(0, 0).widget() is tab.metric_buttons["NAV"]
        assert grid.itemAtPosition(0, 1).widget() is tab.metric_buttons["NAV change"]
        assert grid.itemAtPosition(1, 0).widget() is tab.metric_buttons["Adjusted NAV change"]
        assert grid.itemAtPosition(1, 1).widget() is tab.metric_buttons["Adjusted performance"]
        window.resize(1400, 900)
        app.processEvents()
        assert tab.pnl_summary_panel._columns == 6
        QTest.mouseClick(tab.metric_buttons["NAV"], Qt.LeftButton)
        assert tab.pnl_metric == "NAV"
        assert tab.metric_buttons["NAV"].caption.property("metricSelected") is True
        button = tab.metric_buttons["Adjusted performance"]
        button.setFocus()
        QTest.keyClick(button, Qt.Key_Space)
        assert tab.pnl_metric == "Adjusted performance"
        assert sum(item.isChecked() for item in tab.metric_buttons.values()) == 1

        tab.pnl_layout_combo.setCurrentIndex(tab.pnl_layout_combo.findData("horizontal"))
        assert tab.pnl_splitter.orientation() == Qt.Horizontal
        assert window.db.setting("analytics_pnl_splitter_orientation") == "horizontal"
        tab.pnl_display_combo.setCurrentText("Calendar")
        assert tab.pnl_views.currentWidget() is tab.pnl_calendar
        assert tab.pnl_summary_panel.isHidden()
        assert tab.pnl_summary_details.isHidden()
        assert tab.pnl_hint.isHidden()
        tab.pnl_display_combo.setCurrentText("Chart and table")
        tab.pnl_layout_combo.setCurrentIndex(tab.pnl_layout_combo.findData("vertical"))
        assert tab.pnl_splitter.orientation() == Qt.Vertical
        assert not tab.pnl_summary_panel.isHidden()
        assert not tab.pnl_summary_details.isHidden()
        assert tab.pnl_hint.isVisible()

        tab.sections.setCurrentWidget(tab.capital_page)
        assert tab.pnl_page.isAncestorOf(tab.benchmark_toggle)
        assert not tab.pnl_display_combo.isVisible()
        assert tab.capital_caption.isHidden()
        assert tab.capital_info.toolTip() == tab.capital_caption.text()
        assert tab.capital_page.isAncestorOf(tab.capital_benchmark_toggle)
        tab.capital_controls.range_combo.setCurrentText("1 month")
        assert tab.range_combo.currentText() == "1 month"
        assert tab.capital_controls.start_date.date() == tab.start_date.date()
        tab.capital_controls.start_date.setDate(tab.start_date.date().addDays(1))
        assert tab.range_combo.currentText() == "Custom"
        assert tab.capital_controls.start_date.date() == tab.start_date.date()
        tab.capital_benchmark_toggle.setChecked(False)
        assert not tab.benchmark_toggle.isChecked()
        tab.sections.setCurrentWidget(tab.pnl_page)
        assert tab.pnl_page.isAncestorOf(tab.benchmark_toggle)
        assert tab.pnl_display_combo.isVisible()
        assert tab.pnl_metric == "Adjusted performance"
    finally:
        window.close()


def test_pnl_metric_dropdown_text_and_selection_styling(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QLabel
    import app.ui as ui
    from app.views.ui_theme import COLORS

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "pnl-metric-style.sqlite3"))
    window = ui.MainWindow()
    try:
        tab = window.analytics_tab
        window.tabs.setCurrentWidget(tab)
        window.resize(1400, 900)
        window.show()
        tab.set_pnl_metric("NAV")
        for theme in ("light", "dark"):
            window._apply_theme(theme)
            app.processEvents()
            values = [*tab.stats.values(), tab.max_daily_value, tab.sharpe_value]
            assert len({(value.font().pixelSize(), value.font().weight()) for value in values}) == 1
            captions = tab.pnl_summary_panel.findChildren(QLabel, "metricLabel")
            caption_fonts = [tab.max_daily_combo.font(), *(caption.font() for caption in captions)]
            assert len({(font.pixelSize(), font.weight()) for font in caption_fonts}) == 1
            selected = tab.metric_buttons["NAV"].grab().toImage()
            other = tab.metric_buttons["NAV change"].grab().toImage()
            assert selected.pixelColor(1, selected.height() // 2).name() == COLORS[theme]["selection_border"]
            assert other.pixelColor(1, other.height() // 2).name() != COLORS[theme]["selection_border"]
            combo = tab.max_daily_combo
            combo.showPopup()
            app.processEvents()
            text_width = max(combo.view().fontMetrics().horizontalAdvance(combo.itemText(index))
                             for index in range(combo.count()))
            assert combo.view().viewport().width() >= text_width + 16
            combo.hidePopup()
    finally:
        window.close()


def test_pnl_calendar_switch_and_interval_restore_in_analytics(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import app.ui as ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ui, "Database", lambda: Database(tmp_path / "calendar.sqlite3"))
    window = ui.MainWindow()
    try:
        for day, nav in (("2025-12-31", 1000), ("2026-01-02", 1000),
                         ("2026-01-05", 1012)):
            window.db.save_account_history(1, day, nav, source="flex", flow_covered=True)
        tab = window.analytics_tab
        tab.range_combo.setCurrentText("Custom")
        tab._set_dates(date(2026, 1, 2), date(2026, 1, 5))
        tab.refresh()
        tab.pnl_display_combo.setCurrentText("Calendar")
        window.tabs.setCurrentWidget(tab)
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        assert tab.pnl_calendar.interval_combo.isVisible()
        for group in tab.pnl_calendar_controls:
            assert group.isVisible()
            assert group.geometry().right() < tab.pnl_controls.width()
        assert tab.pnl_controls.isVisible()
        assert tab.pnl_summary_panel.isHidden()
        assert not tab.range_combo.isVisible()
        assert not tab.date_summary.isVisible()
        assert tab.pnl_calendar._first == date(2025, 12, 31)
        assert tab.pnl_calendar.previous_button.isEnabled()
        tab.pnl_calendar.interval_combo.setCurrentText("Weekly")
        assert tab.pnl_views.currentWidget() is tab.pnl_calendar
        assert "+$12.00" in tab.pnl_calendar.summary_label.text()
        assert "P&L %: +1.20%" in tab.pnl_calendar.summary_label.text()
        tab.pnl_calendar.usd_decimals.setValue(0)
        tab.pnl_calendar.percent_decimals.setValue(3)
        assert "Net +$12 · P&L %: +1.200%" in tab.pnl_calendar.summary_label.text()
        state = tab.view_state()
        tab.pnl_display_combo.setCurrentText("Chart and table")
        assert not tab.pnl_calendar.interval_combo.isVisible()
        assert tab.pnl_controls.isVisible()
        assert not tab.pnl_summary_panel.isHidden()
        assert tab.range_combo.isVisible()
        assert tab.range_combo.currentText() == "Custom"
        tab.pnl_calendar.interval_combo.setCurrentText("Daily")
        tab.pnl_calendar.usd_decimals.setValue(2)
        tab.pnl_calendar.percent_decimals.setValue(2)
        assert tab.apply_view_state(state)
        assert tab.pnl_display_combo.currentText() == "Calendar"
        assert tab.pnl_calendar.interval_combo.currentText() == "Weekly"
        assert (tab.pnl_calendar.usd_decimals.value(),
                tab.pnl_calendar.percent_decimals.value()) == (0, 3)
    finally:
        window.close()

    reopened = ui.MainWindow()
    try:
        assert (reopened.analytics_tab.pnl_calendar.usd_decimals.value(),
                reopened.analytics_tab.pnl_calendar.percent_decimals.value()) == (0, 3)
    finally:
        reopened.close()


def test_pnl_calendar_shows_eight_quarters_per_page():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.views.pnl_calendar import PnlCalendar

    app = QApplication.instance() or QApplication([])
    calendar = PnlCalendar()
    rows = [
        {"day": "2026-01-05", "daily_pnl": 10, "daily_return": .01, "source": "flex"},
        {"day": "2027-12-31", "daily_pnl": -3, "daily_return": -.003, "source": "flex"},
        {"day": "2028-01-03", "daily_pnl": 7, "daily_return": .007, "source": "flex"},
    ]
    calendar.set_data(rows, date(2026, 1, 1), date(2026, 1, 31), True)
    calendar.set_data(rows, date(2026, 1, 1), date(2028, 1, 31), True)
    calendar.interval_combo.setCurrentText("Quarterly")
    assert calendar.period_label.text() == "2026–2027"
    assert len([cell for cell in calendar.period_cells if not cell.isHidden()]) == 8
    assert "Q1 2026" in calendar.period_cells[0].text()
    assert "Q4 2027" in calendar.period_cells[7].text()
    assert "+$7.00" in calendar.summary_label.text()
    calendar.next_button.click()
    assert calendar.period_label.text() == "2028–2029"
    assert "Q1 2028" in calendar.period_cells[0].text()
    assert "+$7.00" in calendar.period_cells[0].text()
    assert not calendar.next_button.isEnabled()
    calendar.previous_button.click()
    assert calendar.period_label.text() == "2026–2027"
    calendar.close()






def test_flex_monthly_history_links_flows_and_includes_latest_partial_month():
    snapshots = [
        {"day": "2026-01-30", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-02-02", "nav_usd": 1515, "source": "flex", "flow_covered": 1},
        {"day": "2026-02-03", "nav_usd": 1545, "source": "flex", "flow_covered": 1},
        {"day": "2026-03-02", "nav_usd": 1800, "source": "live", "flow_covered": 0},
    ]
    flows = [{"day": "2026-01-31", "amount_usd": 500}]
    rows = flex_monthly_history(snapshots, flows)
    assert [row["month"] for row in rows] == ["2026-01", "2026-02"]
    assert rows[0]["return_pct"] is None
    assert rows[1]["period_from"] == "2026-02-02"
    assert rows[1]["period_to"] == "2026-02-03"
    assert rows[1]["nav_usd"] == 1545
    assert rows[1]["return_pct"] == pytest.approx(
        ((1 + 15 / 1500) * (1 + 30 / 1515) - 1) * 100)


def test_flex_monthly_history_leaves_gapped_return_blank_but_keeps_latest_nav():
    snapshots = [
        {"day": "2026-01-30", "nav_usd": 1000, "source": "flex", "flow_covered": 1},
        {"day": "2026-02-02", "nav_usd": 1010, "source": "flex", "flow_covered": 1},
        {"day": "2026-02-10", "nav_usd": 1100, "source": "flex", "flow_covered": 1},
        {"day": "2026-03-02", "nav_usd": 1200, "source": "flex", "flow_covered": 0},
    ]
    rows = flex_monthly_history(snapshots, [])
    assert rows[1]["nav_usd"] == 1100
    assert rows[1]["return_pct"] is None
    assert rows[2]["return_pct"] is None






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
        tab.sections.setCurrentWidget(tab.capital_page)
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
            tab.set_pnl_metric(metric)
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
        assert len(tab.pnl_chart.chart().axes(Qt.Vertical)) == 1
        tab.sections.setCurrentWidget(tab.capital_page)
        assert len(tab.capital_chart.chart().axes(Qt.Vertical)) == 1
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
        assert [window.analytics_tab.sections.tabText(i) for i in range(5)] == [
            "P&&L", "Returns", "Capital deployment", "Exposure", "Monthly P&&L"]
        assert window.analytics_tab.benchmark_toggle.text() == "S&&P 500"
        assert window.analytics_tab.benchmark_toggle.accessibleName() == "S&P 500"
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
        window.analytics_tab.sections.setCurrentWidget(window.analytics_tab.capital_page)
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
        assert window.analytics_tab.max_daily_detail.isHidden()
        assert window.analytics_tab.sharpe_detail.isHidden()
        assert window.analytics_tab.max_daily_info.toolTip() == window.analytics_tab.max_daily_detail.text()
        assert window.analytics_tab.sharpe_info.toolTip() == window.analytics_tab.sharpe_detail.text()
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
        window.analytics_tab.set_pnl_metric("NAV")
        chart = window.analytics_tab.pnl_chart.chart()
        assert chart.series()[0].name() == "NAV"
        assert [point.y() for point in chart.series()[0].points()] == [1000, 1510, 1310]
        assert len(chart.series()) == 3  # Deposits and withdrawals mark the raw NAV chart.
        assert chart.axes(Qt.Vertical)[0].min() > 0
        window.analytics_tab.set_pnl_metric("NAV change")
        chart = window.analytics_tab.pnl_chart.chart()
        assert len(chart.series()) == 3
        for marker, day in zip(chart.series()[1:], ("2026-01-05", "2026-01-06")):
            assert day in marker.name()
            assert marker.points()[0].x() == marker.points()[1].x()
            assert marker.points()[0].y() < marker.points()[1].y()
        window.analytics_tab.set_pnl_metric("Adjusted NAV change")
        chart = window.analytics_tab.pnl_chart.chart()
        assert [series.name() for series in chart.series()] == ["Adjusted NAV change"]
        assert len(chart.axes(Qt.Vertical)) == 1
        assert chart.localizeNumbers()
        assert chart.locale().toString(10000.0, "f", 0) == "10,000"
        assert chart.axes(Qt.Vertical)[0].titleText() == "USD"
        window.analytics_tab.set_pnl_metric("Adjusted performance")
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












@pytest.mark.parametrize("section", ("pnl", "capital"))
def test_chart_drag_updates_shared_dates_and_both_tables(tmp_path, monkeypatch, section):
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
        monkeypatch.setattr(window.service.matching, "trade_rows", lambda: [
            {"trade_date": (first + timedelta(days=offset)).isoformat(),
             "security_type": "STK", "side": "BUY", "gross_usd": 100,
             "remaining_qty": 0}
            for offset in range(41) if (first + timedelta(days=offset)).weekday() < 5
        ])
        tab = window.analytics_tab
        window.resize(1200, 800)
        window.tabs.setCurrentWidget(tab)
        tab.range_combo.setCurrentText("All")
        tab.set_pnl_metric("NAV change")
        tab.sections.setCurrentWidget(getattr(tab, f"{section}_page"))
        window.show()
        app.processEvents()
        navigation = getattr(tab, f"{section}_navigation")
        navigation.select_dates.click()
        app.processEvents()
        active_controls = getattr(tab, f"{section}_controls")
        assert navigation.select_dates.isChecked()
        assert tab.range_combo.currentText() == "Custom"
        assert active_controls.start_date.isVisible()
        assert active_controls.end_date.isVisible()
        assert tab.start_date.date().toPython() == first
        assert tab.end_date.date().toPython() == today
        view = getattr(tab, f"{section}_chart")
        area = view.chart().plotArea()
        assert area.width() > 100
        start = QPoint(int(area.left() + area.width() * .2), int(area.center().y()))
        end = QPoint(int(area.left() + area.width() * .7), int(area.center().y()))
        if section == "capital":
            start, end = end, start
        QTest.mousePress(view.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        QTest.mouseMove(view.viewport(), end)
        highlight = view._band.geometry()
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, Qt.NoModifier, end)
        app.processEvents()
        assert highlight.height() >= int(area.height())
        assert highlight.width() >= abs(end.x() - start.x())
        assert view.cursor().shape() == Qt.CrossCursor
        assert not view._band.isVisible()
        assert tab.range_combo.currentText() == "Custom"
        assert tab.start_date.date().toPython() > first
        assert tab.end_date.date().toPython() < today
        assert tab.capital_controls.range_combo.currentText() == "Custom"
        assert tab.capital_controls.start_date.date() == tab.start_date.date()
        assert tab.capital_controls.end_date.date() == tab.end_date.date()
        selected_first, selected_last = tab.start_date.date(), tab.end_date.date()
        for page, table in ((tab.pnl_page, tab.pnl_table), (tab.capital_page, tab.capital_table)):
            tab.sections.setCurrentWidget(page)
            assert 0 < table.rowCount() < 29
            for row in range(table.rowCount()):
                assert (selected_first.toString(Qt.ISODate) <= table.item(row, 0).text()
                        <= selected_last.toString(Qt.ISODate))
        tab.sections.setCurrentWidget(getattr(tab, f"{section}_page"))
        selected_span = tab.start_date.date().daysTo(tab.end_date.date())
        if section == "capital":
            navigation.select_dates.click()
        modifiers = Qt.ShiftModifier if navigation.select_dates.isChecked() else Qt.NoModifier
        app.processEvents()
        area = view.chart().plotArea()
        start = QPoint(int(area.center().x()), int(area.center().y()))
        end = start - QPoint(int(area.width() * .1), 0)
        QTest.mousePress(view.viewport(), Qt.LeftButton, modifiers, start)
        QTest.mouseMove(view.viewport(), end)
        assert view.cursor().shape() == Qt.ClosedHandCursor
        assert not view._band.isVisible()
        QTest.mouseRelease(view.viewport(), Qt.LeftButton, modifiers, end)
        assert tab.start_date.date() > selected_first
        assert tab.start_date.date().daysTo(tab.end_date.date()) == selected_span
        navigation.zoom_in.click()
        assert tab.start_date.date().daysTo(tab.end_date.date()) < selected_span
        assert tab.capital_controls.start_date.date() == tab.start_date.date()
        navigation.reset.click()
        assert tab.range_combo.currentText() == "All"
        assert tab.start_date.date().toPython() == first
        assert tab.end_date.date().toPython() == today
        if navigation.select_dates.isChecked():
            navigation.select_dates.click()
        assert view.cursor().shape() == Qt.ArrowCursor
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
