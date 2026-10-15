from app.broker.broker_sync import BrokerIngestor
from app.finance.option_labels import option_label
from app.storage.database import Database


def test_option_label_uses_compact_expiry_and_strike():
    assert option_label(" qqq ", "20261120", "CALL", 800.0) == "QQQ 261120 C800"
    assert option_label("QQQ", "2026-11-20", "P", 800.5) == "QQQ 261120 P800.5"
    assert option_label("QQQ 261218C00800000", "20261218", "C", 800) == "QQQ 261218 C800"
    assert option_label("QQQ 261218 C800", "20261218", "C", 800) == "QQQ 261218 C800"
    assert option_label("QQQ", "202611", "C", 800) is None


def test_broker_option_and_linked_trade_use_compact_label(tmp_path):
    db = Database(tmp_path / "option.sqlite3")
    try:
        record = {
            "account_id": "U-SYNTHETIC", "source_id": "OPTION.01",
            "ib_exec_id": "OPTION.01", "trade_date": "2026-10-06",
            "side": "BUY", "quantity": 1, "price": 3.5, "con_id": 12345,
            "symbol": "QQQ 261120C00800000", "security_type": "OPT", "currency": "USD",
            "multiplier": 100, "commission": 0, "commission_currency": "USD",
            "local_symbol": "QQQ   261120C00800000", "expiry": "20261120",
            "option_right": "C", "strike": 800,
        }
        assert BrokerIngestor(db, "U-SYNTHETIC").ingest_many([record], "tws").inserted == 1
        assert db.rows("positions")[0]["display_ticker"] == "QQQ 261120 C800"
        assert db.rows("trades")[0]["display_ticker"] == "QQQ 261120 C800"
    finally:
        db.conn.close()


def test_existing_option_and_linked_trade_are_renamed_on_open(tmp_path):
    path = tmp_path / "existing.sqlite3"
    db = Database(path)
    try:
        position_id = db.add_position({"display_ticker": "QQQ   261120C00800000",
                                       "ib_symbol": "QQQ 261120C00800000", "security_type": "OPT"})
        db.add_trade({"position_id": position_id, "trade_date": "2026-10-06",
                      "side": "BUY", "quantity": 1, "price": 3.5})
        db.conn.execute("""UPDATE positions SET expiry='20261120', option_right='C',
            strike=800 WHERE id=?""", (position_id,))
        db.conn.commit()
    finally:
        db.conn.close()
    reopened = Database(path)
    try:
        assert reopened.rows("positions")[0]["display_ticker"] == "QQQ 261120 C800"
        assert reopened.rows("trades")[0]["display_ticker"] == "QQQ 261120 C800"
    finally:
        reopened.conn.close()
