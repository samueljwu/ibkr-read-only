"""Portfolio projections and auditable many-to-many trade matching."""

from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.finance.calculations import (adv20, advx, choose_price, closed_lot, effective_commission_usd, flow_adjusted_daily_pnl,
                              looks_like_option_ticker, open_lot, position_metrics,
                              prepare_history, valid_multiplier, valid_price, window_returns)


class MatchingService:
    def __init__(self, db, portfolio_id=1):
        self.db = db
        self.portfolio_id = portfolio_id

    def _snapshot(self, trades=None, matches=None):
        """Load trades and their unmatched quantities once for a batch of work.

        The methods below would otherwise re-read both tables on every iteration.
        """
        trades = {t["id"]: t for t in (trades if trades is not None else
                                      self.db.rows("trades", self.portfolio_id))}
        used = defaultdict(float)
        for match in (matches if matches is not None else
                      self.db.rows("trade_matches", self.portfolio_id)):
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

    def remaining(self, *, trades=None, matches=None):
        return self._snapshot(trades, matches)[1]

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
        try:
            multiplier = Decimal(str(buy["multiplier"]))
            fx = Decimal(str(buy["fx_to_usd"]))
            qty = Decimal(str(buy["quantity"]))
            if multiplier <= 0 or fx <= 0 or qty <= 0:
                return None
            return (Decimal(str(buy["price"])) * multiplier * fx +
                    Decimal(str(effective_commission_usd(buy))) / qty)
        except (ValueError, TypeError, ArithmeticError):
            return None

    @staticmethod
    def _gain_per_unit(buy, sell):
        """Net USD gain for one unit, including pro-rata commissions."""
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
                    Decimal(str(effective_commission_usd(sell))) / sell_qty -
                    Decimal(str(buy["price"])) * multiplier * buy_fx -
                    Decimal(str(effective_commission_usd(buy))) / buy_qty)
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

    def _auto_allocations(self, trades, fixed_matches, selected_positions):
        """Pure planner: compute automatic allocations for the selected instruments.

        `trades` is {id: trade row} for (at least) every trade `fixed_matches` or the
        selected positions reference. `fixed_matches` are matches to treat as already
        consuming quantity (manual allocations, or auto matches outside the selection)
        and are not re-emitted here. Returns [(buy_id, sell_id, qty, origin)] without
        touching the database.
        """
        remaining = {key: float(trade["quantity"]) for key, trade in trades.items()}
        for match in fixed_matches:
            remaining[match["buy_trade_id"]] -= match["matched_qty"]
            remaining[match["sell_trade_id"]] -= match["matched_qty"]
        ordered = sorted((t for t in trades.values() if t["position_id"] in selected_positions and
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
        return allocations

    def rebuild_auto(self, position_ids=None):
        """Recalculate automatic matches; keep manually adjusted allocations fixed."""
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        selected = set(position_ids) if position_ids is not None else {t["position_id"] for t in trades.values()}
        matches = self.db.rows("trade_matches", self.portfolio_id)
        auto_ids = [m["match_id"] for m in matches if m["origin"] in {"auto", "auto_short"}
                    and trades[m["sell_trade_id"]]["position_id"] in selected]
        removed = set(auto_ids)
        fixed_matches = [m for m in matches if m["match_id"] not in removed]
        allocations = self._auto_allocations(trades, fixed_matches, selected)
        with self.db.conn:
            self.db.conn.executemany("DELETE FROM trade_matches WHERE match_id=?", [(key,) for key in auto_ids])
            self.db.conn.executemany("""INSERT INTO trade_matches
                (buy_trade_id,sell_trade_id,matched_qty,origin) VALUES (?,?,?,?)""", allocations)

    def validate_manual(self, position_id, manual_matches):
        """Validate proposed manual (buy_id, sell_id, qty) allocations for one instrument.

        Raises ValueError describing the first violation found; does not write to the DB.
        Returns the normalized [(buy_id, sell_id, qty)] list (positive quantities only).
        """
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)}
        normalized = [(int(buy_id), int(sell_id), float(qty))
                      for buy_id, sell_id, qty in manual_matches if float(qty) > 0]
        buy_used, sell_used = defaultdict(float), defaultdict(float)
        for buy_id, sell_id, qty in normalized:
            buy, sell = trades.get(buy_id), trades.get(sell_id)
            if not buy or not sell or buy["side"] != "BUY" or sell["side"] != "SELL":
                raise ValueError("Matches must pair a BUY with a SELL")
            if buy["position_id"] != position_id or sell["position_id"] != position_id:
                raise ValueError("BUY and SELL must belong to the selected instrument")
            if not self._precedes(buy, sell):
                raise ValueError("BUY must be dated before the SELL")
            if (buy["security_type"] != sell["security_type"] or
                self._gain_per_unit(buy, sell) is None):
                raise ValueError("Matching requires valid instrument, multiplier and FX")
            buy_used[buy_id] += qty
            sell_used[sell_id] += qty
            if buy_used[buy_id] > float(buy["quantity"]) + 1e-9:
                raise ValueError("Matched quantity exceeds BUY quantity")
            if sell_used[sell_id] > float(sell["quantity"]) + 1e-9:
                raise ValueError("Matched quantity exceeds SELL quantity")
        return normalized

    def position_state(self, position_id):
        """Trades and current matches for one instrument, for the manual-matching UI."""
        trades = {t["id"]: t for t in self.db.rows("trades", self.portfolio_id)
                  if t["position_id"] == position_id and t["security_type"] != "CASH"}
        matches = [m for m in self.db.rows("trade_matches", self.portfolio_id)
                   if m["buy_trade_id"] in trades or m["sell_trade_id"] in trades]
        return trades, matches

    def plan_position(self, position_id, manual_matches):
        """Preview the full match set for one instrument: validated manual allocations
        plus the automatic match of whatever remains. Does not write to the DB.
        """
        normalized = self.validate_manual(position_id, manual_matches)
        trades, _ = self.position_state(position_id)
        fixed = [{"buy_trade_id": b, "sell_trade_id": s, "matched_qty": q} for b, s, q in normalized]
        auto = self._auto_allocations(trades, fixed, {position_id})
        manual_rows = [(b, s, q, "manual") for b, s, q in normalized]
        return manual_rows + auto

    def apply_position_matches(self, position_id, manual_matches, *, settings=None):
        """Replace one instrument's matches with manual allocations plus a recalculated
        automatic remainder. Optional view settings share the same transaction.
        Validates first; on failure, no rows change (atomic).
        """
        planned = self.plan_position(position_id, manual_matches)
        _, matches = self.position_state(position_id)
        old_ids = [m["match_id"] for m in matches]
        with self.db.conn:
            self.db.conn.executemany("DELETE FROM trade_matches WHERE match_id=?", [(i,) for i in old_ids])
            self.db.conn.executemany("""INSERT INTO trade_matches
                (buy_trade_id,sell_trade_id,matched_qty,origin) VALUES (?,?,?,?)""", planned)
            if settings:
                self.db.conn.executemany(
                    """INSERT INTO settings(key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    [(key, str(value)) for key, value in settings.items()])

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

    def trade_rows(self, *, trades=None, remaining=None):
        trades = trades if trades is not None else self.db.rows("trades", self.portfolio_id)
        remaining = remaining if remaining is not None else self.remaining(trades=trades)
        openings = self.db.legacy_opening_quantities(self.portfolio_id)
        position_qty = defaultdict(float)
        anchored = set()
        traded_pct = {}
        for trade in sorted(trades, key=lambda item: (
                item["trade_date"], item.get("executed_at") or "", item["id"])):
            if trade["security_type"] == "CASH":
                traded_pct[trade["id"]] = None
                continue
            position_id = trade["position_id"]
            opening = openings.get(position_id)
            if opening and trade["trade_date"] > opening["as_of"] and position_id not in anchored:
                position_qty[position_id] = float(opening["quantity"])
                anchored.add(position_id)
            prior = position_qty[position_id]
            traded_pct[trade["id"]] = (float(trade["quantity"]) / abs(prior)
                                        if abs(prior) > 1e-9 else None)
            position_qty[position_id] = prior + float(trade["quantity"]) * (
                1 if trade["side"] == "BUY" else -1)
        result = []
        for trade in trades:
            row = dict(trade)
            row["traded_pct"] = traded_pct[trade["id"]]
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
        self._shared_trades = None
        self._shared_matches = None
        self._shared_positions = None
        self._shared_remaining = None
        self._shared_histories = None

    @contextmanager
    def shared_rows(self):
        """Reuse one ledger read while building related views in a refresh."""
        if self._shared_trades is not None:
            yield
            return
        trades = self.db.rows("trades", self.portfolio_id)
        matches = self.db.rows("trade_matches", self.portfolio_id)
        positions = self.db.rows("positions", self.portfolio_id)
        self._shared_trades = trades
        self._shared_matches = matches
        self._shared_positions = positions
        self._shared_histories = {}
        try:
            yield
        finally:
            self._shared_trades = None
            self._shared_matches = None
            self._shared_positions = None
            self._shared_remaining = None
            self._shared_histories = None

    def _trades(self, position_ids=None):
        return (self._shared_trades if self._shared_trades is not None else
                self.db.rows("trades", self.portfolio_id, position_ids=position_ids))

    def _matches(self, position_ids=None):
        return (self._shared_matches if self._shared_matches is not None else
                self.db.rows("trade_matches", self.portfolio_id, position_ids=position_ids))

    def _positions(self, position_ids=None):
        return (self._shared_positions if self._shared_positions is not None else
                self.db.rows("positions", self.portfolio_id, position_ids=position_ids))

    def _remaining(self, trades, position_ids=None):
        if self._shared_remaining is not None:
            return self._shared_remaining
        remaining = self.matching.remaining(trades=trades, matches=self._matches(position_ids))
        if self._shared_trades is not None:
            self._shared_remaining = remaining
        return remaining

    def _history(self, position, quote, histories):
        if quote.get("mode") == "Mock":
            return quote.get("history") or []
        con_id = position.get("con_id")
        if not con_id:
            return []
        cache = self._shared_histories if self._shared_histories is not None else histories
        if con_id not in cache:
            cache[con_id] = self.db.history(con_id)
        return cache[con_id]

    def trade_rows(self, lots=None):
        """Show closed-lot P&L on both matched trades and open P&L on open buys.

        Realized values are intentionally repeated across the two executions
        for trade-level inspection; they must not be summed across trade rows.
        """
        lots = self.lots_rows() if lots is None else lots
        aggregates = defaultdict(lambda: {
            "realized_net": 0.0, "realized_basis": 0.0, "realized_missing": False,
            "net_unrealized": 0.0, "open_basis": 0.0, "open_missing": False,
            "has_realized": False, "has_open": False, "fee_pending": False,
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
                    if lot.get("realized_net") is None or lot.get("buy_value_usd") is None:
                        aggregate["realized_missing"] = True
                    else:
                        aggregate["realized_net"] += lot["realized_net"]
                        aggregate["realized_basis"] += lot["buy_value_usd"] + (lot["buy_commission"] or 0)
                else:
                    aggregate["has_open"] = True
                    if lot.get("net_unrealized") is None or lot.get("buy_value_usd") is None:
                        aggregate["open_missing"] = True
                    else:
                        aggregate["net_unrealized"] += lot["net_unrealized"]
                        aggregate["open_basis"] += lot["buy_value_usd"] + (lot["buy_commission"] or 0)
                aggregate["fee_pending"] = aggregate.get("fee_pending", False) or lot.get("fee_pending", False)

        trades = self._trades()
        rows = self.matching.trade_rows(trades=trades, remaining=self._remaining(trades))
        for row in rows:
            aggregate = aggregates.get(row["id"])
            row.update({"realized_net": None, "realized_pct": None,
                        "net_unrealized": None, "unrealized_pct": None})
            if aggregate is None:
                continue
            row["fee_pending"] = aggregate["fee_pending"] or not row["commission_complete"]
            if aggregate["has_realized"] and not aggregate["realized_missing"]:
                row["realized_net"] = aggregate["realized_net"]
                if aggregate["realized_basis"]:
                    row["realized_pct"] = aggregate["realized_net"] / aggregate["realized_basis"]
            if aggregate["has_open"] and not aggregate["open_missing"]:
                row["net_unrealized"] = aggregate["net_unrealized"]
                if aggregate["open_basis"]:
                    row["unrealized_pct"] = aggregate["net_unrealized"] / aggregate["open_basis"]
        return rows

    def portfolio_rows(self, position_ids=None):
        trades = self._trades(position_ids)
        openings = self.db.legacy_opening_quantities(self.portfolio_id)
        remaining = self._remaining(trades, position_ids)
        ledger = defaultdict(float)
        by_position = defaultdict(list)
        for trade in trades:
            position_id = trade["position_id"]
            opening = openings.get(position_id)
            if trade["source"] == "CSV market baseline" and opening:
                # The CSV quantity anchor already includes this estimated lot.
                pass
            elif not opening or trade["trade_date"] > opening["as_of"]:
                ledger[position_id] += trade["quantity"] * (1 if trade["side"] == "BUY" else -1)
            by_position[position_id].append(trade)
        broker = {p["con_id"]: p for p in self.db.broker_positions(self.portfolio_id)}
        broker_complete = bool(self.db.metadata(f"broker_positions_refresh_{self.portfolio_id}"))
        nav = self.db.effective_nav(self.portfolio_id)
        rows = []
        prepared_history = {}
        histories = {}
        fx_rates = {}

        def fx_rate(currency, day):
            key = (currency, day)
            if key not in fx_rates:
                fx_rates[key] = self.db.fx_rate(currency, day)
            return fx_rates[key]

        for position in self._positions(position_ids):
            if position_ids is not None and position["id"] not in position_ids:
                continue
            if position["security_type"] == "CASH":
                continue  # FX pair executions do not represent security holdings.
            row = dict(position)
            opening = openings.get(position["id"])
            opening_qty = float(opening["quantity"]) if opening else 0.0
            position_trades = by_position[position["id"]]
            open_buys = [(t, remaining[t["id"]]) for t in position_trades
                         if t["side"] == "BUY" and remaining[t["id"]] > 1e-9]
            open_qty = sum(qty for _, qty in open_buys)
            estimated_openings = [t["trade_date"] for t, _ in open_buys
                                  if t["source"] == "CSV market baseline"]
            row["estimated_baseline"] = min(estimated_openings) if estimated_openings else None
            row["recorded_qty"] = position["position_qty"]
            raw_qty = opening_qty + ledger[position["id"]]
            row["position_qty"] = 0.0 if abs(raw_qty) < 1e-9 else raw_qty
            multiplier = valid_multiplier(position["multiplier"])
            complete_open_lots = (open_qty > 1e-9 and row["position_qty"] > 0 and
                                  abs(open_qty - row["position_qty"]) < 1e-9 and
                                  multiplier is not None and all(
                                      valid_price(t["fx_to_usd"]) is not None and
                                      t["multiplier"] == multiplier
                                      for t, _ in open_buys))
            row["open_pnl_fee_pending"] = any(not t["commission_complete"] for t, _ in open_buys)
            row["average_cost"] = (
                sum(qty * (t["price"] * multiplier +
                           effective_commission_usd(t) / t["quantity"] / t["fx_to_usd"])
                    for t, qty in open_buys) / (open_qty * multiplier)
                if complete_open_lots else None)
            current_fx = fx_rate(position["currency"], date.today().isoformat())
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
            history = self._history(position, quote, histories)
            as_of = self._as_of(position)
            prior_bar = next((bar for bar in reversed(history)
                              if bar["day"] < as_of.isoformat() and
                              valid_price(bar["close"]) is not None and
                              (as_of - date.fromisoformat(bar["day"])).days <= 7), None)
            ticks = quote.get("ticks", {})
            tick_close = (ticks.get("delayed_close")
                          if quote.get("mode") == "Delayed" and
                          valid_price(ticks.get("delayed_close")) is not None
                          else ticks.get("close"))
            prior_close = (tick_close if valid_price(tick_close) is not None else
                           prior_bar["close"] if prior_bar else quote.get("prior_close"))
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
            tick_key = ("delayed_last" if source == "DELAYED LAST" else
                        "last" if source in ("LIVE LAST", "FROZEN LAST") else None)
            tick_time = (quote.get("tick_updated") or {}).get(tick_key) if tick_key else None
            stale_tick = bool(tick_time and self._quote_day(position, tick_time) != as_of)
            if stale_tick:
                row["daily_move"] = row["one_day"] = None
            # A price-only shortcut cannot value partial, missing, or short lots.
            # The broker-compatible open P&L below uses each surviving purchase's
            # acquisition FX and allocated fee.
            row["unrealized_pnl"] = row["unrealized_pct"] = None
            prior_day = (prior_bar["day"] if prior_bar and
                         valid_price(tick_close) is None else
                         (as_of - timedelta(days=1)).isoformat())
            today_trades = [t for t in position_trades if t["trade_date"] == as_of.isoformat()]
            row["traded_today"] = bool(today_trades)
            row["daily_pnl_fee_pending"] = any(not t["commission_complete"] for t in today_trades)
            row["daily_pnl"] = flow_adjusted_daily_pnl(
                row["position_qty"], price, current_fx, prior_close,
                fx_rate(position["currency"], prior_day), position["multiplier"],
                today_trades)
            if abs(row["position_qty"]) > 1e-9 and (
                (cached_close and source != f"SAVED SESSION BAR {as_of.isoformat()}") or
                source == "CLOSE" or source.startswith("CACHED QUOTE") or stale_tick):
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
            row.update(window_returns(None if stale_tick or source.startswith("CACHED QUOTE") else price,
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
            if stale_tick:
                row["data_status"] = "STALE QUOTE"
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
            elif row["open_pnl_fee_pending"]:
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
            if row["estimated_baseline"]:
                row["data_status"] += " · ESTIMATED CSV COST"
            rows.append(row)
        return rows

    def lots_rows(self, portfolio_rows=None):
        trade_rows = self._trades()
        trades = {t["id"]: t for t in trade_rows}
        positions = {p["id"]: p for p in self._positions()}
        valuations = {p["id"]: p for p in (portfolio_rows if portfolio_rows is not None else self.portfolio_rows())}
        reconciliation = {key: p["reconciliation"] for key, p in valuations.items()}
        rows = []
        for match in self._matches():
            buy, sell = trades[match["buy_trade_id"]], trades[match["sell_trade_id"]]
            if buy["security_type"] == "CASH":
                continue
            qty = match["matched_qty"]
            row = self._base_lot(buy, qty)
            short = match["origin"] == "auto_short"
            row["lot_state"] = "Closed short" if short else "Closed"
            row["match_origin"] = ("Manual" if match["origin"] == "manual" else
                                   "Auto (short)" if short else "Auto")
            row.update({"match_id": match["match_id"], "sell_date": sell["trade_date"],
                        "sell_trade_id": sell["id"],
                        "sell_qty": qty, "sell_price": sell["price"], "sell_currency": sell["currency"],
                        "sell_fx": sell["fx_to_usd"], "sell_notes": sell["notes"],
                        "sell_value_lc": qty * sell["price"] * sell["multiplier"] if valid_multiplier(sell["multiplier"]) else None,
                        "duration": abs((date.fromisoformat(sell["trade_date"]) - date.fromisoformat(buy["trade_date"])).days)
                        if buy["source"] != "CSV market baseline" and buy["trade_date"] and sell["trade_date"]
                        else None})
            row.update(closed_lot(buy, sell, qty))
            row["buy_fee_pending"] = not buy["commission_complete"]
            row["sell_fee_pending"] = not sell["commission_complete"]
            row["fee_pending"] = row["buy_fee_pending"] or row["sell_fee_pending"]
            if not buy["commission_complete"] or not sell["commission_complete"]:
                if not buy["commission_complete"]:
                    row["buy_commission"] = None
                if not sell["commission_complete"]:
                    row["sell_commission"] = None
                row["lot_note"] = "Fee pending"
            else:
                row["lot_note"] = ("Short sale covered by later buy" if short else "") if row.get("realized_net") is not None else "FX or multiplier required"
            if buy["source"] == "CSV market baseline":
                row["lot_note"] = "; ".join(filter(None, (row["lot_note"],
                    "Estimated CSV closing value; acquisition date unknown")))
            rows.append(row)
        remaining = self._remaining(trade_rows)
        position_as_of = {}
        prepared_history = {}
        histories = {}
        returns_by_position = {}
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
            row["fee_pending"] = not buy["commission_complete"]
            if buy["security_type"] == "FUT":
                row["current_value"] = row["net_unrealized"] = row["unrealized_pct"] = None
                row["lot_note"] = "Futures valuation unavailable"
            if not buy["commission_complete"]:
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
                    history = self._history(position, quote, histories)
                    prepared = prepare_history(history, as_of)
                    if position.get("con_id"):
                        prepared_history[bar_key] = prepared
                returns = returns_by_position.get(position["id"])
                if returns is None:
                    returns = window_returns(price, prepared[0], prepared[1])
                    returns_by_position[position["id"]] = returns
                row["mtd"], row["ytd"] = returns["mtd"], returns["ytd"]
            if buy["source"] == "CSV market baseline":
                row["lot_note"] = "; ".join(filter(None, (row.get("lot_note"),
                    "Estimated CSV closing value; acquisition date unknown")))
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

    def _quote_day(self, position, timestamp):
        """Date a received tick in its instrument's trading timezone."""
        try:
            stamp = datetime.fromisoformat(timestamp)
            if stamp.tzinfo is None:
                stamp = stamp.astimezone()
            if position.get("con_id"):
                row = self.db.conn.execute(
                    "SELECT time_zone FROM contracts WHERE position_id=?", (position["id"],)
                ).fetchone()
                if row and row[0]:
                    return stamp.astimezone(ZoneInfo(row[0])).date()
            exchange = str(position.get("primary_exchange") or position.get("exchange") or "").upper()
            if position.get("currency") == "USD" and exchange in {
                "NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "CBOE", "SMART"
            }:
                return stamp.astimezone(ZoneInfo("America/New_York")).date()
            return stamp.astimezone().date()
        except (TypeError, ValueError, OverflowError, KeyError):
            return None
