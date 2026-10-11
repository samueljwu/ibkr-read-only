# Custom IB Monitor

A read-only desktop app for an Interactive Brokers account with a USD base currency. Track your portfolio and plan rebalancing scenarios. **The app cannot place orders.**

- **Portfolio:** holdings, live prices, trades, lots, and P&L.
- **Analytics:** performance, S&P 500 comparison, returns, exposure, and cash.
- **Planning:** rebalancing targets and upside/downside scenarios.

Export to Excel, create backups, and customize tables and themes. Press **F1** for the in-app guide.

Windows v1.0.1 and later encrypt local databases and database backups in stable and Dev builds. Use **File → Export portable recovery backup** and save its recovery code separately to recover after losing your Windows profile. Excel exports and report caches remain unencrypted. The published v1.0.0 installer still uses plaintext databases.

The v1.0.2 patch improves Activity Statement imports, broker lot matching, history rechecks, and cash reconciliation, and adds resizable dialogs.

## Quick start

On Windows x64, install Python 3.11, then run from this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Source runs open as **Portfolio Monitor Dev**, starting with an empty portfolio in Demo mode. Dev uses separate data and credentials from installed stable builds; existing portfolios are not loaded automatically.

## Connect IBKR

1. In TWS or IB Gateway, enable socket API access and **Read-Only API**.
2. In the app's **Settings**, select **IBKR** and enter your account, host, API port, and an unused client ID. Save settings.
3. Follow the [history setup guide](HISTORICAL_REPORTS.md) to create Trades, Analytics, and Cash Flex Queries and enable Flex Web Service.
4. Enter the query IDs under **Settings → History & Flex**, save settings, and choose **Save Flex token**.
5. Click **Sync IBKR** for updates or **Import history** for earlier records.

No IBKR login password is required. Live prices depend on your market-data permissions; delayed and cached quotes are labelled.

## More

- [Development, testing, and Windows releases](DEVELOPMENT.md)
- [Security and credential handling](SECURITY_DESIGN.md)
- Installed Windows builds: **Help → Check for updates…**
