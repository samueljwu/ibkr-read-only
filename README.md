# Custom IB Monitor

A Windows desktop monitor for one Interactive Brokers account. It reads live account and market data through your local TWS or IB Gateway and builds historical trades, performance, and cash views from your own Activity Flex queries. It does not place orders.

## Start

Install Python 3.11 or newer, then run in PowerShell:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The app starts in Mock mode. In TWS or IB Gateway, enable socket API access and **Read-Only API**, then sign in. In **Settings**, choose **Use IBKR**, enter the host (`127.0.0.1` for the same computer), the socket port shown in TWS/Gateway, a client ID, and your account ID; apply the settings. No IBKR login password is entered into this app. [IBKR API setup](https://interactivebrokers.github.io/tws-api/initial_setup.html)

For history, create three account-scoped **Activity Flex Queries** with XML output in IBKR Client Portal: Trades, Analytics, and Cash. Enter their query IDs and apply the account in **Settings**. Enable Flex Web Service, generate a token, paste it into the blank password field in Settings, and click **Save Flex token**. This saves or replaces the token for that account in Windows Credential Manager; only its configured status is shown. Press **Sync IBKR** for catch-up, or choose **File → Import Data** to select a report source and dates ending before today. The import dialog shows progress, results, and gaps while the worker runs. See [Historical reports](HISTORICAL_REPORTS.md) for the required sections, fields, and coverage. [IBKR Flex Queries](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex.htm) · [Flex Web Service](https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm)

Use the pause button beside **Sync IBKR** to pause syncing across the app; the button changes to a resume icon while paused. Pausing disconnects live IBKR updates, lets an active Flex sync finish, and pauses a running Flex history import after its current report. Pending sync work continues when you resume. The pause lasts until you resume or close the app.

## What it shows

| View | Purpose |
| --- | --- |
| Overview | Account totals, trade-derived holdings, prices, P&L, and differences from broker positions. |
| Trades and Lots | Executions, matched sale lots, open lots, and realized P&L. |
| Rebalance | Saved target weights, buy/sell size estimates, and price scenarios; no order submission. |
| Analytics | Historical NAV, flow-adjusted performance, cumulative position returns with attributable income and tax, capital deployment, cash ratio, and current market or delta-adjusted exposure. |
| Cash | Imported dated cash activity and totals by currency. |

For pre-2022 history, **File → Import Data → Import older CSV history** accepts a
PortfolioAnalyst since-inception CSV and a matching Activity Statement CSV.
**Analytics → Monthly history** displays the reported monthly NAV and returns.
Daily analysis for the imported 2021 period remains unavailable; statement
activity is retained as source evidence rather than daily or execution coverage.
Reconciled statement closing stock quantities anchor the position ledger, and
the reported since-inception account return is shown with its report end date.
Where the Activity Statement reports a closing price but no earlier priced lot,
the app uses that price as an estimated opening value and labels the resulting
return as starting on the statement date. It is not original purchase cost or
tax basis.
See [Historical reports](HISTORICAL_REPORTS.md) for the checks and limitations.

Analytics adjusted performance follows [IBKR's published account TWR formula](https://www.ibkrguides.com/portfolioanalyst/pa-white-papers-hk.pdf): daily return is `ending NAV / (previous NAV + external flow) - 1`, and daily returns are compounded. The white paper's worked example uses this formula, although its text also describes account flows as occurring at the end of the day. The app follows the numeric formula and example; this does not guarantee an exact match to every IBKR report.

Data, settings, reports, backups, and diagnostic logs stay on your computer. Source runs use `%LOCALAPPDATA%\CustomIBMonitor\dev\data`; packaged runs use `%LOCALAPPDATA%\CustomIBMonitor\prod\data`. Each portfolio database lives in its own `yyyymmdd_` plus 12-character random key folder. New portfolio databases and new backups use SQLCipher encryption. The database key is stored in Windows Credential Manager; save the recovery code shown on first launch outside the data and backup folders. You need that code to open an encrypted database or backup on another Windows profile. Use **File → Show Database Recovery Code** to view it again while the original profile is available.

Existing plaintext databases remain readable until you select **File → Encrypt Database**. The app creates and verifies an encrypted copy in a hidden worker, then retains the prior plaintext database beside it. Verify an encrypted backup and recovery before removing that old copy. Earlier backups, reports, workbook exports, and diagnostic logs may still contain readable financial data. An encrypted database from an unrelated older build may require a readable SQLite export if its key or SQLCipher settings are incompatible. Choose a local, unsynchronized data and backup folder in Settings if you change the defaults. Keep real reports, databases, exports, and credentials out of Git. Security design and release checks remain incomplete; see [SECURITY_DESIGN.md](SECURITY_DESIGN.md).
