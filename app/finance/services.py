"""Portfolio projections and auditable many-to-many trade matching."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.finance.calculations import (adv20, advx, choose_price, closed_lot, flow_adjusted_daily_pnl,
                              looks_like_option_ticker, open_lot, position_metrics,
                              prepare_history, valid_multiplier, valid_price, window_returns)


class MatchingService:
    def __init__(self, db, portfolio_id=1):
        self.db = db
        self.portfolio_id = portfolio_id

    def _snapshot(self):
        """Load trades and their unmatched quantities once for a batch of work.

        The methods below would otherwise re-read both tables on every iteration.
        """
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        used = defaultdict(float)
        for match in self.db.rows("trade_matches", self.portfolio_id):
            used[match["buy_trade_id"]] += match["matched_qty"]
            used[match["sell_trade_id"]] += match["matched_qty"]
        remaining = {key: max(0.0, float(t["quantity"]) - used[key]) for key, t in trades.items()}
        return trades, remaining

    @staticmethod
    def _by_position(trades):
        index = defaultdict(list)
        for trade in trades.values():
            index[trade["position_id"]].append(trade)
        return index

    def remaining(self):
        return self._snapshot()[1]

    @staticmethod
    def _precedes(buy, sell):
        if buy["trade_date"] != sell["trade_date"]:
            return buy["trade_date"] < sell["trade_date"]
        if buy.get("executed_at") and sell.get("executed_at"):
            return (buy["executed_at"], buy["id"]) < (sell["executed_at"], sell["id"])
        return buy["id"] < sell["id"]

    def match(self, sell_id, allocations, origin="manual", *, snapshot=None):
        trades, remaining = snapshot if snapshot is not None else self._snapshot()
        if sell_id not in trades:
            raise ValueError("SELL is outside this portfolio")
        sell = trades[sell_id]
        if sell["side"] != "SELL":
            raise ValueError("Select a SELL trade")
        if sell["security_type"] == "STK" and looks_like_option_ticker(sell["display_ticker"]):
            raise ValueError("Verify the security type for this option-like ticker")
        allocations = [(int(buy_id), float(qty)) for buy_id, qty in allocations if float(qty) > 0]
        if sum(qty for _, qty in allocations) > remaining[sell_id] + 1e-9:
            raise ValueError("Matched quantity exceeds remaining SELL quantity")
        seen = set()
        for buy_id, qty in allocations:
            if buy_id not in trades:
                raise ValueError("BUY is outside this portfolio")
            buy = trades[buy_id]
            if buy_id in seen or buy["side"] != "BUY" or buy["position_id"] != sell["position_id"]:
                raise ValueError("BUY lots must be unique and have the same instrument")
            if (buy["security_type"] != sell["security_type"] or
                valid_multiplier(buy["multiplier"]) is None or
                buy["multiplier"] != sell["multiplier"]):
                raise ValueError("BUY and SELL must have the same security type and a valid contract multiplier")
            if not self._precedes(buy, sell):
                raise ValueError("BUY must be dated before the SELL")
            if qty > remaining[buy_id] + 1e-9:
                raise ValueError("Matched quantity exceeds remaining BUY quantity")
            seen.add(buy_id)
        with self.db.conn:
            self.db.conn.executemany(
                "INSERT INTO trade_matches(buy_trade_id,sell_trade_id,matched_qty,origin) VALUES (?,?,?,?)",
                [(buy_id, sell_id, qty, origin) for buy_id, qty in allocations])
        if snapshot is not None:
            # Keep the shared snapshot current for the caller's next iteration.
            for buy_id, qty in allocations:
                remaining[buy_id] = max(0.0, remaining[buy_id] - qty)
                remaining[sell_id] = max(0.0, remaining[sell_id] - qty)

    def _eligible_buys(self, by_position, remaining, sell):
        buys = [t for t in by_position.get(sell["position_id"], ()) if t["side"] == "BUY" and
                self._precedes(t, sell) and
                t["security_type"] == sell["security_type"] and
                t["multiplier"] == sell["multiplier"] and remaining[t["id"]] > 1e-9]
        buys.sort(key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        return [(buy, remaining[buy["id"]]) for buy in buys]

    def _auto_chronological(self, sell_ids=None, *, newest_first=False):
        trades, remaining = self._snapshot()
        by_position = self._by_position(trades)
        sells = [t for t in trades.values() if t["side"] == "SELL"]
        if sell_ids is not None:
            sells = [t for t in sells if t["id"] in sell_ids]
        sells.sort(key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        for sell in sells:
            need = remaining[sell["id"]]
            allocations = []
            eligible = self._eligible_buys(by_position, remaining, sell)
            if newest_first:
                eligible.reverse()
            for buy, available in eligible:
                take = min(available, need)
                if take > 0:
                    allocations.append((buy["id"], take))
                    need -= take
                if need <= 1e-9:
                    break
            if allocations:
                self.match(sell["id"], allocations, snapshot=(trades, remaining))

    def auto_fifo(self, sell_ids=None):
        self._auto_chronological(sell_ids)

    def auto_lifo(self, sell_ids=None):
        self._auto_chronological(sell_ids, newest_first=True)

    @staticmethod
    def _open_cost_per_unit(buy):
        """USD purchase cost per unit, including its share of the buy commission."""
        if not buy.get("commission_complete", True):
            return None
        try:
            multiplier = Decimal(str(buy["multiplier"]))
            fx = Decimal(str(buy["fx_to_usd"]))
            qty = Decimal(str(buy["quantity"]))
            if multiplier <= 0 or fx <= 0 or qty <= 0:
                return None
            return (Decimal(str(buy["price"])) * multiplier * fx +
                    Decimal(str(buy["commission_usd"] or 0)) / qty)
        except (ValueError, TypeError, ArithmeticError):
            return None

    @staticmethod
    def _gain_per_unit(buy, sell):
        """Net USD gain for one unit, including pro-rata commissions."""
        if not buy.get("commission_complete", True) or not sell.get("commission_complete", True):
            return None
        try:
            multiplier = Decimal(str(sell["multiplier"]))
            if multiplier <= 0 or Decimal(str(buy["multiplier"])) != multiplier:
                return None
            buy_fx, sell_fx = Decimal(str(buy["fx_to_usd"])), Decimal(str(sell["fx_to_usd"]))
            if buy_fx <= 0 or sell_fx <= 0:
                return None
            buy_qty, sell_qty = Decimal(str(buy["quantity"])), Decimal(str(sell["quantity"]))
            if buy_qty <= 0 or sell_qty <= 0:
                return None
            return (Decimal(str(sell["price"])) * multiplier * sell_fx -
                    Decimal(str(sell["commission_usd"] or 0)) / sell_qty -
                    Decimal(str(buy["price"])) * multiplier * buy_fx -
                    Decimal(str(buy["commission_usd"] or 0)) / buy_qty)
        except (ValueError, TypeError, ArithmeticError):
            return None

    def suggested_max_gain(self, sell_id, replace_existing=False, *, snapshot=None):
        trades, remaining = snapshot if snapshot is not None else self._snapshot()
        if sell_id not in trades or trades[sell_id]["side"] != "SELL":
            raise ValueError("Select a SELL trade in this portfolio")
        sell = trades[sell_id]
        old_by_buy = defaultdict(float)
        if replace_existing:
            for match in self.db.rows("trade_matches", self.portfolio_id):
                if match["sell_trade_id"] == sell_id:
                    old_by_buy[match["buy_trade_id"]] += match["matched_qty"]
        by_position = self._by_position(trades)
        if replace_existing:
            available_buys = [(buy, remaining[buy["id"]] + old_by_buy[buy["id"]])
                              for buy in by_position.get(sell["position_id"], ())
                              if buy["side"] == "BUY" and self._precedes(buy, sell)]
        else:
            available_buys = self._eligible_buys(by_position, remaining, sell)
        candidates = [(buy, available, self._gain_per_unit(buy, sell))
                      for buy, available in available_buys if available > 1e-9]
        candidates = [item for item in candidates if item[2] is not None]
        candidates.sort(key=lambda item: (-item[2], item[0]["trade_date"], item[0]["id"]))
        need = float(sell["quantity"]) if replace_existing else remaining[sell_id]
        result = []
        for buy, available, _ in candidates:
            take = min(available, need)
            if take > 1e-9:
                result.append((buy["id"], take))
                need -= take
            if need <= 1e-9:
                break
        return result

    def auto_max_gain(self, sell_ids=None):
        trades, remaining = self._snapshot()
        sells = [t for t in trades.values() if t["side"] == "SELL"]
        if sell_ids is not None:
            sells = [t for t in sells if t["id"] in sell_ids]
        for sell in sorted(sells, key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"])):
            allocations = self.suggested_max_gain(sell["id"], snapshot=(trades, remaining))
            if allocations:
                self.match(sell["id"], allocations, origin="auto", snapshot=(trades, remaining))

    def auto_max_unrealized_gain(self, sell_ids=None):
        """Sell the highest USD cost lots, leaving the largest open gain at a common mark."""
        trades, remaining = self._snapshot()
        by_position = self._by_position(trades)
        sells = [t for t in trades.values() if t["side"] == "SELL"]
        if sell_ids is not None:
            sells = [t for t in sells if t["id"] in sell_ids]
        sells.sort(key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        for sell in sells:
            need = remaining[sell["id"]]
            candidates = [(buy, available, self._open_cost_per_unit(buy))
                          for buy, available in self._eligible_buys(by_position, remaining, sell)]
            candidates = [item for item in candidates if item[2] is not None]
            candidates.sort(key=lambda item: (-item[2], item[0]["trade_date"], item[0]["id"]))
            allocations = []
            for buy, available, _ in candidates:
                take = min(available, need)
                if take > 1e-9:
                    allocations.append((buy["id"], take))
                    need -= take
                if need <= 1e-9:
                    break
            if allocations:
                self.match(sell["id"], allocations, snapshot=(trades, remaining))

    def rebuild_auto(self, position_ids=None):
        """Recalculate automatic matches; keep manually adjusted allocations fixed."""
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        selected = set(position_ids) if position_ids is not None else {t["position_id"] for t in trades.values()}
        matches = self.db.rows("trade_matches", self.portfolio_id)
        auto_ids = [m["match_id"] for m in matches if m["origin"] in {"auto", "auto_short"}
                    and trades[m["sell_trade_id"]]["position_id"] in selected]
        removed = set(auto_ids)
        remaining = {key: float(trade["quantity"]) for key, trade in trades.items()}
        for match in matches:
            if match["match_id"] not in removed:
                remaining[match["buy_trade_id"]] -= match["matched_qty"]
                remaining[match["sell_trade_id"]] -= match["matched_qty"]
        ordered = sorted((t for t in trades.values() if t["position_id"] in selected and
                          t["security_type"] != "CASH"),
                         key=lambda t: (t["trade_date"], t.get("executed_at") or "", t["id"]))
        # Index by instrument so each sale only considers its own instrument's
        # buys; scanning every selected trade per sale is quadratic in the book.
        buys_by_position = defaultdict(list)
        sells_by_position = defaultdict(list)
        for trade in ordered:
            (buys_by_position if trade["side"] == "BUY" else sells_by_position)[trade["position_id"]].append(trade)
        allocations = []
        for sell in (t for t in ordered if t["side"] == "SELL"):
            candidates = [(buy, self._gain_per_unit(buy, sell))
                          for buy in buys_by_position[sell["position_id"]]
                          if self._precedes(buy, sell) and remaining[buy["id"]] > 1e-9
                          and buy["security_type"] == sell["security_type"]]
            for buy, _ in sorted((pair for pair in candidates if pair[1] is not None),
                                 key=lambda pair: (-pair[1], pair[0]["trade_date"], pair[0]["id"])):
                qty = min(remaining[buy["id"]], remaining[sell["id"]])
                if qty > 1e-9:
                    allocations.append((buy["id"], sell["id"], qty, "auto"))
                    remaining[buy["id"]] -= qty
                    remaining[sell["id"]] -= qty
                if remaining[sell["id"]] <= 1e-9:
                    break
        for buy in (t for t in ordered if t["side"] == "BUY"):
            candidates = [(sell, self._gain_per_unit(buy, sell))
                          for sell in sells_by_position[buy["position_id"]]
                          if self._precedes(sell, buy) and remaining[sell["id"]] > 1e-9
                          and sell["security_type"] == buy["security_type"]]
            for sell, _ in sorted((pair for pair in candidates if pair[1] is not None),
                                  key=lambda pair: (-pair[1], pair[0]["trade_date"], pair[0]["id"])):
                qty = min(remaining[buy["id"]], remaining[sell["id"]])
                if qty > 1e-9:
                    allocations.append((buy["id"], sell["id"], qty, "auto_short"))
                    remaining[buy["id"]] -= qty
                    remaining[sell["id"]] -= qty
                if remaining[buy["id"]] <= 1e-9:
                    break
        with self.db.conn:
            self.db.conn.executemany("DELETE FROM trade_matches WHERE match_id=?", [(key,) for key in auto_ids])
            self.db.conn.executemany("""INSERT INTO trade_matches
                (buy_trade_id,sell_trade_id,matched_qty,origin) VALUES (?,?,?,?)""", allocations)

    def replace_sell_matches(self, sell_id, allocations):
        """Replace one sale's allocations while preserving its raw trade."""
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        if sell_id not in trades or trades[sell_id]["side"] != "SELL":
            raise ValueError("Select a SELL trade in this portfolio")
        sell = trades[sell_id]
        allocations = [(int(buy_id), float(qty)) for buy_id, qty in allocations if float(qty) > 0]
        old = [m for m in self.db.rows("trade_matches", self.portfolio_id) if m["sell_trade_id"] == sell_id]
        remaining = self.remaining()
        old_by_buy = defaultdict(float)
        for match in old:
            old_by_buy[match["buy_trade_id"]] += match["matched_qty"]
        if sum(qty for _, qty in allocations) > float(sell["quantity"]) + 1e-9:
            raise ValueError("Matched quantity exceeds SELL quantity")
        if len({buy_id for buy_id, _ in allocations}) != len(allocations):
            raise ValueError("A BUY lot can appear only once")
        for buy_id, qty in allocations:
            buy = trades.get(buy_id)
            if not buy or buy["side"] != "BUY" or buy["position_id"] != sell["position_id"]:
                raise ValueError("BUY must belong to the same instrument")
            if not self._precedes(buy, sell):
                raise ValueError("BUY must precede SELL")
            if (buy["security_type"] != sell["security_type"] or
                self._gain_per_unit(buy, sell) is None):
                raise ValueError("Matching requires valid instrument, multiplier and FX")
            if qty > remaining[buy_id] + old_by_buy[buy_id] + 1e-9:
                raise ValueError("Matched quantity exceeds remaining BUY quantity")
        with self.db.conn:
            self.db.conn.execute("DELETE FROM trade_matches WHERE sell_trade_id=?", (sell_id,))
            self.db.conn.executemany("""INSERT INTO trade_matches(buy_trade_id,sell_trade_id,matched_qty,origin)
                VALUES (?,?,?,'manual')""", [(buy_id, sell_id, qty) for buy_id, qty in allocations])

    def clear(self, match_id):
        if not any(m["match_id"] == match_id for m in self.db.rows("trade_matches", self.portfolio_id)):
            raise ValueError("Match is outside this portfolio")
        with self.db.conn:
            self.db.conn.execute("DELETE FROM trade_matches WHERE match_id=?", (match_id,))

    def trade_rows(self):
        remaining = self.remaining()
        result = []
        for trade in self.db.rows("trades", self.portfolio_id):
            row = dict(trade)
            if not trade["commission_complete"]:
                row["commission_usd"] = None
            multiplier = valid_multiplier(trade["multiplier"])
            row["gross_lc"] = trade["quantity"] * trade["price"] * multiplier if multiplier else None
            fx = valid_price(trade["fx_to_usd"])
            row["gross_usd"] = row["gross_lc"] * fx if row["gross_lc"] is not None and fx else None
            row["valuation_status"] = ("VERIFY SECURITY TYPE" if trade["security_type"] == "STK" and
                                       looks_like_option_ticker(trade["display_ticker"]) else
                                       "MULTIPLIER REQUIRED" if multiplier is None else
                                       "FX REQUIRED" if fx is None else "OK")
            if row["valuation_status"] == "VERIFY SECURITY TYPE":
                row["gross_lc"] = row["gross_usd"] = None
            row["matched_qty"] = trade["quantity"] - remaining[trade["id"]]
            row["remaining_qty"] = remaining[trade["id"]]
            if trade["security_type"] == "CASH":
                pair = str(trade["display_ticker"] or "").upper().split(".")
                if len(pair) == 2 and all(len(currency) == 3 for currency in pair):
                    # FX quantity is in the pair's base currency; price quotes the
                    # second currency per base unit. Show the actual USD leg where
                    # one exists, rather than retranslating it at IBKR's reference FX.
                    if pair[0] == "USD":
                        row["gross_usd"] = trade["quantity"]
                    elif pair[1] == "USD":
                        row["gross_usd"] = row["gross_lc"]
                row["match_status"] = "FX cash activity"
                row["matched_qty"] = row["remaining_qty"] = None
                result.append(row)
                continue
            if trade["side"] == "BUY":
                row["match_status"] = ("Open" if row["matched_qty"] <= 1e-9 else
                                       "Closed" if row["remaining_qty"] <= 1e-9 else "Partially sold")
            else:
                row["match_status"] = ("Needs matching" if row["matched_qty"] <= 1e-9 else
                                       "Matched" if row["remaining_qty"] <= 1e-9 else "Partially matched")
            result.append(row)
        return result


class PortfolioService:
    def __init__(self, db, market, portfolio_id=1):
        self.db, self.market = db, market
        self.portfolio_id = portfolio_id
        self.matching = MatchingService(db, portfolio_id)

    def trade_rows(self, lots=None):
        """Show closed-lot P&L on both matched trades and open P&L on open buys.

        Realized values are intentionally repeated across the two executions
        for trade-level inspection; they must not be summed across trade rows.
        """
        lots = self.lots_rows() if lots is None else lots
        aggregates = defaultdict(lambda: {
            "realized_net": 0.0, "realized_basis": 0.0, "realized_missing": False,
            "net_unrealized": 0.0, "open_basis": 0.0, "open_missing": False,
            "has_realized": False, "has_open": False,
        })
        for lot in lots:
            closed = str(lot.get("lot_state", "")).startswith("Closed")
            trade_ids = ({lot.get("buy_trade_id"), lot.get("sell_trade_id")} if closed
                         else {lot.get("buy_trade_id")})
            for trade_id in trade_ids:
                if trade_id is None:
                    continue
                aggregate = aggregates[trade_id]
                if closed:
                    aggregate["has_realized"] = True
                    if (lot.get("realized_net") is None or lot.get("buy_value_usd") is None or
                        lot.get("buy_commission") is None):
                        aggregate["realized_missing"] = True
                    else:
                        aggregate["realized_net"] += lot["realized_net"]
                        aggregate["realized_basis"] += lot["buy_value_usd"] + lot["buy_commission"]
                else:
                    aggregate["has_open"] = True
                    if (lot.get("net_unrealized") is None or lot.get("buy_value_usd") is None or
                        lot.get("buy_commission") is None):
                        aggregate["open_missing"] = True
                    else:
                        aggregate["net_unrealized"] += lot["net_unrealized"]
                        aggregate["open_basis"] += lot["buy_value_usd"] + lot["buy_commission"]

        rows = self.matching.trade_rows()
        for row in rows:
            aggregate = aggregates.get(row["id"])
            row.update({"realized_net": None, "realized_pct": None,
                        "net_unrealized": None, "unrealized_pct": None})
            if aggregate is None:
                continue
            if aggregate["has_realized"] and not aggregate["realized_missing"]:
                row["realized_net"] = aggregate["realized_net"]
                if aggregate["realized_basis"]:
                    row["realized_pct"] = aggregate["realized_net"] / aggregate["realized_basis"]
            if aggregate["has_open"] and not aggregate["open_missing"]:
                row["net_unrealized"] = aggregate["net_unrealized"]
                if aggregate["open_basis"]:
                    row["unrealized_pct"] = aggregate["net_unrealized"] / aggregate["open_basis"]
        return rows

    def portfolio_rows(self):
        trades = self.db.rows("trades", self.portfolio_id)
        openings = self.db.legacy_opening_quantities(self.portfolio_id)
        remaining = self.matching.remaining()
        ledger = defaultdict(float)
        by_position = defaultdict(list)
        for trade in trades:
            position_id = trade["position_id"]
            opening = openings.get(position_id)
            if not opening or trade["trade_date"] > opening["as_of"]:
                ledger[position_id] += trade["quantity"] * (1 if trade["side"] == "BUY" else -1)
            by_position[position_id].append(trade)
        broker = {p["con_id"]: p for p in self.db.broker_positions(self.portfolio_id)}
        broker_complete = bool(self.db.metadata(f"broker_positions_refresh_{self.portfolio_id}"))
        nav = self.db.effective_nav(self.portfolio_id)
        rows = []
        prepared_history = {}
        for position in self.db.rows("positions", self.portfolio_id):
            if position["security_type"] == "CASH":
                continue  # FX pair executions do not represent security holdings.
            row = dict(position)
            opening = openings.get(position["id"])
            opening_qty = float(opening["quantity"]) if opening else 0.0
            position_trades = by_position[position["id"]]
            open_buys = [(t, remaining[t["id"]]) for t in position_trades
                         if t["side"] == "BUY" and remaining[t["id"]] > 1e-9]
            open_qty = sum(qty for _, qty in open_buys)
            row["recorded_qty"] = position["position_qty"]
            raw_qty = opening_qty + ledger[position["id"]]
            row["position_qty"] = 0.0 if abs(raw_qty) < 1e-9 else raw_qty
            multiplier = valid_multiplier(position["multiplier"])
            complete_open_lots = (not opening and open_qty > 1e-9 and row["position_qty"] > 0 and
                                  abs(open_qty - row["position_qty"]) < 1e-9 and
                                  multiplier is not None and all(
                                      valid_price(t["fx_to_usd"]) is not None and
                                      t["multiplier"] == multiplier and t["commission_complete"]
                                      for t, _ in open_buys))
            row["average_cost"] = (
                sum(qty * (t["price"] * multiplier +
                           t["commission_usd"] / t["quantity"] / t["fx_to_usd"])
                    for t, qty in open_buys) / (open_qty * multiplier)
                if complete_open_lots else None)
            current_fx = self.db.fx_rate(position["currency"], date.today().isoformat())
            row["fx_to_usd"] = current_fx
            quote = self.market.quote(position["id"])
            price, source = choose_price(quote.get("ticks", {}), quote.get("mode", "Live"))
            if price is None and valid_price(quote.get("cached_price")) is not None:
                price = float(quote["cached_price"])
                source = f"CACHED QUOTE {quote['cached_as_of']}"
            suspect_option = (position["security_type"] == "STK" and
                              looks_like_option_ticker(position["display_ticker"]))
            if suspect_option:
                price, source = None, ""
            history = ((quote.get("history") or []) if quote.get("mode") == "Mock" else
                       self.db.history(position["con_id"]) if position["con_id"] else [])
            as_of = self._as_of(position)
            prior_bar = next((bar for bar in reversed(history)
                              if bar["day"] < as_of.isoformat()), None)
            prior_close = prior_bar["close"] if prior_bar else quote.get("prior_close")
            cached_close = False
            if (price is None or source == "CLOSE") and not suspect_option and history:
                latest = next((bar for bar in reversed(history)
                               if bar["day"] <= as_of.isoformat() and
                               valid_price(bar["close"]) is not None), None)
                if latest and (as_of - date.fromisoformat(latest["day"])).days <= 7:
                    price = float(latest["close"])
                    source = (f"SAVED SESSION BAR {latest['day']}" if latest["day"] == as_of.isoformat()
                              else f"CACHED CLOSE {latest['day']}")
                    cached_close = True
            row.update(position_metrics(row["position_qty"], row["average_cost"],
                                        current_fx, price, prior_close, nav,
                                        position["multiplier"]))
            if source == "CLOSE" or source.startswith("CACHED QUOTE"):
                row["daily_move"] = row["one_day"] = None
            # A price-only shortcut cannot value partial, missing, or short lots.
            # The broker-compatible open P&L below uses each surviving purchase's
            # acquisition FX and allocated fee.
            row["unrealized_pnl"] = row["unrealized_pct"] = None
            prior_day = prior_bar["day"] if prior_bar else (as_of - timedelta(days=1)).isoformat()
            today_trades = [t for t in position_trades if t["trade_date"] == as_of.isoformat()]
            row["traded_today"] = bool(today_trades)
            row["daily_pnl"] = flow_adjusted_daily_pnl(
                row["position_qty"], price, current_fx, prior_close,
                self.db.fx_rate(position["currency"], prior_day), position["multiplier"],
                today_trades)
            if abs(row["position_qty"]) > 1e-9 and (
                (cached_close and source != f"SAVED SESSION BAR {as_of.isoformat()}") or
                source == "CLOSE" or source.startswith("CACHED QUOTE")):
                row["daily_pnl"] = None
            if complete_open_lots:
                lots = [open_lot(buy, qty, price, current_fx) for buy, qty in open_buys]
                row["unrealized_pnl"] = (sum(lot["net_unrealized"] for lot in lots)
                                         if all(lot["net_unrealized"] is not None for lot in lots) else None)
                basis = sum(lot["buy_value_usd"] + lot["buy_commission"] for lot in lots
                            if lot["buy_value_usd"] is not None and lot["buy_commission"] is not None)
                row["unrealized_pct"] = (row["unrealized_pnl"] / basis
                                         if row["unrealized_pnl"] is not None and basis > 0 and
                                         all(lot["buy_value_usd"] is not None for lot in lots) else None)
            bar_key = (position["con_id"], as_of)
            prepared = prepared_history.get(bar_key) if position["con_id"] else None
            if prepared is None:
                prepared = prepare_history(history, as_of)
                if position["con_id"]:
                    prepared_history[bar_key] = prepared
            row.update(window_returns(None if source.startswith("CACHED QUOTE") else price,
                                      prepared[0], prepared[1]))
            row["last_px"] = price
            row["price_source"] = "MOCK" if quote.get("mode") == "Mock" and price else source
            row["last_updated"] = quote.get("cached_as_of", "") if source.startswith("CACHED QUOTE") else quote.get("updated", "")
            cached_status = "CACHED QUOTE"
            broker_status = str(quote.get("status") or "")
            if broker_status and broker_status not in ("CACHED QUOTE", "OK", "DELAYED"):
                cached_status += f" · {broker_status}"
            row["data_status"] = ("SAVED SESSION BAR" if source.startswith("SAVED SESSION BAR") else
                                  "CACHED CLOSE" if cached_close else
                                  cached_status if source.startswith("CACHED QUOTE") else
                                  quote.get("status", "DISCONNECTED"))
            if valid_multiplier(position["multiplier"]) is None:
                row["data_status"] = "MULTIPLIER REQUIRED"
            elif valid_price(current_fx) is None:
                row["data_status"] = "FX REQUIRED"
            elif suspect_option:
                row["data_status"] = "VERIFY SECURITY TYPE"
            elif not position_trades:
                row["data_status"] = "STATEMENT OPENING" if opening else "NO TRADES"
            elif any(t["side"] == "SELL" and remaining[t["id"]] > 1e-9 for t in position_trades):
                row["data_status"] = "UNMATCHED SALE"
            elif any(not t["commission_complete"] for t, qty in open_buys if qty > 1e-9):
                row["data_status"] = "FEE PENDING"
            row["adv20"] = adv20(history)
            row["advx"] = advx(row["position_qty"], row["adv20"])
            row["ledger_qty"] = row["position_qty"]
            broker_row = broker.get(position["con_id"])
            broker_qty = (broker_row["quantity"] if broker_row else
                          0 if broker_complete and position["con_id"] else None)
            row["broker_qty"] = broker_qty
            row["qty_difference"] = (row["position_qty"] - broker_qty if broker_qty is not None else None)
            row["reconciliation"] = ("NOT CHECKED" if broker_qty is None else
                                     "OK" if abs(row["qty_difference"]) < 1e-9 else "DIFFERENCE")
            if row["reconciliation"] == "DIFFERENCE":
                if position_trades:
                    row["data_status"] = "POSITION DIFFERENCE"
                for key in ("value", "portfolio_pct", "daily_pnl", "unrealized_pnl", "unrealized_pct"):
                    row[key] = None
            if position["security_type"] == "FUT":
                # A futures quote times its multiplier is not an asset value;
                # its cash effect is the change since settlement.
                for key in ("value", "portfolio_pct", "daily_pnl", "unrealized_pnl", "unrealized_pct"):
                    row[key] = None
                if row["reconciliation"] != "DIFFERENCE":
                    row["data_status"] = "FUTURES VALUATION UNAVAILABLE"
            rows.append(row)
        return rows

    def lots_rows(self, portfolio_rows=None):
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        positions = {p["id"]: p for p in self.db.rows("positions", self.portfolio_id)}
        valuations = {p["id"]: p for p in (portfolio_rows if portfolio_rows is not None else self.portfolio_rows())}
        reconciliation = {key: p["reconciliation"] for key, p in valuations.items()}
        rows = []
        for match in self.db.rows("trade_matches", self.portfolio_id):
            buy, sell = trades[match["buy_trade_id"]], trades[match["sell_trade_id"]]
            if buy["security_type"] == "CASH":
                continue
            qty = match["matched_qty"]
            row = self._base_lot(buy, qty)
            short = match["origin"] == "auto_short"
            row["lot_state"] = "Closed short" if short else "Closed"
            row.update({"match_id": match["match_id"], "sell_date": sell["trade_date"],
                        "sell_trade_id": sell["id"],
                        "sell_qty": qty, "sell_price": sell["price"], "sell_currency": sell["currency"],
                        "sell_fx": sell["fx_to_usd"], "sell_notes": sell["notes"],
                        "sell_value_lc": qty * sell["price"] * sell["multiplier"] if valid_multiplier(sell["multiplier"]) else None,
                        "duration": abs((date.fromisoformat(sell["trade_date"]) - date.fromisoformat(buy["trade_date"])).days)
                        if buy["trade_date"] and sell["trade_date"] else None})
            row.update(closed_lot(buy, sell, qty))
            if not buy["commission_complete"] or not sell["commission_complete"]:
                row["realized_net"] = row["realized_pct"] = None
                if not buy["commission_complete"]:
                    row["buy_commission"] = None
                if not sell["commission_complete"]:
                    row["sell_commission"] = None
                row["lot_note"] = "Fee pending"
            else:
                row["lot_note"] = ("Short sale covered by later buy" if short else "") if row.get("realized_net") is not None else "FX or multiplier required"
            rows.append(row)
        remaining = self.matching.remaining()
        position_as_of = {}
        prepared_history = {}
        for buy in trades.values():
            if buy["security_type"] == "CASH":
                continue
            qty = remaining[buy["id"]]
            if buy["side"] != "BUY" or qty <= 1e-9:
                continue
            row = self._base_lot(buy, qty)
            row["lot_state"] = "Open"
            position = positions.get(buy["position_id"])
            quote = self.market.quote(position["id"]) if position else {}
            price = valuations[position["id"]]["last_px"] if position else None
            if position and (position["security_type"] != buy["security_type"] or
                             position["multiplier"] != buy["multiplier"]):
                price = None
            if buy["security_type"] == "STK" and looks_like_option_ticker(buy["display_ticker"]):
                price = None
            row["last_price"] = price
            current_fx = valuations[position["id"]]["fx_to_usd"] if position else buy["fx_to_usd"]
            row.update(open_lot(buy, qty, price, current_fx))
            if buy["security_type"] == "FUT":
                row["current_value"] = row["net_unrealized"] = row["unrealized_pct"] = None
                row["lot_note"] = "Futures valuation unavailable"
            if not buy["commission_complete"]:
                row["net_unrealized"] = row["unrealized_pct"] = None
                row["buy_commission"] = None
                row["lot_note"] = "Fee pending"
            if reconciliation.get(buy["position_id"]) == "DIFFERENCE":
                row["net_unrealized"] = None
                row["lot_note"] = "IBKR qty differs"
            elif buy["commission_complete"] and buy["security_type"] != "FUT":
                price_source = valuations[position["id"]]["price_source"] if position else ""
                row["lot_note"] = (price_source if row.get("net_unrealized") is not None and
                                   price_source.startswith(("SAVED SESSION BAR", "CACHED CLOSE", "CACHED QUOTE")) else
                                   "" if row.get("net_unrealized") is not None else
                                   "Price unavailable" if price is None else
                                   "FX required" if current_fx is None else "Valuation data required")
            if position:
                as_of = position_as_of.get(position["id"])
                if as_of is None:
                    as_of = self._as_of(position)
                    position_as_of[position["id"]] = as_of
                bar_key = (position["con_id"], as_of)
                prepared = prepared_history.get(bar_key) if position["con_id"] else None
                if prepared is None:
                    history = ((quote.get("history") or []) if quote.get("mode") == "Mock" else
                               self.db.history(position["con_id"]) if position.get("con_id") else [])
                    prepared = prepare_history(history, as_of)
                    if position.get("con_id"):
                        prepared_history[bar_key] = prepared
                returns = window_returns(price, prepared[0], prepared[1])
                row["mtd"], row["ytd"] = returns["mtd"], returns["ytd"]
            rows.append(row)
        return rows

    @staticmethod
    def _base_lot(buy, qty):
        multiplier = valid_multiplier(buy["multiplier"])
        return {"buy_date": buy["trade_date"], "ticker": buy["display_ticker"],
                "buy_trade_id": buy["id"],
                "position_id": buy["position_id"],
                "buy_qty": qty, "buy_price": buy["price"], "buy_currency": buy["currency"],
                "buy_fx": buy["fx_to_usd"], "buy_notes": buy["notes"],
                "buy_value_lc": qty * buy["price"] * multiplier if multiplier else None,
                "buy_value_usd": qty * buy["price"] * multiplier * buy["fx_to_usd"]
                if multiplier and valid_price(buy["fx_to_usd"]) else None}

    def _as_of(self, position):
        if not position.get("con_id"):
            return date.today()
        row = self.db.conn.execute("SELECT time_zone FROM contracts WHERE position_id=?", (position["id"],)).fetchone()
        if row and row[0]:
            try:
                return datetime.now(ZoneInfo(row[0])).date()
            except Exception:
                pass
        exchange = str(position.get("primary_exchange") or position.get("exchange") or "").upper()
        if position.get("currency") == "USD" and exchange in {
            "NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "CBOE", "SMART"
        }:
            return datetime.now(ZoneInfo("America/New_York")).date()
        return date.today()
