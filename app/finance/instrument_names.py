"""Human-readable instrument labels; stored broker identities remain untouched."""

from datetime import date
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re


_ROOT = r"[A-Z0-9][A-Z0-9./_-]*"


def _date(value):
    value = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{8}(?:\s+.*)?", value):
            value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _strike(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            return ""
        text = format(number, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    except (InvalidOperation, ValueError):
        return ""


def _parsed(value):
    """Decode supported OCC/IBKR/legacy names only when all details validate."""
    value = str(value or "").strip().upper()
    occ = re.fullmatch(rf"({_ROOT}?)\s*(\d{{6}})([CP])(\d{{8}})(?:\s*\[\d+\])?", value)
    if occ:
        root, expiry, right, strike = occ.groups()
        expiry = _date("20" + expiry)
        strike = _strike(Decimal(strike) / 1000)
    else:
        legacy = re.fullmatch(
            rf"({_ROOT})(?:\s+US)?\s+(\d{{1,2}})/(\d{{1,2}})/(\d{{2}}|\d{{4}})\s+([CP])\s*(\d+(?:\.\d+)?)(?:\s+EQUITY)?",
            value)
        if not legacy:
            return {}
        root, month, day, year, right, strike = legacy.groups()
        year = "20" + year if len(year) == 2 else year
        expiry = _date(f"{year}-{int(month):02}-{int(day):02}")
        strike = _strike(strike)
    return {"root": root, "expiry": expiry, "right": right, "strike": strike} if expiry and strike else {}


def instrument_name(row, *, under_symbol=""):
    """Format an option when its facts (or a supported local name) are complete."""
    fallback = str(row.get("display_ticker") or row.get("local_symbol") or row.get("ib_symbol") or "")
    if str(row.get("security_type") or "").upper() not in {"OPT", "FOP"}:
        return fallback
    parsed = _parsed(row.get("local_symbol")) or _parsed(fallback) or _parsed(row.get("ib_symbol"))
    root = str(under_symbol or row.get("ib_symbol") or parsed.get("root") or "").strip().upper()
    # Some legacy rows put the whole contract name in ib_symbol.
    if not re.fullmatch(_ROOT, root) or _parsed(root):
        root = parsed.get("root", "")
    expiry = _date(row.get("expiry")) if row.get("expiry") else parsed.get("expiry", "")
    right = str(row.get("option_right") or parsed.get("right") or "").strip().upper()
    right = {"C": "Call", "CALL": "Call", "P": "Put", "PUT": "Put"}.get(right, "")
    strike = _strike(row["strike"]) if row.get("strike") is not None else parsed.get("strike", "")
    return f"{root} {right} {strike} · {expiry}" if root and right and strike and expiry else fallback


class InstrumentNames:
    """Resolve trade labels via position IDs and portfolio-scoped underlying facts."""

    def __init__(self, db, portfolio_id):
        self.positions = {row["id"]: row for row in db.rows("positions", portfolio_id)}
        by_contract = {row["con_id"]: row for row in self.positions.values() if row.get("con_id")}
        self.underlyings = {}
        self.aliases = defaultdict(set)
        for fact in db.conn.execute(
                "SELECT con_id,under_con_id,under_symbol FROM exposure_facts WHERE portfolio_id=?", (portfolio_id,)):
            underlying = by_contract.get(fact["under_con_id"], {})
            symbol = str(fact["under_symbol"] or underlying.get("ib_symbol") or "").strip().upper()
            if not symbol:
                continue
            self.underlyings[fact["con_id"]] = symbol
            position = by_contract.get(fact["con_id"], {})
            root = self._root(position)
            if root and position.get("security_type") in {"OPT", "FOP"}:
                self.aliases[root].add(symbol)
        self.labels = {pid: self._base_name(row) for pid, row in self.positions.items()}
        groups = defaultdict(list)
        for pid, row in self.positions.items():
            if row.get("security_type") in {"OPT", "FOP"}:
                groups[self.labels[pid]].append(row)
        for label, rows in groups.items():
            identities = {row.get("con_id") or ("position", row["id"]) for row in rows}
            if len(identities) <= 1 or " · " not in label:
                continue
            classes = {row.get("trading_class") for row in rows}
            for row in rows:
                detail = (row["trading_class"] if len(classes) == len(identities) and all(classes) else
                          f"IBKR #{row['con_id']}" if row.get("con_id") else f"position #{row['id']}")
                self.labels[row["id"]] = f"{label} · {detail}"

    @staticmethod
    def _root(row):
        symbol = str(row.get("ib_symbol") or "").strip().upper()
        parsed = _parsed(symbol) or _parsed(row.get("local_symbol")) or _parsed(row.get("display_ticker"))
        return parsed.get("root") or (symbol if re.fullmatch(_ROOT, symbol) else "")

    def _base_name(self, row):
        aliases = self.aliases[self._root(row)]
        symbol = self.underlyings.get(row.get("con_id")) or (next(iter(aliases)) if len(aliases) == 1 else "")
        return instrument_name(row, under_symbol=symbol)

    def position(self, row):
        return self.labels.get(row.get("id")) or self._base_name(row)

    def trade(self, row):
        if row.get("security_type") not in {"OPT", "FOP"}:
            return instrument_name(row)
        position = self.positions.get(row.get("position_id"))
        return self.position(position) if position and position.get("security_type") == row.get("security_type") else instrument_name(row)
