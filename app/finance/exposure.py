"""Current portfolio exposure, with broker-sourced option and company facts."""

from collections import defaultdict
from datetime import date, datetime, timedelta
import math


def _valid(value, *, positive=False):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and (number > 0 if positive else True) else None


def _recent(stamp):
    try:
        return datetime.now() - datetime.fromisoformat(stamp) <= timedelta(days=7)
    except (TypeError, ValueError):
        return False


def save_broker_fact(db, portfolio_id, event):
    """Persist one validated IBKR event; never infer absent broker fields."""
    kind, con_id, *payload = event
    if kind not in {"exposure_detail", "exposure_delta",
                    "exposure_underlying_price"} or not isinstance(con_id, int) or con_id <= 0:
        return False
    stamp = datetime.now().isoformat(timespec="seconds")
    fields = {}
    if kind == "exposure_detail":
        detail = payload[0]
        for key in ("sector", "category", "stock_type", "under_symbol"):
            if detail.get(key):
                fields[key] = str(detail[key])[:200]
        under_id = detail.get("under_con_id")
        if isinstance(under_id, int) and under_id > 0:
            fields["under_con_id"] = under_id
        fields["details_as_of"] = stamp
    elif kind == "exposure_delta":
        delta = _valid(payload[0])
        if delta is None or abs(delta) > 1:
            return False
        fields = {"delta": delta, "delta_mode": str(payload[1]), "delta_as_of": stamp}
    else:
        price = _valid(payload[0], positive=True)
        if price is None:
            return False
        fields = {"underlying_price": price, "underlying_currency": str(payload[1]),
                  "underlying_mode": str(payload[2]), "underlying_as_of": stamp}
    columns = list(fields)
    sql = ("INSERT INTO exposure_facts(portfolio_id,con_id," + ",".join(columns) + ") VALUES (" +
           ",".join("?" for _ in range(2 + len(columns))) + ") ON CONFLICT(portfolio_id,con_id) DO UPDATE SET " +
           ",".join(f"{name}=excluded.{name}" for name in columns))
    with db.conn:
        db.conn.execute(sql, (portfolio_id, con_id, *(fields[key] for key in columns)))
    return True


def custom_sets(db, portfolio_id):
    return [row[0] for row in db.conn.execute(
        "SELECT name FROM exposure_custom_sets WHERE portfolio_id=? ORDER BY name", (portfolio_id,))]


def add_custom_set(db, portfolio_id, name):
    name = name.strip()[:60]
    if not name:
        raise ValueError("Enter a name for the custom set")
    with db.conn:
        db.conn.execute("INSERT OR IGNORE INTO exposure_custom_sets(portfolio_id,name) VALUES (?,?)",
                        (portfolio_id, name))


def rename_custom_set(db, portfolio_id, old_name, new_name):
    new_name = new_name.strip()[:60]
    if not new_name or new_name in custom_sets(db, portfolio_id):
        raise ValueError("Choose a new, unique set name")
    with db.conn:
        db.conn.execute("UPDATE exposure_custom_sets SET name=? WHERE portfolio_id=? AND name=?",
                        (new_name, portfolio_id, old_name))
        db.conn.execute("UPDATE exposure_custom SET set_name=? WHERE portfolio_id=? AND set_name=?",
                        (new_name, portfolio_id, old_name))
    return new_name


def delete_custom_set(db, portfolio_id, name):
    with db.conn:
        db.conn.execute("DELETE FROM exposure_custom WHERE portfolio_id=? AND set_name=?",
                        (portfolio_id, name))
        db.conn.execute("DELETE FROM exposure_custom_sets WHERE portfolio_id=? AND name=?",
                        (portfolio_id, name))


def custom_labels(db, portfolio_id, set_name):
    return {row["con_id"]: row["label"] for row in db.conn.execute(
        "SELECT con_id,label FROM exposure_custom WHERE portfolio_id=? AND set_name=?",
        (portfolio_id, set_name))}


def set_custom_label(db, portfolio_id, set_name, con_id, label):
    set_name, label = set_name.strip()[:60], label.strip()[:80]
    if not set_name or not isinstance(con_id, int) or con_id <= 0:
        raise ValueError("Choose a set and an instrument")
    add_custom_set(db, portfolio_id, set_name)
    with db.conn:
        if label:
            db.conn.execute("""INSERT INTO exposure_custom(portfolio_id,set_name,con_id,label)
                VALUES (?,?,?,?) ON CONFLICT(portfolio_id,set_name,con_id) DO UPDATE SET
                label=excluded.label""", (portfolio_id, set_name, con_id, label))
        else:
            db.conn.execute("DELETE FROM exposure_custom WHERE portfolio_id=? AND set_name=? AND con_id=?",
                            (portfolio_id, set_name, con_id))


def exposure_snapshot(db, service, portfolio_id, *, custom_set="", portfolio_rows=None):
    facts = {row["con_id"]: dict(row) for row in db.conn.execute(
        "SELECT * FROM exposure_facts WHERE portfolio_id=?", (portfolio_id,))}
    labels = {(row["set_name"], row["con_id"]): row["label"] for row in db.conn.execute(
        "SELECT * FROM exposure_custom WHERE portfolio_id=?", (portfolio_id,))}
    nav = db.effective_nav(portfolio_id)
    positions = []
    issues = []
    fx_rates = {}
    today = date.today().isoformat()
    source_rows = list(portfolio_rows if portfolio_rows is not None else service.portfolio_rows())
    stock_marks = {row.get("con_id"): row for row in source_rows
                   if row.get("security_type") == "STK" and row.get("con_id")}
    for row in source_rows:
        qty = _valid(row.get("position_qty"))
        if qty is None or abs(qty) < 1e-9:
            continue
        con_id = row.get("con_id")
        fact = facts.get(con_id, {})
        security_type = row.get("security_type", "STK")
        value = _valid(row.get("value"))
        adjusted = value
        equivalent_shares = None
        issue = "" if value is not None else "Market value unavailable"
        under_id = fact.get("under_con_id") if security_type == "OPT" else con_id
        under_fact = facts.get(under_id, {}) if under_id else {}
        underlying_price = fact.get("underlying_price")
        underlying_as_of = fact.get("underlying_as_of", "")
        underlying_mode = fact.get("underlying_mode", "")
        underlying_currency = fact.get("underlying_currency") or row.get("currency")
        if security_type == "OPT":
            delta = _valid(fact.get("delta"))
            price = _valid(underlying_price, positive=True)
            if (not _recent(underlying_as_of) or price is None) and under_id in stock_marks:
                stock = stock_marks[under_id]
                candidate = _valid(stock.get("last_px"), positive=True)
                if (candidate is not None and _recent(stock.get("last_updated")) and
                        stock.get("price_source") != "MOCK"):
                    price = candidate
                    underlying_price = candidate
                    underlying_as_of = stock["last_updated"]
                    underlying_mode = f"Holding quote: {stock.get('price_source', 'IBKR')}"
                    underlying_currency = stock.get("currency") or underlying_currency
            multiplier = _valid(row.get("multiplier"), positive=True)
            if underlying_currency and underlying_currency not in fx_rates:
                fx_rates[underlying_currency] = db.fx_rate(underlying_currency, today)
            fx = fx_rates.get(underlying_currency)
            delta_recent = delta is not None and _recent(fact.get("delta_as_of"))
            price_recent = price is not None and _recent(underlying_as_of)
            if not delta_recent or not price_recent or multiplier is None or _valid(fx, positive=True) is None:
                adjusted = None
                issue = ", ".join(name for name, present in (
                    ("recent delta", delta_recent), ("recent underlying price", price_recent),
                    ("multiplier", multiplier is not None), ("underlying FX", _valid(fx, positive=True) is not None))
                                  if not present) + " unavailable"
            else:
                equivalent_shares = qty * multiplier * delta
                adjusted = equivalent_shares * price * fx
        elif security_type in {"FUT", "FOP", "CFD", "IOPT", "WAR", "BAG"}:
            adjusted = None
            issue = "Adjusted exposure unsupported for this derivative"
        if row.get("reconciliation") == "DIFFERENCE":
            adjusted = None
            issue = "Broker position mismatch"
        classification = under_fact or fact
        sector = classification.get("sector") or fact.get("sector") or "Unclassified"
        stock_type = (classification.get("stock_type") or fact.get("stock_type") or "").upper()
        category = (classification.get("category") or fact.get("category") or "").upper()
        is_fund = any(token in stock_type + " " + category + " " + sector.upper()
                      for token in ("ETF", "EXCHANGE TRADED FUND", "MUTUAL FUND"))
        if is_fund:
            sector = "Funds / ETFs"
        custom = labels.get((custom_set, under_id), "Unclassified") if custom_set else "Unclassified"
        positions.append({"id": row.get("id"), "con_id": con_id, "under_con_id": under_id,
                          "under_symbol": fact.get("under_symbol", ""),
                          "symbol": row.get("display_ticker", ""), "type": security_type,
                          "quantity": qty, "value": value, "adjusted": adjusted,
                          "equivalent_shares": equivalent_shares, "delta": fact.get("delta"),
                          "delta_mode": fact.get("delta_mode", ""),
                          "delta_as_of": fact.get("delta_as_of", ""),
                          "underlying_price": underlying_price,
                          "underlying_mode": underlying_mode,
                          "underlying_as_of": underlying_as_of,
                          "price_source": row.get("price_source", ""),
                          "mark_as_of": row.get("last_updated", ""),
                          "sector": sector,
                          "custom": custom, "issue": issue})
        if issue:
            issues.append(f"{row.get('display_ticker', '')}: {issue}")
    totals = {}
    for field in ("value", "adjusted"):
        values = [item[field] for item in positions]
        totals[field] = sum(values) if all(v is not None for v in values) else None
        totals[field + "_gross"] = sum(abs(v) for v in values) if all(v is not None for v in values) else None
    return {"nav": nav, "positions": positions, "totals": totals, "issues": issues,
            "as_of": max((item["mark_as_of"] for item in positions), default="")}


def grouped_exposure(positions, group_field, value_field):
    totals = defaultdict(lambda: [0.0, 0.0, 0, False])
    for position in positions:
        bucket = totals[position[group_field]]
        bucket[2] += 1
        value = position[value_field]
        if value is None:
            bucket[3] = True
            continue
        bucket[0] += value
        bucket[1] += abs(value)
    rows = [(key, None if partial else net, None if partial else gross, count)
            for key, (net, gross, count, partial) in totals.items()]
    return sorted(rows, key=lambda item: (item[2] is None, -(item[2] or 0), item[0]))


def exposure_mix(positions, group_field, value_field):
    """Gross absolute slices with explicit coverage for unavailable valuations."""
    buckets = defaultdict(float)
    known_net = 0.0
    missing = 0
    for position in positions:
        value = position[value_field]
        if value is None:
            missing += 1
            continue
        buckets[position[group_field]] += abs(value)
        known_net += value
    slices = sorted(((name, amount) for name, amount in buckets.items() if amount > 0),
                    key=lambda item: (-item[1], item[0]))
    return {"slices": slices, "gross": sum(buckets.values()), "known_net": known_net,
            "missing": missing}
