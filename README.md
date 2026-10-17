# Custom IB Monitor

A Windows desktop app for viewing one Interactive Brokers account. It combines live data from TWS or IB Gateway with historical Activity Flex reports. It does not place orders.

## IBKR setup

1. Sign in to TWS or IB Gateway and enable its socket API with **Read-Only API**. Note the host and socket port; use `127.0.0.1` when it runs on the same computer.
2. In IBKR Client Portal, create three account-scoped **Activity Flex Queries** with XML output: **Trades**, **Analytics**, and **Cash**. Enable Flex Web Service and generate a token. For the sections and fields to select, open **Help → User guide (F1) → Start** in the app. The app currently expects a USD base account.

## App setup

Install Python 3.11 or newer, then run in PowerShell:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The app starts in Mock mode. Open **Settings**, choose **Use IBKR**, enter the host, port, account ID, and three Flex query IDs, then click **Apply Settings**. The client ID is under **Advanced settings** if you need to change it. Paste the Flex Web Service token and click **Save Flex token**. The app never asks for your IBKR login password.

Press **Sync IBKR** to refresh live data and catch up on recent reports. Use **File → Import Data** to request older report dates and review coverage gaps. You can also import a matching PortfolioAnalyst and Activity Statement CSV pair for older monthly history; see **Help → User guide (F1) → Start** for the steps and limits.

For a guided introduction to the controls and tabs, choose **Help → Take a tour**. The tour is also available from the Help dialog.

## What it does

| Area | Purpose |
| --- | --- |
| Overview | Shows account totals, holdings, prices, P&L, and broker position differences. |
| Trades | Lists executions and their realized or open P&L. |
| Lots | Shows open and matched lots; lets you adjust sale-to-purchase matches. |
| Cash | Shows dated deposits, income, tax, fees, and other cash activity. |
| Analytics | Charts NAV, performance, capital deployment, returns, exposure, and imported monthly history. Missing history can make results partial. |
| Rebalance | Estimates trades for target weights and price scenarios; it is a planning worksheet. |

The Analytics **Buy / sell levels** chart checks Yahoo Finance split history using the selected stock's public ticker and verifies the returned currency. It combines those dates with recorded split corporate actions and shows buy and sell markers on the same split-adjusted price axis as IBKR daily closes. Marker size reflects recorded trade notional (quantity times original execution price). The table reuses the Trades tab's realized and open P&L in USD and percent; realized values repeat on both matched executions and should not be summed across trade rows. Original execution prices remain in the trade table and ledger. The result is cached for the day; if split history cannot be verified, the chart hides execution markers instead of showing uncertain levels. While connected to IBKR, **Load older prices** fills available daily closes back up to five years in one click; availability depends on IBKR's historical data for the stock.

## Where data is saved

Source runs use `%LOCALAPPDATA%\CustomIBMonitor\dev\data`; packaged runs use `%LOCALAPPDATA%\CustomIBMonitor\prod\data`. Each portfolio has its own folder containing the SQLite database, local backups, and diagnostic logs. You can change the data and backup folders in **Settings**. Workbook exports go wherever you choose, and imported CSV files remain where you selected them. Downloaded Flex XML is not saved by default.

Flex tokens and database keys are kept in the OS credential store (Keychain on macOS or Credential Manager on Windows). New databases and backups use SQLCipher; older databases, retained copies, and earlier backups or exports may still be readable. Save the recovery code shown on first launch outside the data and backup folders. Security and release checks remain incomplete; see [Security design](SECURITY_DESIGN.md).
