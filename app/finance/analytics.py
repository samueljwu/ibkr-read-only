"""Flow-adjusted account performance, cash share, and capital deployment.

Daily account returns follow IBKR's published TWR formula; gaps in covered
history are left visible rather than bridged.
"""

import math
from collections import defaultdict
from datetime import date


def return_base(prior_nav, period_flow):
    """IBKR's published TWR denominator: beginning NAV plus net external flow."""
    return float(prior_nav) + float(period_flow)


def flex_monthly_history(snapshots, flows):
    """Summarize completed daily closes without bridging missing NAV or flow coverage."""
    completed = sorted((row for row in snapshots if row["source"] != "live"),
                       key=lambda row: row["day"])
    dated_flows = sorted((row["day"], float(row["amount_usd"])) for row in flows)
    rows = []
    previous = None
    flow_index = 0
    growth = 1.0
    covered = False
    for snapshot in completed:
        day = snapshot["day"]
        month = day[:7]
        if not rows or rows[-1]["month"] != month:
            rows.append({"month": month, "period_from": day, "period_to": day,
                         "nav_usd": snapshot["nav_usd"], "return_pct": None})
            growth = 1.0
            covered = bool(previous and previous["flow_covered"] and
                           snapshot["flow_covered"] and previous["nav_usd"] > 0)
        daily_flow = 0.0
        while flow_index < len(dated_flows) and dated_flows[flow_index][0] <= day:
            flow_day, amount = dated_flows[flow_index]
            if previous and flow_day > previous["day"]:
                daily_flow += amount
            flow_index += 1
        if previous:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous["day"])).days
            capital = return_base(previous["nav_usd"], daily_flow)
            if gap > 4 or not previous["flow_covered"] or not snapshot["flow_covered"] or capital <= 0:
                covered = False
            elif covered:
                growth *= 1 + (float(snapshot["nav_usd"]) -
                               float(previous["nav_usd"]) - daily_flow) / capital
        row = rows[-1]
        row["period_to"] = day
        row["nav_usd"] = snapshot["nav_usd"]
        row["return_pct"] = (growth - 1) * 100 if covered else None
        previous = snapshot
    return rows


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
    visible_flow_by_day = defaultdict(float)
    for flow in flows:
        if first.isoformat() <= flow["day"] <= last.isoformat():
            visible_flow_by_day[flow["day"]] += float(flow["amount_usd"])
        if baseline["day"] < flow["day"] <= last.isoformat():
            flow_by_day[flow["day"]] += float(flow["amount_usd"])
    dated_flows = sorted(flow_by_day.items())
    flow_index = 0

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
                         "external_flow": visible_flow_by_day[day], "source": "live"})
            continue
        completed_count += 1
        daily_flow = 0.0
        while flow_index < len(dated_flows) and dated_flows[flow_index][0] <= day:
            daily_flow += dated_flows[flow_index][1]
            flow_index += 1
        cumulative_flow += daily_flow
        nav = float(snapshot["nav_usd"])
        daily_pnl = daily_return = None
        if snapshot is not baseline:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous["day"])).days
            capital = return_base(previous["nav_usd"], daily_flow)
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
                     "external_flow": visible_flow_by_day[day], "source": snapshot["source"]})
        previous = snapshot

    # Show weekend and holiday transfers on their actual dates in the table.
    observed_days = {row["day"] for row in rows}
    for day, amount in visible_flow_by_day.items():
        if first.isoformat() <= day <= last.isoformat() and day not in observed_days:
            rows.append({"day": day, "nav": None, "nav_change": None,
                         "adjusted_change": None, "performance": None,
                         "daily_pnl": None, "daily_return": None,
                         "external_flow": amount, "source": "flow"})
    rows.sort(key=lambda row: row["day"])
    return rows, covered and (completed_count >= 2 or usable_predecessor)


def covered_daily_pnl_rows(snapshots, flows):
    """Daily account P&L wherever adjacent completed closes have flow coverage.

    A gap leaves that day blank, then the new close can serve as the next
    baseline. This lets a calendar browse all history without bridging gaps.
    """
    observations = sorted(snapshots, key=lambda row: row["day"])
    dated_flows = sorted((row["day"], float(row["amount_usd"])) for row in flows)
    rows = []
    previous = None
    flow_index = 0
    for snapshot in observations:
        day = snapshot["day"]
        if snapshot["source"] == "live":
            rows.append({"day": day, "daily_pnl": None,
                         "daily_return": None, "source": "live"})
            continue
        daily_flow = 0.0
        while flow_index < len(dated_flows) and dated_flows[flow_index][0] <= day:
            flow_day, amount = dated_flows[flow_index]
            if previous and flow_day > previous["day"]:
                daily_flow += amount
            flow_index += 1
        daily_pnl = daily_return = None
        if previous:
            gap = (date.fromisoformat(day) - date.fromisoformat(previous["day"])).days
            capital = return_base(previous["nav_usd"], daily_flow)
            if (gap <= 4 and previous["flow_covered"] and snapshot["flow_covered"]
                    and capital > 0):
                daily_pnl = float(snapshot["nav_usd"]) - float(previous["nav_usd"]) - daily_flow
                daily_return = daily_pnl / capital
        rows.append({"day": day, "daily_pnl": daily_pnl,
                     "daily_return": daily_return, "source": snapshot["source"]})
        previous = snapshot
    return rows


def daily_pnl_summary(rows, complete):
    """Extremes and annualized Sharpe from covered, completed daily returns."""
    result = {"max_usd_gain": None, "max_usd_loss": None,
              "max_pct_gain": None, "max_pct_loss": None,
              "sharpe": None, "return_count": 0}
    if not complete:
        return result
    days = [row for row in rows if row["source"] != "live" and
            row["daily_pnl"] is not None and row["daily_return"] is not None and
            math.isfinite(row["daily_pnl"]) and math.isfinite(row["daily_return"])]
    gains = [row for row in days if row["daily_pnl"] > 0]
    losses = [row for row in days if row["daily_pnl"] < 0]
    result["max_usd_gain"] = max(gains, key=lambda row: row["daily_pnl"], default=None)
    result["max_usd_loss"] = min(losses, key=lambda row: row["daily_pnl"], default=None)
    result["max_pct_gain"] = max(gains, key=lambda row: row["daily_return"], default=None)
    result["max_pct_loss"] = min(losses, key=lambda row: row["daily_return"], default=None)
    returns = [row["daily_return"] for row in days]
    result["return_count"] = len(returns)
    if len(returns) >= 2:
        average = math.fsum(returns) / len(returns)
        variance = math.fsum((value - average) ** 2 for value in returns) / (len(returns) - 1)
        if variance > 0:
            result["sharpe"] = average / math.sqrt(variance) * math.sqrt(252)
    return result


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


def capital_flow_rows(trades, first, last):
    """Gross security purchases, sale proceeds, and net cash deployed in a range."""
    by_day = defaultdict(lambda: [0.0, 0.0])
    missing = False
    for trade in trades:
        day = trade.get("trade_date")
        if not day or not first <= date.fromisoformat(day) <= last or trade.get("security_type") in {"CASH", "FUT"}:
            continue  # FX conversions and futures notionals are not cash deployed into securities.
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
