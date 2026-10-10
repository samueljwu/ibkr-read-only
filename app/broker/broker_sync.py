"""Account-scoped, replay-safe ingestion of IBKR execution records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Iterable
from contextlib import nullcontext
import xml.etree.ElementTree as ET


def _number(value):
    try:
        result = Decimal(str(value).replace(",", ""))
        return float(result) if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _day(value):
    value = str(value or "").strip()
    if len(value) >= 8 and value[:8].isdigit():
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()
    return date.fromisoformat(value[:10]).isoformat()


def _clock(value):
    value = str(value or "")
    if ";" in value:
        value = value.split(";", 1)[1]
    elif "T" in value:
        value = value.split("T", 1)[1]
    elif len(value) >= 8 and value[:8].isdigit():
        value = value[8:]
    digits = "".join(char for char in value if char.isdigit())[:6]
    return digits.ljust(6, "0") if digits else ""


def _effective_multiplier(security_type, value):
    """IBKR bond prices are quoted per 100 of face value."""
    multiplier = _number(value)
    if multiplier is None:
        return None
    return multiplier / 100 if security_type == "BOND" and multiplier >= 1 else multiplier


def execution_identity(record):
    """Corrections differ in the last component of the IBKR execution ID."""
    exec_id = str(record.get("ib_exec_id") or "").strip()
    if exec_id:
        parts = exec_id.rsplit(".", 1)
        base = parts[0] if len(parts) == 2 and parts[1].isdigit() else exec_id
        revision = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
        return f"E:{base}", revision
    flex_id = str(record.get("flex_trade_id") or "").strip()
    if flex_id:
        return f"F:{flex_id}", 0
    return "", 0


def record_from_tws(contract, execution, account_id: str) -> dict:
    """Normalize an ibapi execution callback without invoking any order API."""
    side = {"BOT": "BUY", "SLD": "SELL"}.get(str(execution.side).upper(),
                                            str(execution.side).upper())
    security_type = str(contract.secType).upper()
    exec_id = str(execution.execId or "").strip()
    time = str(execution.time or "")
    try:
        trade_day = _day(time)
    except (TypeError, ValueError):
        trade_day = ""
    return {
        "account_id": str(execution.acctNumber or account_id),
        "source": "tws", "source_id": exec_id, "ib_exec_id": exec_id,
        "trade_date": trade_day, "executed_at": _clock(time),
        "side": side, "quantity": _number(execution.shares),
        "price": _number(execution.price), "con_id": contract.conId,
        "symbol": str(contract.symbol or ""), "security_type": security_type,
        "currency": str(contract.currency or "").upper(),
        "multiplier": _effective_multiplier(
            security_type, contract.multiplier or ("1" if security_type in {"STK", "CASH"} else None)),
        "fx_to_usd": 1.0 if str(contract.currency).upper() == "USD" else None,
        "taxes": None,
        "commission": None, "commission_currency": str(contract.currency or "").upper(),
        "local_symbol": str(contract.localSymbol or ""),
        "expiry": str(contract.lastTradeDateOrContractMonth or ""),
        "option_right": str(contract.right or ""), "strike": _number(contract.strike),
    }


@dataclass
class IngestResult:
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    review: int = 0
    execution_review: int = 0
    legacy_inserted: int = 0
    legacy_duplicates: int = 0
    corporate_inserted: int = 0
    issues: list[str] = field(default_factory=list)
    changed_position_ids: set[int] = field(default_factory=set)


def parse_flex_xml(xml_data: bytes, expected_account: str) -> tuple[list[dict], list[dict]]:
    """Keep every execution and lifecycle event for ingestion or explicit review."""
    if len(xml_data) > 30_000_000 or b"<!DOCTYPE" in xml_data.upper():
        raise ValueError("Flex XML is too large or contains an unsupported document type")
    root = ET.fromstring(xml_data)
    statements = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "FlexStatement"]
    accounts = {node.get("accountId", "") for node in statements}
    if accounts != {expected_account}:
        raise ValueError("Flex report account does not match the selected IBKR account")
    declared_bases = {node.get("baseCurrency", "").upper() for node in statements if node.get("baseCurrency")}
    conversion_targets = {node.get("toCurrency", "").upper() for node in root.iter()
                          if node.tag.rsplit("}", 1)[-1] == "ConversionRate"}
    if declared_bases and declared_bases != {"USD"}:
        raise ValueError("Flex import requires USD base currency")
    if not declared_bases and conversion_targets != {"USD"}:
        raise ValueError("Flex report does not establish USD base currency")
    executions, events = [], []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        raw = dict(node.attrib)
        if tag in {"CorporateAction", "OptionEAE", "Transfer"}:
            if raw:
                if raw.get("accountId") and raw["accountId"] != expected_account:
                    raise ValueError("Flex event account does not match the selected IBKR account")
                events.append({"kind": tag, "raw": raw})
            continue
        if tag != "Trade":
            continue
        if raw.get("accountId") and raw["accountId"] != expected_account:
            raise ValueError("Flex execution account does not match the selected IBKR account")
        detail = (raw.get("levelOfDetail") or raw.get("levelOfDetailCode") or "").upper()
        if detail not in {"EXECUTION", "EXECUTIONS"}:
            continue
        trade_id = str(raw.get("tradeID") or raw.get("tradeId") or "").strip()
        exec_id = str(raw.get("ibExecID") or raw.get("ibExecId") or "").strip()
        source_id = trade_id or exec_id or sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        try:
            trade_date = _day(raw.get("tradeDate") or raw.get("dateTime"))
        except (TypeError, ValueError):
            trade_date = ""
        executions.append({
            "account_id": expected_account, "source": "flex", "source_id": source_id,
            "flex_trade_id": trade_id, "ib_exec_id": exec_id,
            "trade_date": trade_date,
            "executed_at": _clock(raw.get("tradeTime") or raw.get("dateTime")),
            "side": (raw.get("buySell") or raw.get("side") or "").upper(),
            "quantity": _number(raw.get("quantity")),
            "price": _number(raw.get("tradePrice") or raw.get("price")),
            "con_id": raw.get("conid") or raw.get("conId"),
            "symbol": raw.get("symbol") or raw.get("underlyingSymbol") or "",
            "security_type": (raw.get("assetCategory") or raw.get("assetClass") or "").upper(),
            "currency": (raw.get("currency") or "").upper(),
            "multiplier": _effective_multiplier(
                (raw.get("assetCategory") or "").upper(),
                raw.get("multiplier") or ("1" if raw.get("assetCategory") == "STK" else None)),
            "fx_to_usd": _number(raw.get("fxRateToBase")),
            "taxes": _number(raw.get("taxes")),
            "commission": _number(raw.get("ibCommission")),
            "commission_currency": (raw.get("ibCommissionCurrency") or "").upper(),
            "local_symbol": raw.get("localSymbol") or "",
            "expiry": raw.get("expiry") or "",
            "option_right": raw.get("putCall") or "",
            "strike": _number(raw.get("strike")),
            "raw": raw,
        })
    return executions, events


class BrokerIngestor:
    def __init__(self, db, account_id: str, portfolio_id: int = 1):
        if not account_id:
            raise ValueError("IBKR account ID is required")
        configured = db.setting("ib_account_id")
        if configured and configured != account_id:
            raise ValueError("IBKR account differs from the selected portfolio account")
        db.validate_account_ownership(account_id)
        self.db, self.account_id, self.portfolio_id = db, account_id, portfolio_id

    def ingest_many(self, records: Iterable[dict], source: str, window=None, events=(), *, transaction=True) -> IngestResult:
        if source not in {"flex", "tws", "activity_statement"}:
            raise ValueError("Unsupported IBKR execution source")
        records = list(records)
        result = IngestResult()
        conn = self.db.conn
        with conn if transaction else nullcontext():
            accepted_count = 0
            for record in records:
                if record.get("account_id") != self.account_id:
                    raise ValueError("Execution account does not match the selected account")
                if record.get("source", source) != source:
                    raise ValueError("Execution source mismatch")
                accepted_count += bool(self._ingest_one(record, source, result))
            # History arrives newest first. Retry splits once earlier purchases
            # or transferred opening positions supply their missing share basis.
            pending = [dict(kind="CorporateAction", raw=json.loads(row[0]))
                       for row in conn.execute("""SELECT raw_json FROM broker_source_records
                           WHERE account_id=? AND source='flex_event' AND status='review'
                           AND issue LIKE 'Forward split requires ledger review:%'""",
                                               (self.account_id,))]
            event_by_id = {self._event_source_id(event): event for event in pending}
            event_by_id.update((self._event_source_id(event), event) for event in events)
            split_groups = {}
            for event in event_by_id.values():
                raw = event.get("raw") or {}
                if (event.get("kind") == "CorporateAction" and raw.get("type") == "FS" and
                    re.search(r"\bSPLIT\s+\d+\s+FOR\s+\d+\b", raw.get("actionDescription") or "", re.I)):
                    group_key = (raw.get("conid"), raw.get("dateTime"),
                                 raw.get("actionDescription"))
                    split_groups.setdefault(group_key, []).append(event)
                else:
                    self._ingest_event(event, result)
            for group in split_groups.values():
                self._ingest_forward_split(group, result)
            if window is not None:
                from_day, to_day = window
                conn.execute("""INSERT INTO broker_sync_windows
                    (account_id,source,from_day,to_day,status,source_count,imported_count,issue_count)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id,source,from_day,to_day) DO UPDATE SET
                    status=excluded.status,source_count=excluded.source_count,
                    imported_count=excluded.imported_count,issue_count=excluded.issue_count,
                    completed_at=CURRENT_TIMESTAMP""",
                    (self.account_id, source, from_day, to_day,
                     "review" if result.execution_review else "complete", len(records), accepted_count,
                     result.review))
        return result

    def _ingest_event(self, event, result):
        raw = event.get("raw") or {}
        kind = event.get("kind") or "Broker event"
        raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        source_id = f"{kind}:{raw.get('transactionID') or ''}:{sha256(raw_json.encode()).hexdigest()[:16]}"
        if raw.get("accountId") and raw["accountId"] != self.account_id:
            raise ValueError("Broker event account does not match the selected account")
        if kind == "OptionEAE" and self._link_option_expiration(raw, source_id):
            return
        if (kind == "CorporateAction" and raw.get("type") in {"TC", "BM"} and
            self._ingest_cash_corporate_action(raw, source_id, result)):
            return
        if kind == "Transfer" and raw.get("assetCategory") == "CASH":
            self._record_source("flex_event", source_id, "", None, "informational", "",
                                {"raw": raw})
            return
        if kind == "Transfer" and raw.get("direction") == "IN" and raw.get("assetCategory") == "STK":
            try:
                quantity = _number(raw.get("quantity"))
                cost = _number(raw.get("cost"))
                multiplier = _number(raw.get("multiplier"))
                con_id = int(raw.get("conid"))
                trade_day = _day(raw.get("date"))
                currency = (raw.get("currency") or "").upper()
                fx = 1.0 if currency == "USD" else _number(raw.get("fxRateToBase"))
                if (not quantity or quantity <= 0 or not cost or cost <= 0 or
                    not multiplier or multiplier <= 0 or con_id <= 0 or
                    not trade_day or not raw.get("symbol") or not currency or not fx or fx <= 0):
                    raise ValueError("incomplete transferred position")
            except (TypeError, ValueError):
                pass
            else:
                trade_id = f"IBKR:{self.account_id}:LEGACY:{source_id}"
                prior = self.db.conn.execute("SELECT id FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
                if prior:
                    result.legacy_duplicates += 1
                    row_id = prior[0]
                else:
                    position_id = self._position({
                        "con_id": con_id, "symbol": raw["symbol"], "security_type": "STK",
                        "currency": currency, "multiplier": multiplier,
                    })
                    ticker = self.db.conn.execute("SELECT display_ticker FROM positions WHERE id=?",
                                                  (position_id,)).fetchone()[0]
                    row_id = self.db.conn.execute("""INSERT INTO trades
                        (portfolio_id,position_id,trade_id,trade_date,display_ticker,ib_symbol,
                         side,quantity,price,security_type,multiplier,currency,fx_to_usd,
                         commission_usd,source,notes)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (self.portfolio_id, position_id, trade_id, trade_day, ticker,
                         raw["symbol"], "BUY", quantity, cost / quantity, "STK", multiplier,
                         currency, fx, 0.0, "IBKR legacy transfer",
                         "Transferred opening position; broker cost basis used. Original acquisition date unavailable.")).lastrowid
                    result.legacy_inserted += 1
                    result.changed_position_ids.add(position_id)
                self._record_source("flex_event", source_id, f"L:{source_id}", row_id,
                                    "complete", "", {"raw": raw})
                return
        issue = f"{kind} requires ledger review"
        self._record_source("flex_event", source_id, "", None, "review", issue,
                            {"raw": raw})
        result.review += 1
        result.issues.append(issue)

    def _link_option_expiration(self, raw, source_id):
        if (raw.get("transactionType") != "Expiration" or raw.get("assetCategory") != "OPT" or
            _number(raw.get("tradePrice")) != 0 or _number(raw.get("proceeds")) != 0):
            return False
        try:
            con_id = int(raw.get("conid"))
            quantity = _number(raw.get("quantity"))
            trade_day = _day(raw.get("date"))
            if not quantity:
                return False
        except (TypeError, ValueError):
            return False
        matches = self.db.conn.execute("""SELECT t.id FROM trades t JOIN positions p ON p.id=t.position_id
            WHERE p.portfolio_id=? AND p.con_id=? AND t.trade_date=? AND t.side=?
            AND ABS(t.quantity-?)<0.000001 AND t.price=0""",
            (self.portfolio_id, con_id, trade_day, "BUY" if quantity > 0 else "SELL",
             abs(quantity))).fetchall()
        if len(matches) != 1:
            return False
        self._record_source("flex_event", source_id, "", matches[0][0],
                            "informational", "", {"raw": raw})
        return True

    def _ingest_cash_corporate_action(self, raw, source_id, result):
        category, action = raw.get("assetCategory"), raw.get("type")
        description = (raw.get("actionDescription") or "").upper()
        if not ((action == "TC" and category == "STK" and "MERGED(CASH)" in description) or
                (action == "BM" and category == "BOND" and
                 ("BOND MATURITY" in description or "REDEMPTION" in description))):
            return False
        try:
            quantity = _number(raw.get("quantity"))
            proceeds = _number(raw.get("proceeds"))
            multiplier = _effective_multiplier(category, raw.get("multiplier") or "1")
            con_id = int(raw.get("conid"))
            trade_day = _day(raw.get("dateTime"))
            currency = (raw.get("currency") or "").upper()
            fx = 1.0 if currency == "USD" else _number(raw.get("fxRateToBase"))
            txn = str(raw.get("transactionID") or "")
            if (quantity is None or quantity >= 0 or proceeds is None or proceeds <= 0 or
                multiplier is None or multiplier <= 0 or con_id <= 0 or not txn or
                not raw.get("symbol") or not currency or fx is None or fx <= 0):
                return False
            price = proceeds / -quantity / multiplier
            position_id = self._position({"con_id": con_id, "symbol": raw["symbol"],
                                          "security_type": category, "currency": currency,
                                          "multiplier": multiplier})
            trade_id = f"IBKR:{self.account_id}:CA:{action}:{txn}"
            notes = f"{raw.get('actionDescription') or action}; IBKR corporate action {txn}"
            row_id = self._insert_corporate_trade(
                trade_id, position_id, trade_day, "SELL", -quantity, price,
                category, multiplier, currency, fx, notes, result)
            if row_id is None:
                return False
        except (TypeError, ValueError, ZeroDivisionError):
            return False
        self._record_source("flex_event", source_id, f"CA:{txn}", row_id,
                            "complete", "", {"raw": raw})
        return True

    def _ingest_forward_split(self, events, result):
        raw = events[0]["raw"]
        source_ids = [self._event_source_id(event) for event in events]
        try:
            if any(event["raw"].get("accountId") != self.account_id for event in events):
                raise ValueError("account mismatch")
            description = raw.get("actionDescription") or ""
            match = re.search(r"\bSPLIT\s+(\d+)\s+FOR\s+(\d+)\b", description, re.I)
            if not match or int(match[2]) <= 0:
                raise ValueError("split ratio missing")
            ratio = Decimal(match[1]) / Decimal(match[2])
            if ratio <= 1:
                raise ValueError("not a forward split")
            con_id = int(raw.get("conid"))
            trade_day = _day(raw.get("dateTime"))
            quantity = sum(Decimal(str(event["raw"].get("quantity"))) for event in events)
            if (quantity <= 0 or raw.get("assetCategory") != "STK" or
                any(event["raw"].get("conid") != str(con_id) for event in events)):
                raise ValueError("split quantity or contract mismatch")
            position = self.db.conn.execute("SELECT id FROM positions WHERE portfolio_id=? AND con_id=?",
                                            (self.portfolio_id, con_id)).fetchone()
            if not position:
                raise ValueError("split position missing")
            position_id = position[0]
            before = self.db.conn.execute("""SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END),0)
                FROM trades WHERE position_id=? AND trade_date<?""", (position_id, trade_day)).fetchone()[0]
            stable_key = sha256(f"{con_id}|{trade_day}|{description}".encode()).hexdigest()[:16]
            trade_id = f"IBKR:{self.account_id}:CA:FS:{stable_key}"
            if abs(float(quantity - Decimal(str(before)) * (ratio - 1))) > 1e-6:
                raise ValueError("split shares do not reconcile with the pre-split holding")
            currency = (raw.get("currency") or "").upper()
            fx = 1.0 if currency == "USD" else _number(raw.get("fxRateToBase"))
            if not currency or not fx or fx <= 0:
                raise ValueError("split currency or FX missing")
            row_id = self._insert_corporate_trade(
                trade_id, position_id, trade_day, "BUY", float(quantity), 0.0,
                "STK", 1.0, currency, fx,
                f"{description}; {quantity} additional shares with zero incremental basis. Original executions retained.",
                result)
            if row_id is None:
                raise ValueError("split ledger row conflicts with source")
        except (TypeError, ValueError, InvalidOperation, ZeroDivisionError) as exc:
            issue = f"Forward split requires ledger review: {exc}"
            for event, source_id in zip(events, source_ids):
                self._record_source("flex_event", source_id, "", None, "review", issue,
                                    {"raw": event["raw"]})
            result.review += len(events)
            result.issues.append(issue)
            return
        for event, source_id in zip(events, source_ids):
            self._record_source("flex_event", source_id, f"CA:FS:{stable_key}", row_id,
                                "complete", "", {"raw": event["raw"]})

    @staticmethod
    def _event_source_id(event):
        raw = event.get("raw") or {}
        raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        return f"{event.get('kind') or 'Broker event'}:{raw.get('transactionID') or ''}:{sha256(raw_json.encode()).hexdigest()[:16]}"

    def _insert_corporate_trade(self, trade_id, position_id, trade_day, side, quantity,
                                price, category, multiplier, currency, fx, notes, result):
        old = self.db.conn.execute("""SELECT id,position_id,trade_date,side,quantity,price,multiplier
            FROM trades WHERE trade_id=?""", (trade_id,)).fetchone()
        if old:
            if (old["position_id"] != position_id or old["trade_date"] != trade_day or
                old["side"] != side or abs(old["quantity"] - quantity) > 1e-6 or
                abs(old["price"] - price) > 1e-6 or abs(old["multiplier"] - multiplier) > 1e-9):
                return None
            return old["id"]
        ticker = self.db.conn.execute("SELECT display_ticker FROM positions WHERE id=?",
                                      (position_id,)).fetchone()[0]
        row_id = self.db.conn.execute("""INSERT INTO trades
            (portfolio_id,position_id,trade_id,trade_date,display_ticker,ib_symbol,side,
             quantity,price,security_type,multiplier,currency,fx_to_usd,commission_usd,source,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.portfolio_id, position_id, trade_id, trade_day, ticker, ticker, side,
             quantity, price, category, multiplier, currency, fx, 0.0,
             "IBKR corporate action", notes)).lastrowid
        result.corporate_inserted += 1
        result.changed_position_ids.add(position_id)
        return row_id

    def _record_source(self, source, source_id, key, trade_row_id, status, issue, record):
        self.db.conn.execute("""INSERT INTO broker_source_records
            (account_id,source,source_id,execution_key,trade_row_id,status,issue,raw_json)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id,source,source_id) DO UPDATE SET
            execution_key=excluded.execution_key,trade_row_id=excluded.trade_row_id,
            status=excluded.status,issue=excluded.issue,raw_json=excluded.raw_json,
            seen_at=CURRENT_TIMESTAMP""",
            (self.account_id, source, source_id, key, trade_row_id, status, issue,
             json.dumps(record.get("raw") or record, sort_keys=True, default=str, separators=(",", ":"))))

    def _position(self, record):
        conn = self.db.conn
        con_id = int(record["con_id"])
        row = conn.execute("SELECT id FROM positions WHERE portfolio_id=? AND con_id=?",
                           (self.portfolio_id, con_id)).fetchone()
        if row:
            return row[0]
        ticker = record.get("local_symbol") if record.get("security_type") == "OPT" else record["symbol"]
        ticker = ticker or record["symbol"]
        if conn.execute("SELECT 1 FROM positions WHERE portfolio_id=? AND display_ticker=?",
                        (self.portfolio_id, ticker)).fetchone():
            ticker = f"{ticker} [{con_id}]"
        cur = conn.execute("""INSERT INTO positions
            (portfolio_id,display_ticker,ib_symbol,security_type,currency,local_symbol,
             expiry,option_right,strike,multiplier,con_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.portfolio_id, ticker, record["symbol"], record["security_type"],
             record["currency"], record.get("local_symbol") or "", record.get("expiry") or "",
             record.get("option_right") or "", record.get("strike"), record["multiplier"], con_id))
        conn.execute("INSERT INTO contracts(position_id,con_id) VALUES (?,?)", (cur.lastrowid, con_id))
        return cur.lastrowid

    def _ingest_one(self, record, source, result):
        conn = self.db.conn
        source_id = str(record.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("IBKR execution source ID is required")
        key, revision = execution_identity(record)
        problems = []
        for label in ("trade_date", "symbol", "currency", "security_type"):
            if not record.get(label):
                problems.append(f"missing {label}")
        from app.imports.import_contract import EXECUTION_TYPES
        if record.get("security_type") and record["security_type"] not in EXECUTION_TYPES:
            problems.append("unsupported instrument type")
        if not key:
            problems.append("missing broker execution identity")
        if record.get("side") not in {"BUY", "SELL"}:
            problems.append("unsupported side")
        qty, price, multiplier = (_number(record.get(name)) for name in ("quantity", "price", "multiplier"))
        if qty is None or qty == 0:
            problems.append("missing quantity")
        if price is None or price < 0:
            problems.append("missing price")
        if multiplier is None or multiplier <= 0:
            problems.append("missing multiplier")
        try:
            con_id = int(record.get("con_id"))
            if con_id <= 0:
                raise ValueError()
        except (TypeError, ValueError):
            problems.append("missing contract ID")
            con_id = None
        if problems:
            issue = ", ".join(problems)
            self._record_source(source, source_id, key, None, "review", issue, record)
            result.review += 1
            result.execution_review += 1
            result.issues.append(f"{source_id}: {issue}")
            return False
        qty = abs(qty)
        record = dict(record, con_id=con_id, quantity=qty, price=price, multiplier=multiplier)
        existing = conn.execute("SELECT * FROM broker_executions WHERE account_id=? AND execution_key=?",
                                (self.account_id, key)).fetchone()
        if not existing:
            potential = conn.execute("""SELECT t.id FROM trades t
                JOIN positions p ON p.id=t.position_id
                WHERE t.portfolio_id=? AND p.con_id=? AND t.trade_date=? AND t.side=?
                AND ABS(t.quantity-?)<0.0000001 AND ABS(t.price-?)<0.0000001
                AND (t.source='manual' OR EXISTS (
                    SELECT 1 FROM broker_source_records r WHERE r.trade_row_id=t.id
                    AND r.source<>? AND r.execution_key<>?))
                LIMIT 1""",
                (self.portfolio_id, con_id, record["trade_date"], record["side"], qty, price,
                 source, key)).fetchone()
            if potential:
                issue = "possible duplicate without a shared broker execution ID"
                self._record_source(source, source_id, key, None, "review", issue, record)
                result.review += 1
                result.execution_review += 1
                result.issues.append(f"{source_id}: {issue}")
                return False
        fx = _number(record.get("fx_to_usd"))
        if record["currency"] == "USD":
            fx = 1.0
        commission = _number(record.get("commission"))
        commission_currency = record.get("commission_currency") or record["currency"]
        commission_usd = _number(record.get("commission_usd"))
        if commission_usd is None and commission is not None:
            commission_fx = 1.0 if commission_currency == "USD" else (
                fx if commission_currency == record["currency"] else
                self.db.fx_rate(commission_currency, record["trade_date"]))
            if commission_fx is not None and commission_fx > 0:
                commission_usd = (-commission if source == "flex" else commission) * commission_fx
        # Flex/CSV tax amounts are signed cash flows. Store positive charges.
        taxes = _number(record.get('taxes', 0))
        tax_lc = -taxes if taxes is not None else None
        tax_usd = tax_lc * fx if tax_lc is not None and fx and fx > 0 else None
        if existing and tax_usd is None:
            saved = conn.execute('SELECT * FROM trades WHERE id=?',
                                 (existing['trade_row_id'],)).fetchone()
            same_execution = (revision == existing['revision'] and saved['side'] == record['side']
                              and saved['quantity'] == qty and saved['price'] == price
                              and saved['trade_date'] == record['trade_date']
                              and saved['currency'] == record['currency'])
            if same_execution:
                tax_lc, tax_usd = saved['tax_lc'], saved['tax_usd']
        pending = []
        if tax_usd is None:
            pending.append('tax')
        if fx is None or fx <= 0:
            pending.append("FX")
        if commission_usd is None:
            pending.append("commission")
        status = "complete" if not pending else "pending " + " and ".join(pending)
        if pending:
            result.review += 1
            result.execution_review += 1
            result.issues.append(f"{source_id}: {status}")
        quality = 2 if source == "flex" else 1
        source_label = ("IBKR Activity Statement" if source == "activity_statement" else "IBKR sync")
        if existing:
            row_id = existing["trade_row_id"]
            old = conn.execute("SELECT * FROM trades WHERE id=?", (row_id,)).fetchone()
            newer = revision > existing["revision"]
            richer = quality > existing["quality"] and revision >= existing["revision"]
            if newer or richer:
                manual_match = conn.execute("""SELECT 1 FROM trade_matches WHERE origin='manual'
                    AND (buy_trade_id=? OR sell_trade_id=?) LIMIT 1""", (row_id, row_id)).fetchone()
                if manual_match and (old["quantity"] != qty or old["side"] != record["side"]):
                    issue = "correction conflicts with manually allocated lots"
                    self._record_source(source, source_id, key, row_id, "review", issue, record)
                    result.review += 1
                    result.execution_review += 1
                    result.issues.append(f"{source_id}: {issue}")
                    return False
                old_con_id = conn.execute("SELECT con_id FROM positions WHERE id=?",
                                          (old["position_id"],)).fetchone()[0]
                if old_con_id != con_id:
                    issue = "correction changed contract; review position allocation"
                    self._record_source(source, source_id, key, row_id, "review", issue, record)
                    result.review += 1
                    result.execution_review += 1
                    result.issues.append(f"{source_id}: {issue}")
                    return False
                position_id = old["position_id"]
                conn.execute("""DELETE FROM trade_matches WHERE origin='auto'
                    AND (buy_trade_id=? OR sell_trade_id=?)""", (row_id, row_id))
                conn.execute("""UPDATE trades SET trade_date=?,executed_at=?,side=?,quantity=?,price=?,
                    currency=?,security_type=?,multiplier=?,fx_to_usd=?,commission_usd=?,source=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (record["trade_date"], record.get("executed_at") or "", record["side"], qty,
                     price, record["currency"], record["security_type"], multiplier,
                     fx if fx is not None else old["fx_to_usd"],
                     commission_usd if commission_usd is not None else old["commission_usd"], source_label, row_id))
                conn.execute("""UPDATE broker_executions SET latest_exec_id=?,revision=?,quality=?,
                    status=?,updated_at=CURRENT_TIMESTAMP WHERE account_id=? AND execution_key=?""",
                    (record.get("ib_exec_id") or existing["latest_exec_id"], max(revision, existing["revision"]),
                     max(quality, existing["quality"]), status, self.account_id, key))
                result.updated += 1
                result.changed_position_ids.add(position_id)
            elif quality >= existing["quality"] and revision >= existing["revision"] and (
                (tax_usd is not None and (old["tax_usd"] is None or abs(old["tax_usd"] - tax_usd) > 1e-9)) or
                (commission_usd is not None and abs(old["commission_usd"] - commission_usd) > 1e-9) or
                (commission_usd is not None and "commission" in existing["status"] and status != existing["status"]) or
                (record["security_type"] == "BOND" and abs(old["multiplier"] - multiplier) > 1e-9) or
                (fx is not None and fx > 0 and (old["fx_to_usd"] or 0) <= 0)):
                conn.execute("""UPDATE trades SET commission_usd=?,fx_to_usd=?,multiplier=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (commission_usd if commission_usd is not None else old["commission_usd"],
                     fx if fx is not None and fx > 0 else old["fx_to_usd"], multiplier, row_id))
                if record["security_type"] == "BOND":
                    conn.execute("UPDATE positions SET multiplier=? WHERE id=?", (multiplier, old["position_id"]))
                conn.execute("""UPDATE broker_executions SET status=?,updated_at=CURRENT_TIMESTAMP
                    WHERE account_id=? AND execution_key=?""", (status, self.account_id, key))
                result.updated += 1
                result.changed_position_ids.add(old["position_id"])
            else:
                result.duplicates += 1
        else:
            position_id = self._position(record)
            cur = conn.execute("""INSERT INTO trades
                (portfolio_id,position_id,trade_id,trade_date,executed_at,display_ticker,ib_symbol,
                 side,quantity,price,security_type,multiplier,currency,fx_to_usd,commission_usd,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.portfolio_id, position_id, f"IBKR:{self.account_id}:{key}",
                 record["trade_date"], record.get("executed_at") or "",
                 conn.execute("SELECT display_ticker FROM positions WHERE id=?", (position_id,)).fetchone()[0],
                 record["symbol"], record["side"], qty, price, record["security_type"], multiplier,
                 record["currency"], fx or 0.0, commission_usd or 0.0, source_label))
            row_id = cur.lastrowid
            conn.execute("""INSERT INTO broker_executions
                (account_id,execution_key,trade_row_id,latest_exec_id,revision,quality,status)
                VALUES (?,?,?,?,?,?,?)""",
                (self.account_id, key, row_id, record.get("ib_exec_id") or "", revision, quality, status))
            result.inserted += 1
            result.changed_position_ids.add(position_id)
        # Lower quality or older callbacks must not erase verified Flex taxes.
        if not existing or revision > existing['revision'] or (quality >= existing['quality'] and revision >= existing['revision']):
            accepted_fx = conn.execute('SELECT fx_to_usd FROM trades WHERE id=?', (row_id,)).fetchone()[0]
            tax_usd = tax_lc * accepted_fx if tax_lc is not None and accepted_fx and accepted_fx > 0 else None
            conn.execute('UPDATE trades SET tax_lc=?,tax_usd=? WHERE id=?', (tax_lc, tax_usd, row_id))
        self._record_source(source, source_id, key, row_id, status, "", record)
        return True
