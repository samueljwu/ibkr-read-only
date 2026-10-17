from types import SimpleNamespace
from datetime import date, timedelta

import pytest

from app.broker.broker_sync import BrokerIngestor
from app.storage.database import Database
from app.market.market_data import IBKRMarketDataProvider, five_year_start


ACCOUNT = "U0000002"


def _contract():
    return SimpleNamespace(conId=123, symbol="ABC", secType="STK", currency="USD",
                           multiplier="1", localSymbol="ABC",
                           lastTradeDateOrContractMonth="", right="", strike=0,
                           exchange="SMART", primaryExchange="", tradingClass="")


def _execution(exec_id="EX.01", account=ACCOUNT):
    return SimpleNamespace(execId=exec_id, acctNumber=account, side="BOT", shares=5,
                           price=10, time="20261006 10:15:00 US/Eastern")


def test_tws_read_only_execution_capture_fee_and_reconnect_replay(tmp_path):
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    except RuntimeError:
        pytest.fail("ibapi is required; install requirements.txt to run the TWS tests")
    db = Database(tmp_path / "tws.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    try:
        client = provider.client
        client.execution_request_id = 1001
        client.execDetails(1001, _contract(), _execution(account="OTHER"))
        assert provider.events.empty()
        client.execDetails(1001, _contract(), _execution())
        first = provider.events.get_nowait()
        assert first[0] == "execution" and first[1]["side"] == "BUY"
        ingestor = BrokerIngestor(db, ACCOUNT)
        assert ingestor.ingest_many([first[1]], "tws").inserted == 1
        assert db.conn.execute("SELECT status FROM broker_executions").fetchone()[0] == "pending commission"
        client.commissionReport(SimpleNamespace(execId="EX.01", commission=0.5,
                                                currency="USD"))
        update = provider.events.get_nowait()
        assert update[0] == "execution"
        assert ingestor.ingest_many([update[1]], "tws").updated == 1
        assert db.conn.execute("SELECT commission_usd FROM trades").fetchone()[0] == pytest.approx(0.5)
        client.execDetails(1001, _contract(), _execution())
        replay = provider.events.get_nowait()
        assert ingestor.ingest_many([replay[1]], "tws").duplicates == 1
        assert db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    finally:
        db.conn.close()


def test_tws_connection_requests_account_filtered_executions_without_orders():
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    except RuntimeError:
        pytest.fail("ibapi is required; install requirements.txt to run the TWS tests")
    client = provider.client
    requested = []
    client.reqMarketDataType = lambda _mode: None
    client.reqPositions = lambda: None
    client.reqAccountSummary = lambda *_args: None
    client.reqExecutions = lambda request_id, filters: requested.append((request_id, filters.acctCode))
    provider._request_all = lambda: None
    client.nextValidId(0)
    assert requested == [(client.execution_request_id, ACCOUNT)]


def test_position_updates_after_initial_snapshot_are_emitted():
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    except RuntimeError:
        pytest.fail("ibapi is required; install requirements.txt to run the TWS tests")
    client = provider.client
    client.managedAccounts(ACCOUNT)
    provider.events.get_nowait()
    client.position(ACCOUNT, _contract(), 5, 10)
    client.positionEnd()
    snapshot = provider.events.get_nowait()
    assert snapshot[0] == "broker_positions"
    assert snapshot[1][ACCOUNT][0]["quantity"] == 5
    client.position(ACCOUNT, _contract(), 0, 10)
    assert provider.events.get_nowait()[:2] == ("broker_position_delta", ACCOUNT)


def test_global_connection_events_recover_streams():
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    except RuntimeError:
        pytest.fail("ibapi is required; install requirements.txt to run the TWS tests")
    client = provider.client
    requests = []
    client.contracts[1002] = _contract()
    client.reqMktData = lambda request_id, *_args: requests.append(request_id)
    provider.refresh_live = lambda: requests.append("snapshot")
    client.error(-1, 1100, "Connectivity lost")
    assert provider.events.get_nowait() == ("connected", False)
    client.error(-1, 1101, "Connectivity restored; data lost")
    assert provider.events.get_nowait() == ("connected", True)
    assert requests == [1002, "snapshot"]


def test_stale_contract_history_requests_only_missing_days():
    try:
        provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    except RuntimeError:
        pytest.fail("ibapi is required; install requirements.txt to run the TWS tests")
    provider.positions = {1: {"con_id": 123}}
    provider.stale_con_ids = {123}
    provider.history_last_days = {123: (date.today() - timedelta(days=1)).isoformat()}
    client = provider.client
    durations = []
    client.reqMktData = lambda *_args: None
    client.reqContractDetails = lambda *_args: None
    client.reqHistoricalData = lambda _request_id, _contract, _end, duration, *_args: durations.append(duration)
    provider._subscribe(1, _contract())
    assert durations == ["8 D"]


def test_closed_stock_five_year_history_request_without_market_subscription():
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    provider.connected = True
    provider.client.isConnected = lambda: True
    calls = []
    provider.client.reqHistoricalData = lambda *args: calls.append(args)
    position = {"con_id": 123, "ib_symbol": "ABC", "security_type": "STK",
                "exchange": "SMART", "currency": "USD"}
    provider.client.history_ids[999] = 123  # A short routine refresh may be in flight.
    assert provider.request_price_history(position, "2023-08-01")
    assert len(calls) == 1
    assert calls[0][1].conId == 123
    assert calls[0][2] == "20230731 23:59:59"
    assert calls[0][3] == "1 Y"
    assert calls[0][4:7] == ("1 day", "TRADES", 1)
    assert not provider.request_price_history(position, "2023-08-01")
    assert len(calls) == 1
    request_id = calls[0][0]
    provider.client.error(request_id, 162, "Historical data unavailable")
    assert provider.events.get_nowait() == (
        "history_error", 123, "IBKR ERROR 162: Historical data unavailable")
    assert request_id not in provider.client.history_ids
    assert request_id not in provider.client.chart_history_ids
    assert 123 not in provider.client.chart_history_jobs


def test_one_click_pages_back_five_years_before_first_saved_close():
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    provider.connected = True
    provider.client.isConnected = lambda: True
    calls = []
    provider.client.reqHistoricalData = lambda *args: calls.append(args)
    position = {"con_id": 123, "ib_symbol": "ABC", "security_type": "STK",
                "exchange": "SMART", "currency": "USD"}
    saved = date.today() - timedelta(days=365 * 3)
    assert provider.request_price_history(position, saved.isoformat())
    assert calls[0][2] == f"{saved - timedelta(days=1):%Y%m%d} 23:59:59"
    assert calls[0][3] == "1 Y"
    for age in (4, 5):
        request_id = calls[-1][0]
        day = date.today() - timedelta(days=365 * age)
        bar = SimpleNamespace(date=f"{day:%Y%m%d}", close=50 + age, volume=100)
        provider.client.historicalData(request_id, bar)
        provider.client.historicalDataEnd(request_id, "", "")
    assert len(calls) == 2
    events = [provider.events.get_nowait() for _ in range(2)]
    assert events[0][0:2] == ("history", 123)
    assert {bar["day"] for bar in events[0][2]} == {
        (date.today() - timedelta(days=365 * age)).isoformat() for age in (4, 5)}
    assert events[1] == ("chart_history_complete", 123, True)
    assert 123 not in provider.client.chart_history_jobs


def test_five_year_start_handles_leap_day():
    assert five_year_start(date(2028, 2, 29)) == date(2023, 2, 28)


def test_five_year_request_stops_when_ibkr_has_no_older_bars():
    provider = IBKRMarketDataProvider("127.0.0.1", 7497, 17, account_id=ACCOUNT)
    provider.connected = True
    provider.client.isConnected = lambda: True
    calls = []
    provider.client.reqHistoricalData = lambda *args: calls.append(args)
    position = {"con_id": 123, "ib_symbol": "ABC", "security_type": "STK"}
    assert provider.request_price_history(position, (date.today() - timedelta(days=365)).isoformat())
    provider.client.historicalDataEnd(calls[0][0], "", "")
    assert len(calls) == 1
    assert provider.events.get_nowait() == ("history", 123, [])
    assert provider.events.get_nowait() == ("chart_history_complete", 123, False)
    assert 123 not in provider.client.chart_history_jobs
