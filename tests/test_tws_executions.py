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
