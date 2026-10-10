"""Account history and lot-based analytics, with explicit gaps for missing data."""

import csv
import math
from collections import defaultdict
from datetime import date


def read_history_csv(path):
    """Read a daily USD account history before writing any part of it."""
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "nav_usd", "external_flow_usd"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV needs date, nav_usd, and external_flow_usd columns")
        rows = []
        seen = set()
        for line, raw in enumerate(reader, 2):
            try:
                day = date.fromisoformat(raw["date"].strip()).isoformat()
                nav = float(raw["nav_usd"].replace(",", ""))
                flow = float(raw["external_flow_usd"].replace(",", ""))
                cash_text = (raw.get("cash_usd") or "").strip()
                cash = float(cash_text.replace(",", "")) if cash_text else None
            except (TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"Invalid account history on CSV line {line}") from exc
            if day in seen or nav < 0 or not all(math.isfinite(v) for v in
                                                   (nav, flow, *([cash] if cash is not None else []))):
                raise ValueError(f"Duplicate date or invalid amount on CSV line {line}")
            seen.add(day)
            rows.append((day, nav, cash, flow))
    if not rows:
        raise ValueError("Account history CSV has no rows")
    return sorted(rows)


def import_history_csv(db, portfolio_id, path):
    rows = read_history_csv(path)
    db.backup()
    with db.conn:
        for day, nav, cash, flow in rows:
            db.conn.execute("""INSERT INTO account_history
                (portfolio_id,day,nav_usd,cash_usd,source,flow_covered)
                VALUES (?,?,?,?,'imported',1)
                ON CONFLICT(portfolio_id,day) DO UPDATE SET
                nav_usd=excluded.nav_usd,cash_usd=excluded.cash_usd,
                source=excluded.source,flow_covered=1
                WHERE account_history.source!='flex'""", (portfolio_id, day, nav, cash))
            db.conn.execute("""INSERT INTO external_flows
                (portfolio_id,source_id,day,amount_usd,kind)
                VALUES (?,?,?,?, 'net external')
                ON CONFLICT(portfolio_id,source_id) DO UPDATE SET amount_usd=excluded.amount_usd""",
                (portfolio_id, f"history-csv:{day}", day, flow))
    return len(rows)


def analytics_rows(snapshots, flows, lots, *, basis_complete=True):
    """One row per observed date. Returns require consecutive covered valuations."""
    by_day = {row["day"]: row for row in snapshots}
    flow_by_day = defaultdict(float)
    for flow in flows:
        flow_by_day[flow["day"]] += float(flow["amount_usd"])
    realized = defaultdict(float)
    basis_events = defaultdict(float)
    for lot in lots:
        if lot.get("lot_state") == "Closed short":
            basis_complete = False
            continue
        buy_day = lot.get("buy_date")
        sell_day = lot.get("sell_date")
        basis = lot.get("buy_value_usd")
        commission = lot.get("buy_commission")
        tax = lot.get("buy_tax", 0)
        if buy_day and (basis is None or commission is None or tax is None):
            basis_complete = False
        elif buy_day:
            invested = float(basis) + float(commission) + float(tax)
            basis_events[buy_day] += invested
            if sell_day:
                basis_events[sell_day] -= invested
        if sell_day:
            gain = lot.get("realized_net")
            if gain is not None:
                realized[sell_day] += float(gain)
    days = sorted(set(by_day) | set(realized) | set(basis_events) | set(flow_by_day))
    result = []
    prior = None
    capital = cumulative_realized = 0.0
    cumulative_return = 1.0
    cumulative_pnl = 0.0
    return_chain_valid = False
    for day in days:
        capital += basis_events[day]
        cumulative_realized += realized[day]
        snap = by_day.get(day)
        period_pnl = period_return = cumulative = None
        if snap and prior:
            gap = (date.fromisoformat(day) - date.fromisoformat(prior["day"])).days
            covered = gap <= 4 and snap["flow_covered"] and prior["flow_covered"]
            if covered and prior["nav_usd"] > 0:
                net_flow = sum(amount for flow_day, amount in flow_by_day.items()
                               if prior["day"] < flow_day <= day)
                if not return_chain_valid:
                    cumulative_pnl = 0.0
                period_pnl = float(snap["nav_usd"]) - float(prior["nav_usd"]) - net_flow
                cumulative_pnl += period_pnl
                period_return = period_pnl / float(prior["nav_usd"])
                if not return_chain_valid:
                    cumulative_return = 1.0
                cumulative_return *= 1 + period_return
                cumulative = cumulative_return - 1
                return_chain_valid = True
            else:
                return_chain_valid = False
        if snap:
            prior = snap
        result.append({"day": day, "nav": snap["nav_usd"] if snap else None,
                       "cash": snap["cash_usd"] if snap else None,
                       "daily_pnl": snap.get("daily_pnl_usd") if snap else None,
                       "source": snap["source"] if snap else "",
                       "external_flow": (flow_by_day[day] if (snap and snap["flow_covered"])
                                         or day in flow_by_day else None), "pnl": period_pnl,
                       "cumulative_pnl": cumulative_pnl if return_chain_valid else None,
                       "return": period_return, "cumulative_return": cumulative,
                       "realized": realized[day], "cumulative_realized": cumulative_realized,
                       "capital": max(0.0, capital) if basis_complete else None})
    return result


def normalize_for_window(source_rows):
    """Rebase covered account growth and P&L to the first NAV in a date range."""
    rows = [dict(row) for row in source_rows]
    if not rows:
        return rows, False
    realized_before = rows[0]["cumulative_realized"] - rows[0]["realized"]
    for row in rows:
        row["rebased_realized"] = row["cumulative_realized"] - realized_before
        row["normalized_nav"] = None
        row["rebased_pnl"] = None
    observed = [row for row in rows if row["nav"] is not None]
    if not observed:
        return rows, False
    # The first closing valuation is the visible baseline, even when its daily
    # change was calculated from a valuation outside the selected range.
    observed[0]["pnl"] = observed[0]["return"] = None
    complete = len(observed) >= 2 and all(
        row["pnl"] is not None and row["return"] is not None for row in observed[1:])
    if complete:
        index, pnl = 100.0, 0.0
        observed[0]["normalized_nav"] = index
        observed[0]["rebased_pnl"] = pnl
        for row in observed[1:]:
            index *= 1 + row["return"]
            pnl += row["pnl"]
            row["normalized_nav"] = index
            row["rebased_pnl"] = pnl
    return rows, complete


def performance_window_rows(snapshots, flows, first, last):
    """Rebase account changes to the selected range using the preceding close."""
    observations = sorted((row for row in snapshots if row["day"] <= last.isoformat()),
                          key=lambda row: row["day"])
    visible = [row for row in observations if first.isoformat() <= row["day"]]
    if not visible:
        return [], False
    earlier = [row for row in observations if row["day"] < first.isoformat()]
    predecessor = earlier[-1] if earlier else None
    first_observation = visible[0]
    usable_predecessor = bool(predecessor and
                              (date.fromisoformat(first_observation["day"]) -
                               date.fromisoformat(predecessor["day"])).days <= 4 and
                              predecessor["flow_covered"] and first_observation["flow_covered"] and
                              predecessor["nav_usd"] > 0)
    baseline = predecessor if usable_predecessor else first_observation
    # The first available close is a valid starting point for All history.
    # A missing predecessor inside history must not silently discard that day.
    valid_baseline = usable_predecessor or (not earlier and
                                           first.isoformat() == first_observation["day"])
    baseline_nav = float(baseline["nav_usd"])
    flow_by_day = defaultdict(float)
    for flow in flows:
        if baseline["day"] < flow["day"] <= last.isoformat():
            flow_by_day[flow["day"]] += float(flow["amount_usd"])

    rows = []
    previous = baseline
    growth = 1.0
    covered = bool(valid_baseline and baseline["flow_covered"])
    cumulative_flow = 0.0
    completed_count = 0
    for snapshot in visible:
        day = snapshot["day"]
        if snapshot["source"] == "live" and not snapshot["flow_covered"]:
            nav = float(snapshot["nav_usd"])
            rows.append({"day": day, "nav": nav,
                         "nav_change": nav - baseline_nav if valid_baseline else None,
                         "adjusted_change": None, "performance": None,
                         "daily_pnl": None, "daily_return": None,
                         "external_flow": flow_by_day[day], "source": "live"})
            continue
        completed_count += 1
        daily_flow = sum(amount for flow_day, amount in flow_by_day.items()
                         if previous["day"] < flow_day <= day)
        cumulative_flow += daily_flow
        nav = float(snapshot["nav_usd"])
        daily_pnl = daily_return = None
        if snapshot is not baseline:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous["day"])).days
            capital = float(previous["nav_usd"]) + daily_flow
            if covered and gap <= 4 and snapshot["flow_covered"] and capital > 0:
                daily_pnl = nav - float(previous["nav_usd"]) - daily_flow
                daily_return = daily_pnl / capital
                growth *= 1 + daily_return
            else:
                covered = False
        adjusted = nav - baseline_nav - cumulative_flow if covered else None
        rows.append({"day": day, "nav": nav,
                     "nav_change": nav - baseline_nav if valid_baseline else None,
                     "adjusted_change": adjusted,
                     "performance": (growth - 1) * 100 if covered else None,
                     "daily_pnl": daily_pnl, "daily_return": daily_return,
                     "external_flow": flow_by_day[day], "source": snapshot["source"]})
        previous = snapshot

    # Show weekend and holiday transfers on their actual dates in the table.
    observed_days = {row["day"] for row in rows}
    for day, amount in flow_by_day.items():
        if first.isoformat() <= day <= last.isoformat() and day not in observed_days:
            rows.append({"day": day, "nav": None, "nav_change": None,
                         "adjusted_change": None, "performance": None,
                         "daily_pnl": None, "daily_return": None,
                         "external_flow": amount, "source": "flow"})
    rows.sort(key=lambda row: row["day"])
    return rows, covered and (completed_count >= 2 or usable_predecessor)


def cash_share_rows(snapshots, first, last):
    """Cash as a share of NAV at valid account observations in the range."""
    rows = []
    for snapshot in snapshots:
        if not first.isoformat() <= snapshot["day"] <= last.isoformat():
            continue
        cash, nav = snapshot.get("cash_usd"), snapshot.get("nav_usd")
        if cash is None or nav is None:
            continue
        cash, nav = float(cash), float(nav)
        if not math.isfinite(cash) or not math.isfinite(nav) or nav <= 0:
            continue
        rows.append({"day": snapshot["day"], "cash": cash, "nav": nav,
                     "percent": cash / nav * 100, "source": snapshot.get("source", "")})
    rows.sort(key=lambda row: row["day"])
    completed = [row for row in rows if row["source"] != "live"]
    return completed if completed else rows


def sharpe_ratio(rows, annual_risk_free=0.0):
    observed = [row for row in rows if row["nav"] is not None]
    returns = [row["return"] for row in observed if row["return"] is not None and
               row["source"] != "live"]
    if len(returns) < 63 or any(row["return"] is None for row in observed[1:]):
        return None
    daily_risk_free = (1 + annual_risk_free) ** (1 / 252) - 1
    excess = [value - daily_risk_free for value in returns]
    mean = sum(excess) / len(excess)
    deviation = math.sqrt(sum((value - mean) ** 2 for value in excess) / (len(excess) - 1))
    return mean / deviation * math.sqrt(252) if deviation else None


def capital_flow_rows(trades, first, last):
    """Gross security purchases, sale proceeds, and net cash deployed in a range."""
    by_day = defaultdict(lambda: [0.0, 0.0])
    missing = False
    for trade in trades:
        day = trade.get("trade_date")
        if not day or not first <= date.fromisoformat(day) <= last or trade.get("security_type") == "CASH":
            continue  # An FX conversion moves cash between currencies; it is not deployment.
        gross = trade.get("gross_usd")
        if gross is None or not math.isfinite(float(gross)) or float(gross) < 0:
            missing = True
            continue
        side = trade["side"]
        if side == "BUY":
            by_day[day][0] += float(gross)
        elif side == "SELL":
            by_day[day][1] += float(gross)
    bought = sold = 0.0
    rows = []
    for day, (day_bought, day_sold) in sorted(by_day.items()):
        bought += day_bought
        sold += day_sold
        rows.append({"day": day, "bought": day_bought, "sold": day_sold,
                     "net": day_bought - day_sold, "cumulative_bought": bought,
                     "cumulative_sold": sold, "cumulative_net": bought - sold})
    return rows, None if missing else {"bought": bought, "sold": sold, "net": bought - sold}
