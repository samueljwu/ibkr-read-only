"""Synthetic public split responses and recorded corporate actions."""

from datetime import date
from decimal import Decimal
import json

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from app.market.split_history import (SplitHistoryClient, combine_split_history,
                                      parse_split_history)
from app.storage.database import Database


def _payload(symbol="SYN", currency="USD", instrument_type="EQUITY"):
    return {"chart": {"result": [{"meta": {"symbol": symbol, "currency": currency,
                                            "instrumentType": instrument_type},
                                  "events": {"splits": {
                                      "one": {"date": 1718199000, "numerator": 2.0,
                                              "denominator": 1.0},
                                      "two": {"date": 1788442200, "numerator": 2.0,
                                              "denominator": 1.0}}}}]}}




def test_reject_mismatched_instrument_and_disagreeing_ratio():
    with pytest.raises(ValueError, match="does not match"):
        parse_split_history(_payload(symbol="OTHER"), "SYN", "USD")
    with pytest.raises(ValueError, match="disagree"):
        combine_split_history({"2024-06-11": Decimal(3)},
                              {"2024-06-12": Decimal(2)})




def test_verified_same_day_cache_is_reused_without_a_request(tmp_path):
    db = Database(tmp_path / "split-history.sqlite3")
    position = {"id": 7, "con_id": 123, "ib_symbol": "SYN", "currency": "USD"}
    db.set_setting("analytics_levels_split_history_7", json.dumps({
        "date": date.today().isoformat(), "symbol": "SYN", "currency": "USD",
        "con_id": 123, "events": [["2024-06-12", "2", "1"],
                                   ["2026-09-03", "2", "1"]]}))
    client = SplitHistoryClient(db)
    events, status = client.events(position, "2024-05-01", lambda: None)
    assert status == "ready"
    assert events == {"2024-06-12": Decimal(2), "2026-09-03": Decimal(2)}
    assert not client._pending
    db.conn.close()




@pytest.mark.parametrize("symbol,currency,lookup,instrument_type", [
    ("CRD", "AUD", "CRD.AX", "EQUITY"),
    ("1321", "JPY", "1321.T", "ETF"),
])
def test_foreign_listing_split_lookup_and_cache(tmp_path, symbol, currency,
                                                lookup, instrument_type):
    class Reply(QObject):
        finished = Signal()

        def error(self):
            return QNetworkReply.NoError

        def readAll(self):
            return json.dumps(_payload(lookup, currency, instrument_type)).encode()

        def deleteLater(self):
            pass

    class Network:
        def __init__(self):
            self.reply = Reply()
            self.request = None

        def get(self, request):
            self.request = request
            return self.reply

    db = Database(tmp_path / "foreign-splits.sqlite3")
    client = SplitHistoryClient(db)
    client.network = Network()
    position = {"id": 9, "con_id": 456, "ib_symbol": symbol, "currency": currency}
    changes = []
    assert client.events(position, "2024-05-01", lambda: changes.append(True)) == (None, "fetching")
    assert client.network.request.url().path().endswith("/" + lookup)
    client.network.reply.finished.emit()
    assert changes == [True]
    events, status = client.events(position, "2024-05-01", lambda: None)
    assert status == "ready"
    assert events["2024-06-12"] == Decimal(2)
    assert json.loads(db.setting("analytics_levels_split_history_9"))["lookup_symbol"] == lookup
    db.conn.close()
