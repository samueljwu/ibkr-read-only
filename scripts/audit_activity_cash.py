"""Reconcile an IBKR Activity Statement CSV with local executions, cash, and NAV.

Read-only: the report, SQLite database, and any broker account are never changed.
Usage: python -m scripts.audit_activity_cash path/to/activity.csv
The JSON result is written beside the selected portfolio under reports/.
"""

import argparse
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from app.platform.paths import local_config_path

CENT = Decimal("0.01")


def selected_database():
    config = local_config_path()
    if not config.is_file():
        raise FileNotFoundError("No selected portfolio; pass --database")
    directory = json.loads(config.read_text(encoding="utf-8")).get("data_directory")
    if not directory:
        raise ValueError("Selected portfolio directory is missing")
    database = Path(directory) / "portfolio.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Selected portfolio database is missing: {database}")
    return database


def amount(value):
    return Decimal(str(value or 0))


def close(actual, expected, label, tolerance=CENT):
    if abs(actual - expected) > tolerance:
        raise ValueError(f"{label}: {actual} differs from {expected}")


def statement_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.reader(source))


def section(rows, name, kind="Data"):
    return [row for row in rows if len(row) > 1 and row[0] == name and row[1] == kind]


def one_value(rows, section_name, field):
    matches = [row[3] for row in section(rows, section_name) if row[2] == field]
    if len(matches) != 1:
        raise ValueError(f"Expected one {section_name}/{field} field")
    return matches[0]


def cash_values(rows, currency):
    values = {}
    for row in section(rows, "Cash Report"):
        if row[3] != currency:
            continue
        if row[2] in values:
            raise ValueError(f"Duplicate Cash Report/{currency}/{row[2]}")
        values[row[2]] = amount(row[4])
    return values


def local_usd_executions(connection, first_day, last_day):
    sales = purchases = commissions = Decimal(0)
    count = 0
    query = """SELECT * FROM trades WHERE trade_date BETWEEN ? AND ?
               AND portfolio_id=1 AND source!='IBKR legacy transfer'"""
    for trade in connection.execute(query, (first_day, last_day)):
        if trade["currency"] != "USD":
            continue  # AUD commissions and proceeds are translated in base summary.
        qty = amount(trade["quantity"])
        price = amount(trade["price"])
        if trade["security_type"] == "CASH":
            pair = trade["display_ticker"].upper().split(".")
            if len(pair) != 2 or "USD" not in pair:
                continue
            usd = qty if pair[0] == "USD" else qty * price
            cash_in = (trade["side"] == "BUY") if pair[0] == "USD" else (trade["side"] == "SELL")
        else:
            usd = qty * price * amount(trade["multiplier"])
            cash_in = trade["side"] == "SELL"
        if cash_in:
            sales += usd
        else:
            purchases += usd
        commissions += amount(trade["commission_usd"])
        count += 1
    return sales, purchases, commissions, count


def audit(report, database=None):
    database = Path(database) if database is not None else selected_database()
    rows = statement_rows(Path(report))
    account = one_value(rows, "Account Information", "Account")
    if one_value(rows, "Account Information", "Base Currency") != "USD":
        raise ValueError("Expected a USD base-currency report")
    period = one_value(rows, "Statement", "Period")
    first_text, last_text = period.split(" - ", 1)
    first = datetime.strptime(first_text, "%B %d, %Y").date()
    last = datetime.strptime(last_text, "%B %d, %Y").date()
    if first > last:
        raise ValueError("Report period is reversed")

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        saved_account = connection.execute(
            "SELECT value FROM settings WHERE key='ib_account_id'").fetchone()
        if not saved_account or saved_account[0] != account:
            raise ValueError("Statement account does not match the local portfolio")
        sales, purchases, trade_fees, count = local_usd_executions(
            connection, first.isoformat(), last.isoformat())
        local_stocks = {}
        quantity_sql = """SELECT p.display_ticker, p.security_type,
            SUM(CASE WHEN t.side='BUY' THEN t.quantity ELSE -t.quantity END) AS qty
            FROM trades t JOIN positions p ON p.id=t.position_id
            WHERE t.portfolio_id=1 AND t.trade_date<=?
            AND p.security_type='STK' GROUP BY p.id"""
        for position in connection.execute(quantity_sql, (last.isoformat(),)):
            qty = amount(position["qty"])
            if abs(qty) > Decimal("0.00000001"):
                local_stocks[position["display_ticker"]] = qty
    finally:
        connection.close()

    usd = cash_values(rows, "USD")
    base = cash_values(rows, "Base Currency Summary")
    refund_rows = [row for row in section(rows, "Commission Adjustments")
                   if row[2] == "USD"]
    refunds = sum((amount(row[5]) for row in refund_rows), Decimal(0))
    deposits = sum((amount(row[5]) for row in section(rows, "Deposits & Withdrawals")
                    if row[2] == "USD"), Decimal(0))
    interest_rows = [row for row in section(rows, "Interest") if row[2] == "USD"]
    paid_interest = sum((amount(row[5]) for row in interest_rows), Decimal(0))
    lending_interest = sum((amount(row[5]) for row in interest_rows
                            if "SYEP" in row[4]), Decimal(0))
    close(sales, usd["Trades (Sales)"], "USD sale proceeds")
    close(-purchases, usd["Trades (Purchase)"], "USD purchase payments")
    close(-trade_fees + refunds, usd["Commissions"], "USD commissions after refunds")
    close(deposits, usd["Deposits"], "USD deposits")
    close(paid_interest, usd["Broker Interest Paid and Received"], "USD interest")
    activity = sum((value for key, value in usd.items()
                    if key not in {"Starting Cash", "Ending Cash", "Ending Settled Cash",
                                   "Starting Collateral Value", "Net Securities Lent Activity",
                                   "Ending Collateral Value", "Net Cash Balance",
                                   "Net Settled Cash Balance"}), Decimal(0))
    nontrade = activity - usd["Trades (Sales)"] - usd["Trades (Purchase)"] - usd["Commissions"]

    nav = {row[2].strip(): amount(row[6]) for row in section(rows, "Net Asset Value")
           if len(row) > 6}
    close(sum((value for key, value in nav.items() if key != "Total"), Decimal(0)),
          nav["Total"], "NAV component sum")
    nav_change = {row[2]: amount(row[3]) for row in section(rows, "Change in NAV")}
    nav_movement = sum((value for key, value in nav_change.items()
                        if key not in {"Starting Value", "Ending Value"}), Decimal(0))
    close(nav_change["Starting Value"] + nav_movement, nav_change["Ending Value"],
          "NAV change bridge")
    close(nav_change["Ending Value"], nav["Total"], "NAV change vs component total")
    close(nav["Cash"], base["Ending Cash"], "NAV cash vs base cash report")
    close(nav["Collateral Value"] + nav["Securities Lent"], Decimal(0),
          "Lending collateral and lent securities")
    open_rows = [row for row in section(rows, "Open Positions") if row[2] == "Summary"]
    statement_stocks = {row[5]: amount(row[6]) for row in open_rows
                        if row[3] == "Stocks"}
    if local_stocks != statement_stocks:
        raise ValueError(f"Stock quantities differ: local={local_stocks}, statement={statement_stocks}")
    stocks = sum((amount(row[11]) for row in open_rows if row[3] == "Stocks"), Decimal(0))
    options = sum((amount(row[11]) for row in open_rows
                   if row[3] == "Equity and Index Options"), Decimal(0))
    close(stocks, nav["Stock"], "NAV stock value")
    close(options, nav["Options"], "NAV option value")
    lent_rows = [row for row in section(rows, "Stock Yield Enhancement Program Securities Lent")
                 if row[2] == "Stocks"]
    lent_collateral = sum((amount(row[8]) for row in lent_rows), Decimal(0))
    close(lent_collateral, nav["Collateral Value"], "Stock lending collateral")
    return {
        "account": account, "period": period, "local_execution_count": count,
        "usd_start_cash": str(usd["Starting Cash"]),
        "usd_trade_sales": str(usd["Trades (Sales)"]),
        "usd_trade_purchases": str(usd["Trades (Purchase)"]),
        "usd_trade_commissions_before_refunds": str(trade_fees),
        "usd_commission_refunds": str(refunds),
        "usd_other_cash_activity_excluding_deposits": str(nontrade - deposits),
        "usd_deposits": str(deposits), "usd_end_cash": str(usd["Ending Cash"]),
        "usd_interest_paid_including_lending": str(paid_interest),
        "usd_lending_interest_paid": str(lending_interest),
        "base_end_cash": str(base["Ending Cash"]),
        "stock_positions_reconciled": len(local_stocks),
        "stock_value": str(stocks), "option_value": str(options),
        "interest_accruals": str(nav["Interest Accruals"]),
        "dividend_accruals": str(nav["Dividend Accruals"]),
        "lent_collateral": str(lent_collateral),
        "nav": str(nav["Total"]),
        "nav_start": str(nav_change["Starting Value"]),
        "nav_mark_to_market": str(nav_change["Mark-to-Market"]),
        "nav_change_from_external_flows": str(nav_change["Deposits & Withdrawals"]),
        "checks": "All cash, NAV, trade, refund, and lending checks passed within USD 0.01"
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="IBKR Activity Statement CSV")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    database = args.database or selected_database()
    result = audit(args.report, database)
    output = database.parent / "reports" / "activity_cash_audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
