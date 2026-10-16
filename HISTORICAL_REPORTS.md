# Historical IBKR reports

Create **three separate Activity Flex Queries** in IBKR Client Portal under **Performance & Reports → Flex Queries**. Use XML output, select only the account entered in the app, include every currency and instrument, and leave symbol filters empty. The app requests dated reports directly from IBKR Flex Web Service; no report file is uploaded to another service. Enter each query ID and apply the account in **Settings**, then paste the token into the blank password field and click **Save Flex token**. The token is bound to that account in the OS credential store, and the field clears after saving. Repeating this replaces the saved token; the app shows configured status without revealing the token.

| Query / Settings field | Required sections and fields | Used for |
| --- | --- | --- |
| **Trades** / Trades Flex query ID | **Trades** at **Executions** detail: Account ID, Trade ID or IB Execution ID, Trade Date/Time, Buy/Sell, Quantity, Trade Price, Conid, Symbol, Asset Category, Currency, Multiplier, FX Rate to Base, IB Commission and Commission Currency, and Accrued Interest (`accruedInt`, bonds only). For options, also Local Symbol, Expiry, Put/Call, and Strike. Include **Corporate Actions**, **Option Exercises/Assignments/Expirations**, and **Transfers** with account, transaction ID, date, action/direction, asset class, contract ID, symbol, quantity, currency, cost or proceeds, multiplier and FX Rate to Base where offered. Include **Account Information → Currency** (the account base currency), or a USD base declaration or USD conversion evidence. | Completed executions, transferred opening lots, supported security events, and reconciliation review. Unsupported events stay flagged for review. A bond trade's `accruedInt` is the one-time interest settled with that trade (cash paid on a buy, cash received on a sell); it is distinct from the Flex **Interest Accruals** section, which reports running daily accrual balances and is not read by this app. |
| **Analytics** / Analytics Flex query ID | **NAV Summary in Base** with daily Report Date, Total NAV, Currency, and Cash; **Cash Transactions** with Report Date or Date/Time, Type, Amount, Currency, FX Rate to Base and Transaction ID; **Transfers** with date, direction, asset class, Position Amount in Base or cash amount, Currency, FX Rate to Base, and Transaction ID. Include **Account Information → Currency** (the account base currency), or a USD base declaration in each statement. | Completed daily NAV, cash balance, deposits/withdrawals, security transfers and flow-adjusted returns. Daily NAV rows and complete requested date coverage are needed. |
| **Cash** / Cash Flex query ID | **Cash Transactions**: Account ID, Currency, Report Date or Date/Time, Type, Amount, Description, Transaction ID, Conid, Symbol, and FX Rate to Base. Include every currency and instrument. | Dated nontrade cash activity. Conid or a unique Symbol links income and tax to a Return instrument; missing links remain unattributed. Unsupported transaction types stop that period's import. |

IBKR's field labels can vary by portal version. The importer validates report identity, dates, and financial fields before marking a period complete. Conid, Symbol, and transaction FX are optional in individual cash rows; absent identity leaves income unattributed, and absent FX can leave its USD return unavailable. The app currently expects a **USD base account** and one account per database. The Flex query must allow custom date requests and return the requested account and full date interval. Check the in-app sync status if IBKR cannot provide a section or older period. [IBKR Activity Flex reference](https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm)

The Returns subtab uses all recorded trades and their matched lots. Realized and unrealized bond P&L use clean prices and commissions. Net income includes attributable dividends, payments in lieu, bond coupons, bond accrued interest paid on purchases and received on sales, and withholding tax. Cash entries labeled "Accrued Interest Paid" or "Accrued Interest Received" are excluded because trade `accruedInt` already supplies those transfers. For an open bond, the full return also needs a current, instrument-level accrued-interest receivable, which the app does not currently import. A complete return requires imported Cash coverage from the first recorded trade through the last recorded trade or income posting for a closed position, or through yesterday for an open position. When Cash coverage or another component is incomplete, the Returns subtab adds the available USD components and marks the result ≈ partial. Hover over the figure to see the specific limitations, such as missing income or tax, unmatched sales, missing valuation or FX, or an estimated CSV opening price. If no valued lot or income component exists, the total remains unavailable. Reimport older Cash periods after adding Conid, Symbol, and FX Rate to Base to the Cash query; unchanged financial amounts may still gain instrument attribution. The percentage divides the displayed return by all recorded purchase cash outflows, including commissions and bond accrued interest paid; a partial return yields a partial percentage. It is neither annualized nor time-weighted and is unavailable for short positions or missing purchase cost. These return components are performance measures, not tax classifications.

NAV Summary in Base is represented by `EquitySummaryInBase` / `EquitySummaryByReportDateInBase` in XML. Analytics accepts daily reports that skip Saturday and Sunday only when NAV context establishes the adjacent Friday-to-Monday chain; omitted weekdays and unverified date edges stop validation. A multi-day statement must contain every weekday NAV date within its bounds. Wholly empty early windows are accepted only when no NAV is already stored for that window. It imports reported NAV dates only. Cash imports dated Cash Transactions and tracks complete requested windows without checking statement balances. Commission Adjustments are separate cash transactions, so Return uses the trade fee adjustment ledger to avoid counting them twice.

## Import and coverage

### Before Flex history: older CSV reports

For pre-2022 history, export a **PortfolioAnalyst since-inception Detailed CSV** and an
**Activity Statement CSV** covering account inception through a month end. In
**File → Import Data**, click **Import older CSV history** and select both files
together (Ctrl-click in the file picker). The app identifies each report by its
contents; selection order does not matter.
The app checks the account, USD base currency, statement NAV bridge, and shared
month-end NAV before saving. It also checks that PortfolioAnalyst's cash
allocation equals the statement's cash plus dividend accruals. Changed imports
create a local database backup. A conflicting report needs review instead of
silently replacing saved history.

To replace a conflicting older CSV pair, expand **Imported periods (all runs)**
in **Import Data** and choose **Remove older CSV history…**. After confirmation,
the app backs up the database and removes the saved older CSV monthly history,
statement evidence, closing stock quantity anchors, and reported since-inception
return for the current account. This clears all older CSV imports for that
account; it also removes estimated CSV opening lots, while leaving Flex history,
later trades, and the original CSV files in
place. Import the intended pair again afterward.

The **Analytics → Monthly history** tab shows monthly NAV and the monthly return
reported by PortfolioAnalyst. An inception month is a partial month. The
Activity Statement's dated trades, transfers, cash events, and closing positions
are retained as local source evidence. The statement's reconciled closing stock
quantities anchor the position ledger; later trades change those quantities.
When the statement includes a contract ID in Financial Instrument Information,
the importer uses it to link a closing position across ticker changes.
PortfolioAnalyst's reported since-inception return is shown separately from the
daily Flex return, with its report end date. The statement rows are not treated
as execution-level Flex trades or as complete daily NAV, cash, or flow coverage.
For a stock holding without an earlier priced lot, a positive reported CSV
**Close Price** at the statement end can create an estimated opening lot for the
uncovered quantity. This is a performance baseline on the statement date, not
the original purchase cost or tax basis; gains and losses before that date on
the estimated shares are excluded. Existing priced lots take precedence.
Missing or ambiguous identity, an earlier recorded sale, or missing FX can
leave the estimate unavailable.
Position-specific return can be partial when later sales cannot be matched or
Cash history has gaps. Hover over the marked subtotal for its limitations.
**Daily analysis is
unavailable for the imported 2021 period**, including daily P&L, daily extremes,
Sharpe ratio, and capital deployment across that period. The import does not
fill missing daily observations by interpolation. Protect the source CSVs,
database, and backups; SQLite and backups are currently unencrypted.

1. Start the app once, enter the account and three query IDs in Settings, and save the Flex token. Use a local, unsynchronized data folder if changing the default.
2. To fetch older history, choose **File → Import Data**, select **All reports**, **Trades**, **Analytics**, or **Cash**, and choose an ordered date range ending before today. Click **Import history**. Wait for any current sync to finish first; Flex sync and Settings changes are paused during the import. Keep the app open to let the import finish. Closing the import dialog lets the worker continue; reopen it from File to review progress and results. Closing the app interrupts the worker and saves an interrupted result; completed windows remain in the database, while the window in progress must be requested again. Review imported, unavailable, unrequested, and outside-history windows separately. A completed import with gaps does not establish full historical coverage. Reports are fetched and validated again: unchanged financial data is skipped, and supported corrections update existing records. Cash combines adjacent saved periods into the largest possible checks of at most 365 days. A check covers whole saved periods that intersect the selection, so its boundaries may extend beyond the selected dates and appear in the progress details. Uncovered Cash gaps also use windows of at most 365 days.

   For command-line use, close the app and run, for example:

   ```powershell
   python -m scripts.import_history --source all --from-date 2023-01-01 --to-date 2023-12-31
   ```

   You can use `--source trades`, `analytics`, or `cash` to import one report type. Use `--database C:\path\to\portfolio.sqlite3` only when you deliberately use a nondefault database. The UI always passes its current database to the worker and defaults to the most recent 30 completed days. The importer processes dated windows, newest first, and stops when IBKR reports an unavailable window; remaining windows are marked unrequested. Backups are created before applying changed reports; unchanged reports preserve stored financial rows and timestamps without another backup. Rechecked Cash periods replace their transactions and completion records in one transaction. Missing existing currencies or executions require review because omitted rows may reflect query filters. Invalid Cash transactions preserve previous data. Trades retain broker execution identities and revision precedence; validated same-revision Flex corrections can update economic fields, while manual trade and lot conflicts require review. Analytics replaces only its own dated NAV and flow scope (including replaced CSV flows); incomplete or empty corrections preserve existing data. Results distinguish Updated, Unchanged, and Needs review. These comparison safeguards apply to Import Data; Sync IBKR retains its existing replay behavior.
   To stop an import, click **Pause import**. The worker finishes the current report (or connected Cash group) before stopping, and the dialog shows the dates still unrequested. Click **Resume import** in the same app session to continue without requesting completed windows again. The app can be closed after the paused result appears. If the app is restarted, select the dates and import again; unchanged reports are compared before their saved data is updated.
3. The UI refreshes the views when the worker exits, including any periods saved before a pause or failure. Press **Sync IBKR** to catch up completed days through yesterday. Without an older import, the first automatic Trades and Cash sync starts with recent 30 day windows; Analytics starts at the beginning of the fourth previous calendar year. Review **Trades**, **Analytics**, **Cash**, and sync status for missing dates or events needing review.

Before requesting a history window, the importer moves a Saturday or Sunday end date to the preceding Friday. The omitted weekend remains explicitly unverified in the result; the app does not mark unrequested days as covered. A window containing only weekend dates is skipped and shown as a gap.

Each new override window stays within IBKR's 365-day maximum. Activity Flex history is generally available for the current year and four previous calendar years; older requested dates are recorded as outside history. See [IBKR date override limits](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex3.htm) and [Flex availability](https://www.ibkrguides.com/brokerportal/performanceandstatements/flex.htm). [Account Information Currency](https://www.ibkrguides.com/reportingreference/reportguide/account%20information%20activity%20flex.htm) identifies the base currency.

IBKR controls the oldest available Flex history; an unavailable period cannot be reconstructed by this app. Live TWS/Gateway data supplies current positions, prices, FX, account values and recent executions, but not complete historical NAV or cash transactions. The app stores imported records in local SQLite, keeps backups and diagnostic logs locally, and does not save downloaded XML reports by default. SQLite files and backups are currently unencrypted; protect the local Windows account and backup folder.

Import Data writes a rotating `logs/history-import.log` beside the selected database. It records the report source, requested dates, outcome, and IBKR response code and endpoint stage for unavailable or failed requests. It does not record report contents, account IDs, query IDs, tokens, or IBKR's free-form response text.
