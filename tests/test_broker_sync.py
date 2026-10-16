import pytest

from app.broker.broker_sync import BrokerIngestor, parse_flex_xml
from app.storage.database import Database
from app.finance.calculations import closed_lot
from app.finance.services import MatchingService
from app.finance.services import PortfolioService
from app.market.market_data import MockMarketDataProvider


ACCOUNT = "U0000002"


def execution(**changes):
    record = {
        "account_id": ACCOUNT, "source": "tws", "source_id": "EX.01",
        "ib_exec_id": "EX.01", "trade_date": "2026-10-06", "executed_at": "101500",
        "side": "BUY", "quantity": 5, "price": 10.0, "con_id": 123,
        "symbol": "ABC", "security_type": "STK", "currency": "USD",
        "multiplier": 1, "fx_to_usd": 1, "commission": None,
        "commission_currency": "USD",
    }
    record.update(changes)
    return record


def test_tws_flex_replay_and_correction_share_one_trade(tmp_path):
    db = Database(tmp_path / "sync.sqlite3")
    db.set_setting("ib_account_id", ACCOUNT)
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        first = ingestor.ingest_many([execution()], "tws")
        assert first.inserted == 1
        assert first.review == 1  # fee has not arrived
        assert ingestor.ingest_many([execution()], "tws").duplicates == 1

        flex = execution(source="flex", source_id="T1", flex_trade_id="T1",
                         commission=-0.5, ib_exec_id="EX.01")
        enriched = ingestor.ingest_many([flex], "flex", window=("2026-10-01", "2026-10-06"))
        assert enriched.updated == 1
        assert enriched.review == 0
        assert len(db.rows("trades", 1)) == 1
        assert db.rows("trades", 1)[0]["commission_usd"] == pytest.approx(0.5)
        assert ingestor.ingest_many([flex], "flex").duplicates == 1

        correction = execution(source_id="EX.02", ib_exec_id="EX.02", quantity=6,
                               commission=0.6)
        changed = ingestor.ingest_many([correction], "tws")
        assert changed.updated == 1
        assert len(db.rows("trades", 1)) == 1
        assert db.rows("trades", 1)[0]["quantity"] == 6
        assert db.conn.execute("SELECT COUNT(*) FROM broker_source_records").fetchone()[0] == 3
    finally:
        db.conn.close()


def test_missing_fee_stays_pending_until_zero_fee_arrives(tmp_path):
    db = Database(tmp_path / "fee.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        assert ingestor.ingest_many([execution()], "tws").inserted == 1
        market = MockMarketDataProvider()
        market.refresh(db.rows("positions", 1))
        service = PortfolioService(db, market)
        assert service.matching.trade_rows()[0]["commission_usd"] is None
        assert service.portfolio_rows()[0]["data_status"] == "FEE PENDING"
        assert service.lots_rows()[0]["net_unrealized"] is None
        assert ingestor.ingest_many([execution(commission=0)], "tws").updated == 1
        assert db.conn.execute("SELECT status FROM broker_executions").fetchone()[0] == "complete"
        assert service.matching.trade_rows()[0]["commission_usd"] == 0
        assert service.lots_rows()[0]["net_unrealized"] is not None
    finally:
        db.conn.close()


def test_distinct_equal_fills_are_kept_and_unlinked_source_is_flagged(tmp_path):
    db = Database(tmp_path / "equal.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        assert ingestor.ingest_many([execution(source_id="A.01", ib_exec_id="A.01"),
                                     execution(source_id="B.01", ib_exec_id="B.01")], "tws").inserted == 2
        uncertain = execution(source="flex", source_id="T3", flex_trade_id="T3",
                              ib_exec_id="", commission=-0.1)
        result = ingestor.ingest_many([uncertain], "flex")
        assert result.review == 1
        assert len(db.rows("trades", 1)) == 2
        assert db.conn.execute("SELECT status FROM broker_source_records WHERE source_id='T3'").fetchone()[0] == "review"
    finally:
        db.conn.close()


def test_bad_record_is_retained_for_review_and_batch_is_atomic(tmp_path):
    db = Database(tmp_path / "errors.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        bad = execution(source_id="bad", ib_exec_id="", con_id=None)
        result = ingestor.ingest_many([bad], "tws")
        assert result.review == 1
        assert db.rows("trades", 1) == []
        assert db.conn.execute("SELECT raw_json FROM broker_source_records WHERE source_id='bad'").fetchone()
        with pytest.raises(ValueError, match="account"):
            ingestor.ingest_many([execution(source_id="valid", ib_exec_id="valid.01"),
                                  execution(account_id="OTHER")], "tws")
        assert db.rows("trades", 1) == []
    finally:
        db.conn.close()


def test_flex_parser_preserves_missing_fee_and_lifecycle_events():
    xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002" baseCurrency="USD">
    <Trades><Trade levelOfDetail="EXECUTION" tradeID="T1" ibExecID="EX.01"
     tradeDate="20261006" dateTime="20261006;101500" buySell="BUY" quantity="5"
     tradePrice="10" conid="123" symbol="ABC" currency="USD" assetCategory="STK" multiplier="1"/>
    <Trade levelOfDetail="CLOSED_LOT" tradeID="T1"/></Trades>
    <CorporateActions><CorporateAction conid="123" actionID="X1"/></CorporateActions>
    </FlexStatement></FlexStatements></FlexQueryResponse>'''
    trades, events = parse_flex_xml(xml, ACCOUNT)
    assert len(trades) == 1 and len(events) == 1
    assert trades[0]["commission"] is None
    with pytest.raises(ValueError, match="account"):
        parse_flex_xml(xml, "OTHER")


def test_flex_parser_accepts_report_with_usd_conversion_rates_and_empty_event_section():
    xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002">
    <Trades><Trade levelOfDetail="EXECUTION" tradeID="T1" ibExecID="EX.01"
     tradeDate="20261006" dateTime="20261006;101500" buySell="BUY" quantity="5"
     tradePrice="10" conid="123" symbol="ABC" currency="USD" assetCategory="STK"
     multiplier="1" ibCommission="-0.5"/></Trades><OptionEAE/>
    <ConversionRates><ConversionRate fromCurrency="EUR" toCurrency="USD" rate="1.2"
     reportDate="20261006"/></ConversionRates>
    </FlexStatement></FlexStatements></FlexQueryResponse>'''
    trades, events = parse_flex_xml(xml, ACCOUNT)
    assert len(trades) == 1
    assert events == []


def test_incoming_legacy_transfer_becomes_costed_opening_lot_without_execution(tmp_path):
    db = Database(tmp_path / "legacy.sqlite3")
    try:
        transfer = {"kind": "Transfer", "raw": {
            "accountId": ACCOUNT, "transactionID": "X1", "direction": "IN", "type": "ACATS",
            "assetCategory": "STK", "conid": "123", "symbol": "ABC", "currency": "USD",
            "quantity": "3", "cost": "383.2", "multiplier": "1", "date": "20210422",
        }}
        ingestor = BrokerIngestor(db, ACCOUNT)
        first = ingestor.ingest_many([], "flex", events=[transfer])
        assert first.legacy_inserted == 1
        assert first.review == 0
        lot = db.rows("trades", 1)[0]
        assert lot["quantity"] == 3
        assert lot["price"] == pytest.approx(383.2 / 3)
        assert lot["source"] == "IBKR legacy transfer"
        assert db.conn.execute("SELECT COUNT(*) FROM broker_executions").fetchone()[0] == 0
        second = ingestor.ingest_many([], "flex", events=[transfer])
        assert second.legacy_duplicates == 1
        assert len(db.rows("trades", 1)) == 1
    finally:
        db.conn.close()


def test_cash_merger_closes_shares_at_broker_proceeds_once(tmp_path):
    db = Database(tmp_path / "merger.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        ingestor.ingest_many([execution(source="flex", source_id="T1", flex_trade_id="T1",
                                        ib_exec_id="ZZCO.01", con_id=900000002, symbol="ZZCO",
                                        trade_date="2024-01-01", quantity=20, price=10,
                                        commission=-1)], "flex")
        event = {"kind": "CorporateAction", "raw": {
            "accountId": ACCOUNT, "transactionID": "MERGER-1", "type": "TC",
            "assetCategory": "STK", "conid": "900000002", "symbol": "ZZCO",
            "currency": "USD", "multiplier": "1", "dateTime": "20240115;120000",
            "quantity": "-20", "proceeds": "240", "fxRateToBase": "1",
            "actionDescription": "ZZCO MERGED(Cash) FOR USD 12.00 PER SHARE",
        }}
        first = ingestor.ingest_many([], "flex", events=[event])
        assert first.corporate_inserted == 1 and first.review == 0
        assert ingestor.ingest_many([], "flex", events=[event]).corporate_inserted == 0
        rows = db.rows("trades", 1)
        assert len(rows) == 2
        sell = next(row for row in rows if row["source"] == "IBKR corporate action")
        assert (sell["side"], sell["quantity"], sell["price"]) == ("SELL", 20, 12)
        assert db.conn.execute("SELECT status FROM broker_source_records WHERE source='flex_event'").fetchone()[0] == "complete"
        assert sum(row["quantity"] * (1 if row["side"] == "BUY" else -1) for row in rows) == 0
    finally:
        db.conn.close()


def test_bond_redemption_uses_percent_of_par_and_rebate_sign(tmp_path):
    db = Database(tmp_path / "bond.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002" baseCurrency="USD"><Trades>
        <Trade levelOfDetail="EXECUTION" tradeID="B1" ibExecID="BOND.01" tradeDate="20240101"
          buySell="BUY" quantity="1000" tradePrice="95" conid="900000003" symbol="TESTBOND"
          currency="USD" assetCategory="BOND" multiplier="1" ibCommission="-2" ibCommissionCurrency="USD"/>
        </Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''
        records, _ = parse_flex_xml(xml, ACCOUNT)
        assert records[0]["multiplier"] == pytest.approx(0.01)
        ingestor.ingest_many(records, "flex")
        bond = db.rows("trades", 1)[0]
        assert bond["quantity"] * bond["price"] * bond["multiplier"] == pytest.approx(950)
        event = {"kind": "CorporateAction", "raw": {
            "accountId": ACCOUNT, "transactionID": "MATURITY-1", "type": "BM",
            "assetCategory": "BOND", "conid": "900000003", "symbol": "TESTBOND",
            "currency": "USD", "multiplier": "1", "dateTime": "20240201;120000",
            "quantity": "-1000", "proceeds": "1000", "fxRateToBase": "1",
            "actionDescription": "BOND MATURITY FOR USD 1.00 PER BOND",
        }}
        assert ingestor.ingest_many([], "flex", events=[event]).corporate_inserted == 1
        sell = next(row for row in db.rows("trades", 1) if row["side"] == "SELL")
        assert sell["price"] == pytest.approx(100)
        assert closed_lot(bond, sell, 1000)["realized_net"] == pytest.approx(48)

        rebate = execution(source="flex", source_id="REBATE", flex_trade_id="REBATE",
                           ib_exec_id="REBATE.01", con_id=321, commission=0.2)
        ingestor.ingest_many([rebate], "flex")
        rebate_row = db.conn.execute("SELECT commission_usd FROM trades WHERE trade_id=?",
                                     (f"IBKR:{ACCOUNT}:E:REBATE",)).fetchone()
        assert rebate_row[0] == pytest.approx(-0.2)
    finally:
        db.conn.close()


def test_bond_buy_accrued_interest_stored_signed_negative(tmp_path):
    db = Database(tmp_path / "bond_buy_accrued.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002" baseCurrency="USD"><Trades>
        <Trade levelOfDetail="EXECUTION" tradeID="B2" ibExecID="BOND.02" tradeDate="20240101"
          buySell="BUY" quantity="1000" tradePrice="95" conid="900000004" symbol="TESTBOND2"
          currency="USD" assetCategory="BOND" multiplier="1" ibCommission="-2" ibCommissionCurrency="USD"
          accruedInt="12.5" fxRateToBase="1"/>
        </Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''
        records, _ = parse_flex_xml(xml, ACCOUNT)
        assert records[0]["raw"]["accruedInt"] == "12.5"
        ingestor.ingest_many(records, "flex")
        bond = db.rows("trades", 1)[0]
        # Negative: accrued interest adds to the buyer's cash paid out at settlement.
        assert bond["accrued_interest_usd"] == pytest.approx(-12.5)
    finally:
        db.conn.close()


def test_bond_sell_accrued_interest_stored_signed_positive(tmp_path):
    db = Database(tmp_path / "bond_sell_accrued.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002" baseCurrency="USD"><Trades>
        <Trade levelOfDetail="EXECUTION" tradeID="B3" ibExecID="BOND.03" tradeDate="20240101"
          buySell="SELL" quantity="1000" tradePrice="97" conid="900000005" symbol="TESTBOND3"
          currency="USD" assetCategory="BOND" multiplier="1" ibCommission="-2" ibCommissionCurrency="USD"
          accruedInt="8.25" fxRateToBase="1"/>
        </Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''
        records, _ = parse_flex_xml(xml, ACCOUNT)
        ingestor.ingest_many(records, "flex")
        bond = db.rows("trades", 1)[0]
        # Positive: accrued interest adds to the seller's cash received at settlement.
        assert bond["accrued_interest_usd"] == pytest.approx(8.25)
    finally:
        db.conn.close()


def test_bond_accrued_interest_converted_via_trade_fx_rate(tmp_path):
    db = Database(tmp_path / "bond_fx_accrued.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        xml = b'''<FlexQueryResponse><FlexStatements><FlexStatement accountId="U0000002" baseCurrency="USD"><Trades>
        <Trade levelOfDetail="EXECUTION" tradeID="B4" ibExecID="BOND.04" tradeDate="20240101"
          buySell="BUY" quantity="1000" tradePrice="95" conid="900000006" symbol="TESTBOND4"
          currency="EUR" assetCategory="BOND" multiplier="1" ibCommission="-2" ibCommissionCurrency="EUR"
          accruedInt="10" fxRateToBase="1.1"/>
        </Trades></FlexStatement></FlexStatements></FlexQueryResponse>'''
        records, _ = parse_flex_xml(xml, ACCOUNT)
        ingestor.ingest_many(records, "flex")
        bond = db.rows("trades", 1)[0]
        assert bond["accrued_interest_usd"] == pytest.approx(-11.0)
    finally:
        db.conn.close()


def test_non_bond_instrument_ignores_stray_accrued_interest_attribute(tmp_path):
    db = Database(tmp_path / "stray_accrued.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        # A stray accruedInt attribute on a non-bond trade must never be converted.
        stock = execution(commission=-1, con_id=777, symbol="XYZ")
        stock["raw"] = dict(stock.get("raw") or {}, accruedInt="999")
        ingestor.ingest_many([stock], "tws")
        row = db.rows("trades", 1)[0]
        assert row["security_type"] == "STK"
        assert row["accrued_interest_usd"] == 0
    finally:
        db.conn.close()


def test_completed_forward_split_reconciles_closed_position(tmp_path):
    db = Database(tmp_path / "split.sqlite3")
    try:
        pid = db.add_position({"display_ticker": "ZZSP", "ib_symbol": "ZZSP"})
        db.cache_contract(pid, 4493)
        db.add_trade({"position_id": pid, "trade_date": "2024-04-01", "side": "BUY",
                      "quantity": 100, "price": 100, "commission_usd": 0})
        db.add_trade({"position_id": pid, "trade_date": "2024-09-03", "side": "SELL",
                      "quantity": 200, "price": 60, "commission_usd": 0})
        events = [{"kind": "CorporateAction", "raw": {
            "accountId": ACCOUNT, "transactionID": f"SPLIT-{i}",
            "type": "FS", "assetCategory": "STK", "conid": "4493", "symbol": "ZZSP",
            "currency": "USD", "dateTime": "20240611;120000", "quantity": str(qty),
            "actionDescription": "ZZSP SPLIT 2 FOR 1", "fxRateToBase": "1",
        }} for i, qty in enumerate((100, -100, 100))]
        ingestor = BrokerIngestor(db, ACCOUNT)
        assert ingestor.ingest_many([], "flex", events=events).corporate_inserted == 1
        assert ingestor.ingest_many([], "flex", events=events).corporate_inserted == 0
        rows = db.rows("trades", 1)
        assert sum(row["quantity"] * (1 if row["side"] == "BUY" else -1) for row in rows) == 0
        assert db.conn.execute("SELECT COUNT(*) FROM broker_source_records WHERE status='complete'").fetchone()[0] == 3
        MatchingService(db).rebuild_auto([pid])
        assert sum(row["matched_qty"] for row in db.rows("trade_matches", 1)) == 200
    finally:
        db.conn.close()


def test_option_expiration_event_links_to_zero_price_execution(tmp_path):
    db = Database(tmp_path / "expiration.sqlite3")
    try:
        ingestor = BrokerIngestor(db, ACCOUNT)
        fill = execution(source="flex", source_id="EXP", flex_trade_id="EXP",
                         ib_exec_id="EXP.01", con_id=900000004,
                         symbol="ZZZ   250117P00050000", security_type="OPT",
                         multiplier=100, side="SELL", quantity=2, price=0,
                         trade_date="2025-01-17", commission=0)
        ingestor.ingest_many([fill], "flex")
        event = {"kind": "OptionEAE", "raw": {
            "accountId": ACCOUNT, "transactionType": "Expiration", "assetCategory": "OPT",
            "conid": "900000004", "date": "20250117", "quantity": "-2",
            "tradePrice": "0", "proceeds": "0", "symbol": "ZZZ   250117P00050000",
        }}
        assert ingestor.ingest_many([], "flex", events=[event]).review == 0
        assert db.conn.execute("SELECT status FROM broker_source_records WHERE source='flex_event'").fetchone()[0] == "informational"
        assert len(db.rows("trades", 1)) == 1
    finally:
        db.conn.close()
