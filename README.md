<h1 align="center">IBKR Dashboard</h1>

<p align="center">
  <img src="app/assets/app_icon.png" alt="IBKR Dashboard app icon" width="180">
</p>

A desktop dashboard for one Interactive Brokers account. Track holdings, trades,
cash, and performance using live IBKR data and historical Flex reports.
**Read-only**: no orders are placed. Currently expects a USD base account.

## Install

Download an installer from the [latest release](https://github.com/samueljwu/ibkr-read-only/releases/latest).
The [Desktop bundles workflow](https://github.com/samueljwu/ibkr-read-only/actions/workflows/bundles.yml)
also provides ZIP artifacts from individual build runs. To install:

- **Windows x64:** run `IBKRDashboard-<version>-windows-x64-setup.exe`.
- **macOS (Apple silicon):** open `IBKRDashboard-<version>-macos-arm64.dmg` and drag the app to Applications.

Python is bundled. Windows builds are unsigned; macOS builds are unsigned and not notarized.

For later releases, use **Help → Check for updates…** in the installed app.

## Connect IBKR

The app starts in **Mock mode** so you can explore first.

1. In TWS or IB Gateway, enable the socket API and **Read-Only API**. Note the host and port.
2. In IBKR Client Portal, create three account-scoped XML Activity Flex queries: **Trades**, **Analytics**, and **Cash**. Enable Flex Web Service and generate a token.
3. In **Settings → Use IBKR**, enter the connection details, account ID, and query IDs. Click **Apply Settings**.
4. Paste the token and click **Save Flex token**, then **Sync IBKR**.

For query fields and setup details, open **Help → User guide (F1) → Start**.

## Explore

- **Overview:** account totals, holdings, prices, and P&L.
- **Trades & Lots:** executions, open lots, and sale-to-purchase matching.
- **Cash:** deposits, income, taxes, and fees.
- **Analytics:** NAV, returns, exposure, and buy/sell levels. Missing history can make results partial.
- **Rebalance:** plan target weights and price scenarios.

Try **Help → Take a tour** for a walkthrough.
Use **File → Import Data** for older Flex reports or paired PortfolioAnalyst
and Activity Statement CSVs. See the in-app guide for import limits.

## Your data

- Portfolios, backups, and logs stay local. 
- Packaged data: `%LOCALAPPDATA%\IBKRDashboard\prod\data` on Windows;
  `~/Library/Application Support/IBKRDashboard/prod/data` on macOS.
- Source runs use `dev/data` instead of `prod/data`.
- To choose a parent folder, use **Settings → Advanced settings → Move database…**. The app verifies and opens a copy there, then offers a restart;
  the original database remains in place. Change the backup folder separately in **Settings**.
- Flex tokens and database keys stay in the OS credential store.
- New databases and backups are encrypted.
- Save your first-launch recovery code outside the data and backup folders.

## Run from source

Requires Python 3.11+.

```text
python -m pip install -r requirements.txt
python main.py
```

[Build installers](packaging/README.md) · [License: GNU GPLv3](LICENSE)

## Screenshots

<table>
  <tr>
    <td align="center" valign="top" width="50%"><strong>Overview</strong><br><a href="docs/images/demo-overview.png"><img src="docs/images/demo-overview.png" alt="Overview tab with synthetic holdings" width="100%"></a></td>
    <td align="center" valign="top" width="50%"><strong>Trades</strong><br><a href="docs/images/demo-trades.png"><img src="docs/images/demo-trades.png" alt="Trades tab with synthetic executions" width="100%"></a></td>
  </tr>
  <tr>
    <td align="center" valign="top" width="50%"><strong>Lots</strong><br><a href="docs/images/demo-lots.png"><img src="docs/images/demo-lots.png" alt="Lots tab with synthetic lot matches" width="100%"></a></td>
    <td align="center" valign="top" width="50%"><strong>Cash</strong><br><a href="docs/images/demo-cash.png"><img src="docs/images/demo-cash.png" alt="Cash tab with synthetic transactions" width="100%"></a></td>
  </tr>
  <tr>
    <td align="center" valign="top" width="50%"><strong>Analytics</strong><br><a href="docs/images/demo-analytics.png"><img src="docs/images/demo-analytics.png" alt="Analytics tab with synthetic performance history" width="100%"></a></td>
    <td align="center" valign="top" width="50%"><strong>Rebalance</strong><br><a href="docs/images/demo-rebalance.png"><img src="docs/images/demo-rebalance.png" alt="Rebalance tab with synthetic holdings" width="100%"></a></td>
  </tr>
</table>

### Analytics sections

<table>
  <tr>
    <td align="center" valign="top" width="50%">
      <strong>P&amp;L</strong><br>
      <a href="docs/images/demo-analytics.png">
        <img src="docs/images/demo-analytics.png" width="100%">
      </a>
    </td>
    <td align="center" valign="top" width="50%">
      <strong>P&amp;L calendar</strong><br>
      <a href="docs/images/demo-analytics-calendar.png">
        <img src="docs/images/demo-analytics-calendar.png" width="100%">
      </a>
    </td>
  </tr>
  <tr>
    <td align="center" valign="top" width="50%">
      <strong>Returns</strong><br>
      <a href="docs/images/demo-analytics-returns.png">
        <img src="docs/images/demo-analytics-returns.png" width="100%">
      </a>
    </td>
    <td align="center" valign="top" width="50%">
      <strong>Capital deployment</strong><br>
      <a href="docs/images/demo-analytics-capital.png">
        <img src="docs/images/demo-analytics-capital.png" width="100%">
      </a>
    </td>
  </tr>
  <tr>
    <td align="center" valign="top" width="50%">
      <strong>Exposure</strong><br>
      <a href="docs/images/demo-analytics-exposure.png">
        <img src="docs/images/demo-analytics-exposure.png" width="100%">
      </a>
    </td>
    <td align="center" valign="top" width="50%">
      <strong>Buy / sell levels</strong><br>
      <a href="docs/images/demo-analytics-levels.png">
        <img src="docs/images/demo-analytics-levels.png" alt="Buy and sell markers on synthetic stock prices" width="100%">
      </a>
    </td>
  </tr>
</table>
