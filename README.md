# Custom IB Monitor

A desktop dashboard for one Interactive Brokers account. Track holdings, trades,
cash, and performance using live IBKR data and historical Flex reports.
Read-only: no orders are placed. Currently expects a USD base account.

## Install

Download an installer from a completed [Desktop bundles workflow run](https://github.com/samueljwu/custom-ib-monitor/actions/workflows/bundles.yml).
Extract the downloaded artifact, then:

- **Windows x64:** run `PortfolioMonitor-<version>-windows-x64-setup.exe`.
- **macOS (Apple silicon):** open `PortfolioMonitor-<version>-macos-arm64.dmg` and drag the app to Applications.

Python is bundled. Windows builds are unsigned; macOS builds are unsigned and not notarized.

## Connect IBKR

The app starts in **Mock mode** so you can explore first.

1. In TWS or IB Gateway, enable the socket API and **Read-Only API**. Note the host and port.
2. In IBKR Client Portal, create three account-scoped XML Activity Flex queries: **Trades**, **Analytics**, and **Cash**. Enable Flex Web Service and generate a token.
3. In **Settings → Use IBKR**, enter the connection details, account ID, and query IDs. Click **Apply Settings**.
4. Paste the token and click **Save Flex token**, then **Sync IBKR**.

For query fields and setup details, open **Help → User guide (F1) → Start**.
Try **Help → Take a tour** for a walkthrough.

## Explore

- **Overview:** account totals, holdings, prices, and P&L.
- **Trades & Lots:** executions, open lots, and sale-to-purchase matching.
- **Cash:** deposits, income, taxes, and fees.
- **Analytics:** NAV, returns, exposure, and buy/sell levels. Missing history can make results partial.
- **Rebalance:** plan target weights and price scenarios.

Use **File → Import Data** for older Flex reports or paired PortfolioAnalyst
and Activity Statement CSVs. See the in-app guide for import limits.

## Your data

- Portfolios, backups, and logs stay local. Change folders in **Settings**.
- Packaged data: `%LOCALAPPDATA%\CustomIBMonitor\prod\data` on Windows;
  `~/Library/Application Support/CustomIBMonitor/prod/data` on macOS.
- Source runs use `dev/data` instead of `prod/data`.
- Flex tokens and database keys stay in the OS credential store.
- New databases and backups are encrypted; older copies and exports may not be.
- Save your first-launch recovery code outside the data and backup folders.

## Run from source

Requires Python 3.11+.

```text
python -m pip install -r requirements.txt
python main.py
```

[Build installers](packaging/README.md) · [License: GNU GPLv3](LICENSE)
