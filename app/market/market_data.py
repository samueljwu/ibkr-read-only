"""One read-only market-data provider per application, with UI-safe event polling."""

import math
import queue
import threading
from datetime import date, datetime, timedelta

import pandas as pd

from app.finance.calculations import choose_price, looks_like_option_ticker, valid_price
from app.broker.broker_sync import record_from_tws

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.execution import ExecutionFilter
    from ibapi.wrapper import EWrapper
except ImportError:  # Mock mode works without TWS/API installed.
    EClient = Contract = ExecutionFilter = EWrapper = None


class MarketDataProvider:
    def __init__(self):
        self.events = queue.Queue()
        self.quotes = {}
        self.connected = False
        self.subscriptions = 0
        self.other_events = []
        self.search_results = {}
        self.search_errors = {}

    def quote(self, position_id):
        return self.quotes.get(position_id, {})

    def drain(self):
        changes = set()
        # A busy streaming feed must not keep the Qt timer callback running forever.
        for _ in range(500):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "quote":
                _, ids, tick, value, mode = event
                for position_id in ids:
                    record = self.quotes.setdefault(position_id, {"ticks": {}})
                    if tick and valid_price(value) is not None:
                        stamp = datetime.now().astimezone()
                        record["ticks"][tick] = float(value)
                        record["updated"] = stamp.replace(tzinfo=None).isoformat(timespec="seconds")
                        record.setdefault("tick_updated", {})[tick] = stamp.isoformat(timespec="seconds")
                        record["status"] = "DELAYED" if mode == "Delayed" else "OK"
                    elif not record.get("status"):
                        record["status"] = "WAITING FOR PRICE"
                    record["mode"] = mode
                    changes.add(position_id)
            elif kind == "status":
                _, ids, status = event
                for position_id in ids:
                    record = self.quotes.setdefault(position_id, {"ticks": {}})
                    record["status"] = status
                    changes.add(position_id)
            elif kind == "connected":
                self.connected = event[1]
                if not self.connected:
                    for position_id, record in self.quotes.items():
                        price, _ = choose_price(record.get("ticks", {}), record.get("mode", "Live"))
                        if price is not None:
                            record["cached_price"] = price
                            record["cached_as_of"] = record.get("updated", "")
                        record["ticks"] = {}
                        record["tick_updated"] = {}
                        record["mode"] = "Cached"
                        changes.add(position_id)
            elif kind == "search_results":
                self.search_results[event[1]] = event[2]
            elif kind == "search_error":
                self.search_errors[event[1]] = event[2]
            else:
                self.other_events.append(event)
        return changes


class MockMarketDataProvider(MarketDataProvider):
    def __init__(self):
        super().__init__()
        self.connected = True

    def refresh(self, positions):
        keys = set()
        for position in positions:
            if position["security_type"] == "STK" and looks_like_option_ticker(position["display_ticker"]):
                self.quotes[position["id"]] = {"ticks": {}, "mode": "Mock",
                                                "status": "VERIFY SECURITY TYPE", "updated": ""}
                continue
            if position["security_type"] != "STK":
                self.quotes[position["id"]] = {"ticks": {}, "mode": "Mock",
                                                "status": "MOCK PRICE UNAVAILABLE", "updated": ""}
                continue
            symbol = (position["ib_symbol"] or position["display_ticker"].split(" ")[0]).upper()
            if not symbol:
                continue
            keys.add(symbol)
            # Stable sample quotes; safe for keyboard/UI testing, never presented as live.
            seed = sum(ord(c) for c in symbol)
            close = round(25 + seed % 170 + (seed % 17) / 10, 2)
            last = round(close * (1 + ((seed % 11) - 5) / 1000), 2)
            days = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=270)
            history = [{"day": stamp.date().isoformat(),
                        "close": round(close * (0.94 + 0.06 * (index + 1) / len(days)), 2),
                        "volume": float(200000 + seed * 100 + (index % 20) * 1000)}
                       for index, stamp in enumerate(days)]
            self.quotes[position["id"]] = {"ticks": {"last": last, "close": close},
                                            "prior_close": close, "mode": "Mock", "status": "MOCK",
                                            "history": history,
                                            "updated": datetime.now().strftime("%H:%M:%S")}
        self.subscriptions = len(keys)
        return {p["id"] for p in positions}


if EWrapper is not None:
    class _IBClient(EWrapper, EClient):
        def __init__(self, owner):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.owner = owner
            self.next_id = 1000
            self.pending = {}
            self.found_details = {}
            self.ticker_ids = {}
            self.history_ids = {}
            self.bars = {}
            self.contracts = {}
            self.mode_by_request = {}
            self.position_snapshot = {}
            self.positions_initial_complete = False
            self.managed_accounts = []
            self.fx_requests = {}
            self.fx_history_ids = {}
            self.benchmark_history_id = None
            self.contract_searches = set()
            self.exposure_details = {}
            self.exposure_underlying = {}
            self.exposure_underlying_ticks = {}
            self.exposure_requested = set()
            self.exposure_underlying_last = set()
            self.exposure_underlying_parents = {}
            self.exposure_underlying_marks = {}
            self.exposure_option_quotes = {}
            self.exposure_model_delta = set()
            self.exposure_calculations = {}
            self.exposure_calc_requested = set()
            self.execution_records = {}
            self.commission_reports = {}
            self.execution_request_id = None
            self.execution_count = 0
            self.pnl_request_id = None
            self.pnl_account = None
            self.managed_account = None
            self.ready = False

        def nextValidId(self, orderId):  # callback name is fixed by ibapi
            self.ready = True
            self.owner.events.put(("connected", True))
            self.reqMarketDataType({"Live": 1, "Frozen": 2, "Delayed": 3}.get(self.owner.preference, 1))
            self.reqPositions()
            self.reqAccountSummary(901, "All", "NetLiquidation,TotalCashValue")
            self.owner._request_all()
            if self.owner.account_id or self.managed_account:
                self._request_pnl(self.owner.account_id or self.managed_account)
            if self.owner.account_id:
                request = ExecutionFilter()
                request.acctCode = self.owner.account_id
                self.execution_request_id = self.owner._new_id()
                self.reqExecutions(self.execution_request_id, request)

        def execDetails(self, reqId, contract, execution):
            if reqId != self.execution_request_id:
                return
            if execution.acctNumber and execution.acctNumber != self.owner.account_id:
                return
            try:
                record = record_from_tws(contract, execution, self.owner.account_id)
                commission = self.commission_reports.get(record["ib_exec_id"])
                if commission:
                    record.update(commission)
                self.execution_records[record["ib_exec_id"]] = record
                self.execution_count += 1
                self.owner.events.put(("execution", dict(record)))
            except Exception as exc:
                self.owner.events.put(("execution_error", str(exc)))

        def commissionReport(self, commissionReport):
            exec_id = str(commissionReport.execId or "")
            try:
                amount = float(commissionReport.commission)
            except (TypeError, ValueError, OverflowError):
                return
            if not -1e12 < amount < 1e12:
                return
            commission = {"commission": amount,
                          "commission_currency": str(commissionReport.currency or "").upper()}
            self.commission_reports[exec_id] = commission
            record = self.execution_records.get(exec_id)
            if record:
                record.update(commission)
                self.owner.events.put(("execution", dict(record)))

        def execDetailsEnd(self, reqId):
            if reqId == self.execution_request_id:
                self.owner.events.put(("executions_end", self.execution_count))

        def managedAccounts(self, accountsList):
            accounts = [part.strip() for part in accountsList.split(",") if part.strip()]
            self.managed_accounts = accounts
            self.owner.events.put(("accounts", accounts))
            if not self.owner.account_id and len(accounts) == 1:
                self.managed_account = accounts[0]
                if self.ready:
                    self._request_pnl(accounts[0])

        def _request_pnl(self, account):
            if self.pnl_request_id is None:
                self.pnl_request_id = self.owner._new_id()
                self.pnl_account = account
                self.reqPnL(self.pnl_request_id, account, "")

        def pnl(self, reqId, dailyPnL, unrealizedPnL, realizedPnL):
            if reqId == self.pnl_request_id:
                try:
                    value = float(dailyPnL)
                except (TypeError, ValueError, OverflowError):
                    return
                self.owner.events.put(("daily_pnl", self.pnl_account, value))

        def position(self, account, contract, position, avgCost):
            row = {
                "con_id": contract.conId, "quantity": float(position), "symbol": contract.symbol,
                "security_type": contract.secType, "currency": contract.currency,
                "exchange": contract.exchange, "primary_exchange": contract.primaryExchange,
                "local_symbol": contract.localSymbol,
                "expiry": contract.lastTradeDateOrContractMonth, "option_right": contract.right,
                "strike": contract.strike, "trading_class": contract.tradingClass,
                "multiplier": contract.multiplier}
            if self.positions_initial_complete:
                self.owner.events.put(("broker_position_delta", account, row))
            else:
                self.position_snapshot.setdefault(account, []).append(row)

        def positionEnd(self):
            self.owner.events.put(("broker_positions", self.position_snapshot, self.managed_accounts))
            self.position_snapshot = {}
            self.positions_initial_complete = True

        def accountSummary(self, reqId, account, tag, value, currency):
            if tag in ("NetLiquidation", "TotalCashValue"):
                self.owner.events.put(("account_value", account, tag, value, currency))

        def accountSummaryEnd(self, reqId):
            if reqId == 901:
                self.owner.events.put(("account_summary_end",))

        def symbolSamples(self, reqId, contractDescriptions):
            self.owner.search_requests.discard(reqId)
            results = []
            for item in contractDescriptions:
                contract = item.contract
                results.append({"con_id": contract.conId, "symbol": contract.symbol,
                                "security_type": contract.secType, "currency": contract.currency,
                                "primary_exchange": contract.primaryExchange,
                                "local_symbol": contract.localSymbol,
                                "description": getattr(item, "description", "")})
            self.owner.events.put(("search_results", reqId, results))

        def connectionClosed(self):
            self.owner.events.put(("connected", False))
            self.owner.events.put(("status", list(self.owner.positions), "DISCONNECTED"))

        def contractDetails(self, reqId, contractDetails):
            if reqId in self.exposure_details or reqId in self.exposure_underlying:
                self.found_details.setdefault(reqId, []).append(contractDetails)
            elif reqId in self.pending:
                self.found_details.setdefault(reqId, []).append(contractDetails)
            elif reqId in self.contract_searches:
                self.found_details.setdefault(reqId, []).append(contractDetails)

        def contractDetailsEnd(self, reqId):
            if reqId in self.exposure_details:
                con_id = self.exposure_details.pop(reqId)
                details = self.found_details.pop(reqId, [])
                if len(details) == 1:
                    detail = details[0]
                    under_id = int(getattr(detail, "underConId", 0) or 0)
                    self.owner.events.put(("exposure_detail", con_id, {
                        "sector": str(getattr(detail, "industry", "") or ""),
                        "category": str(getattr(detail, "category", "") or ""),
                        "stock_type": str(getattr(detail, "stockType", "") or ""),
                        "under_con_id": under_id or None,
                        "under_symbol": str(getattr(detail, "underSymbol", "") or "")}))
                    if detail.contract.secType == "OPT" and under_id:
                        self.exposure_underlying_parents.setdefault(under_id, set()).add(con_id)
                        self.owner._request_exposure_underlying(under_id, con_id)
                return
            if reqId in self.exposure_underlying:
                under_id, parent_id = self.exposure_underlying.pop(reqId)
                details = self.found_details.pop(reqId, [])
                if len(details) == 1 and details[0].contract.secType == "STK":
                    detail = details[0]
                    self.owner.events.put(("exposure_detail", parent_id, {
                        "sector": str(getattr(detail, "industry", "") or ""),
                        "category": str(getattr(detail, "category", "") or ""),
                        "stock_type": str(getattr(detail, "stockType", "") or ""),
                        "under_symbol": str(detail.contract.symbol or ""),
                        "under_con_id": under_id}))
                    self.owner._subscribe_exposure_underlying(detail.contract, parent_id)
                return
            if reqId in self.contract_searches:
                self.contract_searches.remove(reqId)
                results = []
                for detail in self.found_details.pop(reqId, []):
                    contract = detail.contract
                    results.append({"con_id": contract.conId, "symbol": contract.symbol,
                                    "security_type": contract.secType, "currency": contract.currency,
                                    "primary_exchange": contract.primaryExchange,
                                    "local_symbol": contract.localSymbol,
                                    "expiry": contract.lastTradeDateOrContractMonth,
                                    "option_right": contract.right, "strike": contract.strike,
                                    "trading_class": contract.tradingClass,
                                    "multiplier": contract.multiplier})
                self.owner.events.put(("search_results", reqId, results))
                return
            if reqId in self.pending:
                position_ids = self.pending.pop(reqId)
                details = self.found_details.pop(reqId, [])
                if len(details) != 1:
                    self.owner.events.put(("status", position_ids, "CONTRACT ERROR"))
                    return
                contract = details[0].contract
                for position_id in position_ids:
                    self.owner.events.put(("contract", position_id, contract.conId,
                                           getattr(details[0], "timeZoneId", ""),
                                           getattr(contract, "multiplier", ""),
                                           {"symbol": contract.symbol, "security_type": contract.secType,
                                            "currency": contract.currency, "exchange": contract.exchange,
                                            "primary_exchange": contract.primaryExchange,
                                            "local_symbol": contract.localSymbol,
                                            "expiry": contract.lastTradeDateOrContractMonth,
                                            "option_right": contract.right, "strike": contract.strike,
                                            "trading_class": contract.tradingClass}))
                    self.owner._subscribe(position_id, contract)

        def tickPrice(self, reqId, tickType, price, attrib):
            if reqId in self.exposure_underlying_ticks:
                if tickType in (4, 9, 68, 75) and valid_price(price) is not None:
                    if tickType in (9, 75) and reqId in self.exposure_underlying_last:
                        return
                    if tickType in (4, 68):
                        self.exposure_underlying_last.add(reqId)
                    under_id, parent_id, currency = self.exposure_underlying_ticks[reqId]
                    if tickType in (9, 75) and parent_id in self.exposure_underlying_marks:
                        return
                    self._exposure_underlying_mark(parent_id, price, currency,
                                                   self.mode_by_request.get(reqId, self.owner.preference))
                return
            contract = self.contracts.get(reqId)
            if contract is not None and contract.secType == "OPT" and contract.conId:
                con_id = int(contract.conId)
                side = {1: "bid", 2: "ask", 4: "last",
                        66: "bid", 67: "ask", 68: "last"}.get(tickType)
                if side and valid_price(price) is not None:
                    self.exposure_option_quotes.setdefault(con_id, {})[side] = float(price)
                    self._maybe_calculate_exposure(con_id, contract)
            if contract is not None and contract.secType == "STK" and contract.conId:
                if tickType in (4, 9, 68, 75) and valid_price(price) is not None:
                    if tickType in (4, 68):
                        self.exposure_underlying_last.add(reqId)
                    if tickType not in (9, 75) or reqId not in self.exposure_underlying_last:
                        for parent_id in self.exposure_underlying_parents.get(int(contract.conId), ()):
                            if tickType in (9, 75) and parent_id in self.exposure_underlying_marks:
                                continue
                            self._exposure_underlying_mark(
                                parent_id, price, contract.currency or "USD",
                                self.mode_by_request.get(reqId, self.owner.preference))
            field = {4: "last", 9: "close", 68: "delayed_last", 75: "delayed_close"}.get(tickType)
            if field and valid_price(price) is not None:
                if reqId in self.fx_requests:
                    self.owner.events.put(("fx", self.fx_requests[reqId], float(price)))
                    return
                ids = self.ticker_ids.get(reqId, set())
                mode = self.mode_by_request.get(reqId, self.owner.preference)
                self.owner.events.put(("quote", list(ids), field, price, mode))

        def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol,
                                  delta, optPrice, pvDividend, gamma, vega, theta, undPrice):
            calculation = self.exposure_calculations.pop(reqId, None)
            if calculation:
                con_id, contract, phase, underlying_price, currency = calculation
                if phase == "volatility":
                    if isinstance(impliedVol, (int, float)) and math.isfinite(impliedVol) and impliedVol > 0:
                        next_id = self.owner._new_id()
                        self.exposure_calculations[next_id] = (
                            con_id, contract, "greeks", underlying_price, currency)
                        self.calculateOptionPrice(next_id, contract, float(impliedVol),
                                                  underlying_price, [])
                    else:
                        self.owner.events.put(("exposure_calculation_error", con_id,
                                               "IBKR did not return a usable implied volatility"))
                    return
                if tickType == 53 and isinstance(delta, (int, float)) and math.isfinite(delta) and -1 <= delta <= 1:
                    if con_id not in self.exposure_model_delta:
                        self.owner.events.put(("exposure_delta", con_id, float(delta), "IBKR calculated"))
                else:
                    self.owner.events.put(("exposure_calculation_error", con_id,
                                           "IBKR did not return a usable calculated delta"))
                return
            if tickType not in (13, 83):
                return
            contract = self.contracts.get(reqId)
            if contract is None or contract.secType != "OPT" or not contract.conId:
                return
            con_id = int(contract.conId)
            mode = "Delayed" if tickType == 83 else self.mode_by_request.get(reqId, self.owner.preference)
            model_valid = (isinstance(delta, (int, float)) and
                           math.isfinite(delta) and -1 <= delta <= 1)
            if model_valid:
                self.exposure_model_delta.add(con_id)
                self.owner.events.put(("exposure_delta", con_id, float(delta), mode))
            if valid_price(undPrice) is not None:
                self._exposure_underlying_mark(con_id, undPrice,
                                               getattr(contract, "currency", "") or "USD", mode)
            if not model_valid:
                self._maybe_calculate_exposure(con_id, contract)

        def _exposure_underlying_mark(self, con_id, price, currency, mode):
            self.exposure_underlying_marks[con_id] = (float(price), currency, mode)
            self.owner.events.put(("exposure_underlying_price", con_id, float(price), currency, mode))
            contract = next((item for item in self.contracts.values()
                             if item.secType == "OPT" and int(item.conId or 0) == con_id), None)
            if contract is not None:
                self._maybe_calculate_exposure(con_id, contract)

        def _maybe_calculate_exposure(self, con_id, contract):
            if con_id in self.exposure_model_delta or con_id in self.exposure_calc_requested:
                return
            quote = self.exposure_option_quotes.get(con_id, {})
            mark = self.exposure_underlying_marks.get(con_id)
            bid, ask = quote.get("bid"), quote.get("ask")
            option_price = ((bid + ask) / 2 if bid and ask and ask >= bid
                            else quote.get("last"))
            if not mark or not option_price:
                return
            self.exposure_calc_requested.add(con_id)
            request_id = self.owner._new_id()
            self.exposure_calculations[request_id] = (
                con_id, contract, "volatility", mark[0], mark[1])
            self.calculateImpliedVolatility(request_id, contract, option_price,
                                            mark[0], [])

        def marketDataType(self, reqId, marketDataType):
            mode = {1: "Live", 2: "Frozen", 3: "Delayed", 4: "Delayed"}.get(marketDataType, "Live")
            self.mode_by_request[reqId] = mode
            self.owner.events.put(("quote", list(self.ticker_ids.get(reqId, [])), None, None, mode))

        def historicalData(self, reqId, bar):
            if reqId == self.benchmark_history_id:
                day = str(bar.date).strip().replace("-", "")[:8]
                try:
                    iso = date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:8]}").isoformat()
                    self.bars.setdefault(reqId, []).append({"day": iso, "close": float(bar.close)})
                except (ValueError, TypeError):
                    pass
                return
            if reqId in self.fx_history_ids:
                day = str(bar.date).strip().replace("-", "")[:8]
                try:
                    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
                    self.bars.setdefault(reqId, []).append({"day": iso, "close": float(bar.close)})
                except (ValueError, TypeError):
                    pass
                return
            if reqId in self.history_ids:
                day = str(bar.date).strip().replace("-", "")[:8]
                try:
                    iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
                    self.bars.setdefault(reqId, []).append({"day": iso, "close": float(bar.close),
                                                             "volume": float(bar.volume)})
                except (ValueError, TypeError):
                    pass

        def historicalDataEnd(self, reqId, start, end):
            if reqId == self.benchmark_history_id:
                self.benchmark_history_id = None
                self.owner.events.put(("benchmark_history", self.bars.pop(reqId, [])))
                return
            if reqId in self.fx_history_ids:
                currency = self.fx_history_ids.pop(reqId)
                self.owner.events.put(("fx_history", currency, self.bars.pop(reqId, [])))
                return
            if reqId in self.history_ids:
                con_id = self.history_ids.pop(reqId)
                self.owner.events.put(("history", con_id, self.bars.pop(reqId, [])))

        def error(self, reqId, *parts):
            # ibapi releases differ on whether an error timestamp precedes the code.
            if len(parts) >= 3 and isinstance(parts[1], int) and isinstance(parts[2], str):
                errorCode, errorString = parts[1], parts[2]
            elif len(parts) >= 2:
                errorCode, errorString = parts[0], parts[1]
            else:
                return
            if errorCode in (2104, 2106, 2158):  # normal farm status messages
                return
            if errorCode == 1100:
                self.owner.events.put(("connected", False))
                return
            if errorCode in (1101, 1102):
                self.owner.events.put(("connected", True))
                if errorCode == 1101:
                    for ticker_id, contract in list(self.contracts.items()):
                        self.reqMktData(ticker_id, contract, "", False, False, [])
                    for ticker_id, currency in list(self.fx_requests.items()):
                        contract = Contract()
                        contract.symbol, contract.secType = currency, "CASH"
                        contract.currency, contract.exchange = "USD", "IDEALPRO"
                        self.reqMktData(ticker_id, contract, "", False, False, [])
                    for ticker_id, (under_id, _, _) in list(self.exposure_underlying_ticks.items()):
                        contract = Contract()
                        contract.conId, contract.secType, contract.exchange = under_id, "STK", "SMART"
                        self.reqMktData(ticker_id, contract, "", False, False, [])
                self.owner.refresh_live()
                return
            calculation = self.exposure_calculations.pop(reqId, None)
            if calculation:
                self.owner.events.put(("exposure_calculation_error", calculation[0],
                                       f"IBKR ERROR {errorCode}: {errorString}"))
                return
            if reqId == self.benchmark_history_id:
                self.benchmark_history_id = None
                self.bars.pop(reqId, None)
                self.owner.events.put(("benchmark_error", f"IBKR ERROR {errorCode}: {errorString}"))
                return
            if errorCode in (354, 10167) and self.owner.allow_delayed and not self.owner.delayed_requested:
                self.owner.delayed_requested = True
                ids = list(self.ticker_ids.get(reqId, []))
                if ids:
                    self.owner.events.put(("status", ids,
                                           f"IBKR ERROR {errorCode}: {errorString}; requesting delayed prices"))
                self.reqMarketDataType(3)
                for ticker_id, contract in list(self.contracts.items()):
                    self.cancelMktData(ticker_id)
                    self.reqMktData(ticker_id, contract, "", False, False, [])
                for ticker_id, (under_id, _, _) in list(self.exposure_underlying_ticks.items()):
                    contract = Contract()
                    contract.conId = under_id
                    contract.secType = "STK"
                    contract.exchange = "SMART"
                    self.cancelMktData(ticker_id)
                    self.reqMktData(ticker_id, contract, "", False, False, [])
                return
            if reqId in self.owner.search_requests:
                self.owner.search_requests.discard(reqId)
                self.owner.events.put(("search_error", reqId, f"IBKR ERROR {errorCode}: {errorString}"))
                return
            ids = list(self.ticker_ids.get(reqId, []))
            if reqId in self.pending:
                ids = self.pending.pop(reqId)
            status = (f"NO MARKET DATA PERMISSION (IBKR {errorCode}: {errorString})"
                      if errorCode in (354, 10167) else f"IBKR ERROR {errorCode}: {errorString}")
            if ids:
                self.owner.events.put(("status", ids, status))


class IBKRMarketDataProvider(MarketDataProvider):
    def __init__(self, host, port, client_id, preference="Live", allow_delayed=True,
                 account_id=""):
        super().__init__()
        if EWrapper is None:
            raise RuntimeError("ibapi is not installed; install requirements.txt to use IBKR mode")
        self.host, self.port, self.client_id = host, int(port), int(client_id)
        self.account_id = account_id
        self.preference, self.allow_delayed = preference, allow_delayed
        self.delayed_requested = False
        self.positions = {}
        self.missing_trade_fx = set()
        self.history_last_days = {}
        self.benchmark_last_day = None
        self.by_contract = {}
        self.idea_contracts = {}
        self.requested_fx = set()
        self.search_requests = set()
        self.client = _IBClient(self)
        self.thread = None

    def start(self, positions, stale_con_ids=None, trades=None, benchmark_last_day=None,
              history_last_days=None):
        self.positions = {p["id"]: dict(p) for p in positions}
        self.benchmark_last_day = benchmark_last_day
        self.missing_trade_fx = {t["currency"] for t in (trades or [])
                                 if t.get("currency") not in (None, "USD") and
                                 not valid_price(t.get("fx_to_usd"))}
        self.stale_con_ids = set(stale_con_ids or [])
        self.history_last_days = history_last_days or {}
        self.thread = threading.Thread(target=self._run, daemon=True, name="IBKR network")
        self.thread.start()

    def _run(self):
        try:
            self.client.connect(self.host, self.port, self.client_id)
            if self.client.isConnected():
                self.client.run()
            else:
                self.events.put(("status", list(self.positions), "DISCONNECTED"))
        except Exception as exc:
            self.events.put(("status", list(self.positions), f"DISCONNECTED: {exc}"))
        finally:
            self.events.put(("connected", False))
            self.events.put(("status", list(self.positions), "DISCONNECTED"))

    def _new_id(self):
        self.client.next_id += 1
        return self.client.next_id

    def refresh_live(self):
        """Request current account state on the existing socket."""
        if not self.connected and not self.client.isConnected():
            return
        self.client.positions_initial_complete = False
        self.client.position_snapshot = {}
        self.client.cancelPositions()
        self.client.reqPositions()
        self.client.cancelAccountSummary(901)
        self.client.reqAccountSummary(901, "All", "NetLiquidation,TotalCashValue")
        if self.account_id:
            request = ExecutionFilter()
            request.acctCode = self.account_id
            self.client.execution_count = 0
            self.client.execution_request_id = self._new_id()
            self.client.reqExecutions(self.client.execution_request_id, request)

    def search(self, text):
        if not self.connected:
            raise RuntimeError("Connect to TWS before searching instruments")
        request_id = self._new_id()
        self.search_requests.add(request_id)
        self.client.reqMatchingSymbols(request_id, text)
        return request_id

    def lookup_con_id(self, con_id):
        if not self.connected:
            raise RuntimeError("Connect to TWS before looking up a contract")
        request_id = self._new_id()
        contract = Contract()
        contract.conId = int(con_id)
        contract.exchange = "SMART"
        self.client.contract_searches.add(request_id)
        self.client.reqContractDetails(request_id, contract)
        return request_id

    def _request_all(self, position_ids=None):
        pending_by_key = {}
        for position_id, position in self.positions.items():
            if position_ids is not None and position_id not in position_ids:
                continue
            if position["security_type"] == "STK" and looks_like_option_ticker(position["display_ticker"]):
                self.events.put(("status", [position_id], "VERIFY SECURITY TYPE"))
                continue
            if not position["ib_symbol"]:
                self.events.put(("status", [position_id], "CONTRACT ERROR"))
                continue
            contract = Contract()
            contract.symbol = position["ib_symbol"]
            contract.secType = position["security_type"]
            contract.exchange = position["exchange"]
            contract.primaryExchange = position["primary_exchange"]
            contract.currency = position["currency"]
            if position.get("local_symbol"):
                contract.localSymbol = position["local_symbol"]
            if position.get("expiry"):
                contract.lastTradeDateOrContractMonth = position["expiry"]
            if position.get("option_right"):
                contract.right = position["option_right"]
            if position.get("strike") is not None:
                contract.strike = float(position["strike"])
            if position.get("trading_class"):
                contract.tradingClass = position["trading_class"]
            if contract.secType != "STK" and position.get("multiplier"):
                contract.multiplier = format(float(position["multiplier"]), "g")
            if contract.secType == "OPT" and not position.get("con_id") and not (
                position.get("local_symbol") or
                (position.get("expiry") and position.get("option_right") and position.get("strike") is not None)):
                self.events.put(("status", [position_id], "CONTRACT DETAILS REQUIRED"))
                continue
            if position.get("con_id") and position.get("multiplier"):
                contract.conId = position["con_id"]
                self._subscribe(position_id, contract)
            else:
                if position.get("con_id"):
                    contract.conId = position["con_id"]
                key = (contract.symbol, contract.secType, contract.exchange,
                       contract.primaryExchange, contract.currency, contract.localSymbol,
                       contract.lastTradeDateOrContractMonth, contract.right,
                       contract.strike, contract.tradingClass, contract.conId)
                if key in pending_by_key:
                    self.client.pending[pending_by_key[key]].append(position_id)
                    continue
                request_id = self._new_id()
                pending_by_key[key] = request_id
                self.client.pending[request_id] = [position_id]
                self.client.reqContractDetails(request_id, contract)
        if position_ids is not None:
            for currency in {self.positions[key]["currency"] for key in position_ids
                             if self.positions[key].get("currency") not in (None, "USD")}:
                self._subscribe_fx(currency)
            return
        for idea_id, details in list(self.idea_contracts.items()):
            self._subscribe_idea(idea_id, details)
        currencies = ({p["currency"] for p in self.positions.values()
                       if p.get("currency") and p["currency"] != "USD"} |
                      {d["currency"] for d in list(self.idea_contracts.values())
                       if d.get("currency") and d["currency"] != "USD"} |
                      self.missing_trade_fx)
        for currency in sorted(currencies):
            contract = self._subscribe_fx(currency)
            if currency in self.missing_trade_fx:
                history_id = self._new_id()
                self.client.fx_history_ids[history_id] = currency
                self.client.reqHistoricalData(history_id, contract, "", "5 Y", "1 day", "MIDPOINT", 0, 1, False, [])
        self._request_benchmark_history()

    def _request_benchmark_history(self):
        # Use only completed daily closes; reconnects catch up after offline periods.
        try:
            last = date.fromisoformat(self.benchmark_last_day) if self.benchmark_last_day else None
        except ValueError:
            last = None
        if last and last >= date.today() - timedelta(days=1):
            return
        contract = Contract()
        contract.symbol = "SPX"
        contract.secType = "IND"
        contract.currency = "USD"
        contract.exchange = "CBOE"
        request_id = self._new_id()
        self.client.benchmark_history_id = request_id
        duration = "1 Y" if last and (date.today() - last).days <= 366 else "10 Y"
        self.client.reqHistoricalData(request_id, contract, "", duration, "1 day",
                                      "TRADES", 1, 1, False, [])

    def _subscribe_fx(self, currency):
        contract = Contract()
        contract.symbol = currency
        contract.secType = "CASH"
        contract.currency = "USD"
        contract.exchange = "IDEALPRO"
        if currency not in self.requested_fx:
            request_id = self._new_id()
            self.client.fx_requests[request_id] = currency
            self.client.reqMktData(request_id, contract, "", False, False, [])
            self.requested_fx.add(currency)
        return contract

    def add_positions(self, positions):
        added = set()
        for position in positions:
            if position["id"] not in self.positions:
                self.positions[position["id"]] = dict(position)
                added.add(position["id"])
        if added and self.connected:
            self._request_all(added)

    def _subscribe(self, position_id, contract):
        key = int(contract.conId) if contract.conId else (contract.symbol, contract.secType,
                                                          contract.exchange, contract.primaryExchange, contract.currency)
        if key in self.by_contract:
            self.client.ticker_ids[self.by_contract[key]].add(position_id)
            return
        request_id = self._new_id()
        self.by_contract[key] = request_id
        self.client.ticker_ids[request_id] = {position_id}
        self.client.contracts[request_id] = contract
        self.client.reqMktData(request_id, contract, "", False, False, [])
        if contract.conId and contract.secType in ("STK", "OPT") and int(contract.conId) not in self.client.exposure_requested:
            self.client.exposure_requested.add(int(contract.conId))
            detail_id = self._new_id()
            self.client.exposure_details[detail_id] = int(contract.conId)
            self.client.reqContractDetails(detail_id, contract)
        self.subscriptions = len(self.by_contract)
        needs_history = (position_id in self.positions and contract.conId and
                         (int(contract.conId) in self.stale_con_ids or
                          not self.positions.get(position_id, {}).get("con_id")))
        if needs_history:
            history_id = self._new_id()
            self.client.history_ids[history_id] = int(contract.conId)
            duration = "1 Y"
            try:
                previous = date.fromisoformat(self.history_last_days[int(contract.conId)])
                missing_days = (date.today() - previous).days + 7
                if missing_days <= 365:
                    duration = f"{max(7, missing_days)} D"
            except (KeyError, TypeError, ValueError):
                pass
            self.client.reqHistoricalData(history_id, contract, "", duration, "1 day", "TRADES", 1, 1, False, [])

    def _request_exposure_underlying(self, under_id, parent_id):
        contract = Contract()
        contract.conId = int(under_id)
        contract.secType = "STK"
        contract.exchange = "SMART"
        request_id = self._new_id()
        self.client.exposure_underlying[request_id] = (under_id, parent_id)
        self.client.reqContractDetails(request_id, contract)

    def _subscribe_exposure_underlying(self, contract, parent_id):
        request_id = self._new_id()
        self.client.exposure_underlying_ticks[request_id] = (
            int(contract.conId), parent_id, str(contract.currency or "USD"))
        self.client.reqMktData(request_id, contract, "", False, False, [])

    def stop(self):
        # Socket teardown can wait on a signed-out Gateway. Never do it on Qt's thread.
        client = self.client
        def disconnect():
            try:
                client.disconnect()
            except Exception:
                pass
        threading.Thread(target=disconnect, daemon=True, name="IBKR disconnect").start()

    def _subscribe_idea(self, idea_id, details):
        if not details.get("con_id"):
            return
        contract = Contract()
        contract.conId = int(details["con_id"])
        contract.symbol = details["symbol"]
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.primaryExchange = details.get("primary_exchange", "")
        contract.currency = details["currency"]
        self._subscribe(idea_id, contract)

    def set_idea_contracts(self, ideas):
        desired = {f"idea:{symbol}": dict(details) for symbol, details in ideas.items()
                   if details.get("con_id") and details.get("security_type") == "STK"}
        changed = {idea_id for idea_id in set(desired) & set(self.idea_contracts)
                   if desired[idea_id].get("con_id") != self.idea_contracts[idea_id].get("con_id")}
        removed = (set(self.idea_contracts) - set(desired)) | changed
        for idea_id in removed:
            for request_id, ids in list(self.client.ticker_ids.items()):
                ids.discard(idea_id)
                if not ids and request_id in self.client.contracts:
                    self.client.cancelMktData(request_id)
                    del self.client.ticker_ids[request_id]
                    self.client.contracts.pop(request_id, None)
                    for key, subscribed_id in list(self.by_contract.items()):
                        if subscribed_id == request_id:
                            del self.by_contract[key]
            self.quotes.pop(idea_id, None)
        added = set(desired) - set(self.idea_contracts)
        self.idea_contracts = desired
        if self.connected:
            for idea_id in added | changed:
                self._subscribe_idea(idea_id, desired[idea_id])
            for details in desired.values():
                currency = details.get("currency")
                if currency and currency != "USD":
                    self._subscribe_fx(currency)
        self.subscriptions = len(self.by_contract)

    def refresh(self, positions=None):
        # Streaming requests update continuously; reconnect applies contract changes.
        if positions is not None:
            self.stop()
            self.by_contract = {}
            self.requested_fx = set()
            self.search_requests = set()
            self.client = _IBClient(self)
            self.start(positions, self.stale_con_ids,
                       benchmark_last_day=self.benchmark_last_day)
        return set(self.positions)
