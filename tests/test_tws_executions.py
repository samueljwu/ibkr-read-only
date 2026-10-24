from types import SimpleNamespace
from datetime import date, timedelta

import pytest

from app.broker.broker_sync import BrokerIngestor
from app.storage.database import Database
from app.market.market_data import IBKRMarketDataProvider


ACCOUNT = "U0000002"


def _contract():
    return SimpleNamespace(conId=123, symbol="ABC", secType="STK", currency="USD",
                           multiplier="1", localSymbol="ABC",
                           lastTradeDateOrContractMonth="", right="", strike=0,
                           exchange="SMART", primaryExchange="", tradingClass="")


def _execution(exec_id="EX.01", account=ACCOUNT):
    return SimpleNamespace(execId=exec_id, acctNumber=account, side="BOT", shares=5,
                           price=10, time="20261006 10:15:00 US/Eastern")


@pytest.mark.parametrize("callback,field,amount", [
    ("commissionAndFeesReport", "commissionAndFees", 0.5),
    ("commissionReport", "commission", -0.2),
])
def test_tws_read_only_execution_capture_fee_and_reconnect_replay(tmp_path, callback, field, amount):
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
        getattr(client, callback)(SimpleNamespace(execId="EX.01", currency="USD",
                                                  **{field: amount}))
        update = provider.events.get_nowait()
        assert update[0] == "execution"
        assert ingestor.ingest_many([update[1]], "tws").updated == 1
        assert db.conn.execute("SELECT commission_usd FROM trades").fetchone()[0] == pytest.approx(amount)
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
