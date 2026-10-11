# IBKR history setup and support

Create three **Activity Flex Queries**: **Trades**, **Analytics** and **Cash**. The app supports one **USD base account** per database.

## 1. Open Flex Queries

1. Sign in to IBKR Client Portal.
2. Click **Performance & Reports > Flex Queries**. Another route is **Menu > Reporting > Flex Queries**.
3. Find **Activity Flex Query** and click its **+** button.

[IBKR's query setup instructions](https://www.ibkrguides.com/clientportal/performanceandstatements/activityflex.htm)

## 2. Create Trades

1. Enter **Trades** in **Query Name**.
2. Click **Account Information**. Tick **Account ID**, **Currency** and **Date Opened**, then click **Save**. Leave the other fields unticked. **Currency** means your account's base currency.
3. Click **Trades**. Click **Select All** for the fields. Under **Level of Detail**, select **Executions** and **Closed Lots**. Click **Save**.
4. Click **Open Positions**. Click **Select All** for the fields, select both **Summary** and **Lot** detail, then **Save**.
5. Click **Corporate Actions > Select All > Save**.
6. Click **Option Exercises, Assignments and Expirations > Select All > Save**.
7. Click **Transfers > Select All > Save**.
8. Follow **Save each query** below before creating Analytics.

Use **Select All** inside these section pop-ups, not across the whole page. Other sections can stay unselected. The detail choices are separate from the field checkboxes.

[Account Information fields](https://www.ibkrguides.com/reportingreference/reportguide/account%20information%20activity%20flex.htm) | [Trades detail](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm) | [Open Positions detail](https://www.ibkrguides.com/reportingreference/reportguide/open%20positionsfq.htm)

## 3. Create Analytics

1. Return to **Flex Queries**, click **+** under **Activity Flex Query**, and name it **Analytics**.
2. Click **Account Information**. Tick **Account ID**, **Currency** and **Date Opened**, then **Save**.
3. Click **Net Asset Value (NAV) in Base > Select All > Save**. This includes **Report Date**, **Total** and **Cash**.
4. Click **Cash Transactions > Select All > Save**.
5. Click **Transfers > Select All > Save**.
6. Follow **Save each query** below. Set **Breakout by Day** to **Yes** for Analytics.

The NAV section may also be labelled **Net Asset Value (NAV) Summary in Base**. It supplies daily account values. [IBKR NAV fields](https://www.ibkrguides.com/reportingreference/reportguide/net%20asset%20value%20(nav)%20summary%20in%20base.htm)

## 4. Create Cash

1. Return to **Flex Queries**, click **+** under **Activity Flex Query**, and name it **Cash**.
2. Click **Account Information**. Tick **Account ID**, **Currency** and **Date Opened**, then **Save**.
3. Click **Cash Report > Select All > Save**.
4. Click **Cash Transactions > Select All > Save**. Keep every transaction type included if a type filter is offered.
5. Follow **Save each query** below. Set **Breakout by Day** to **No** for Cash.

Commission adjustments come through **Cash Transactions**; you do not need a separate Commission Details query. [IBKR explanation](https://www.interactivebrokers.com/en/?f=%2Fen%2Faccounts%2FreleaseNotes%2FStatement_Notes_Feb2013.php)

## 5. Save each query

Do this for **Trades**, **Analytics** and **Cash**:

1. Leave **Filters > Symbols** empty.
2. Under **Delivery Configuration**, click **Accounts > Add/Edit Accounts** and select only your app's account. Set **Format** to **XML** and **Period** to **Last 30 Calendar Days**. The app chooses its own dates when importing history.
3. Under **General Configuration**, use these settings:

   | Setting | Choose |
   | --- | --- |
   | Date Format | **yyyyMMdd** |
   | Time Format | **HHmmss** |
   | Date/Time Separator | **;** (semicolon) |
   | Include Currency Rates | **Yes** |
   | Display Account Alias in Place of Account ID | **No** |
   | Breakout by Day | **Analytics: Yes. Trades and Cash: No.** |

4. Click **Continue > Create**. Note the saved query's **Query ID**; reopen its details if needed.

[IBKR configuration reference](https://www.ibkrguides.com/reportingreference/reportguide/delivery%20configuration%20and%20general%20configuration.htm)

## 6. Connect the app

1. In Client Portal, find **Flex Web Service Configuration** and click its **gear**.
2. Turn **Flex Web Service** on. Copy the token shown. If it is expired, use **Generate A New Token**.
3. In the app, open **Settings > History & Flex**. Paste each Query ID into its matching **Trades**, **Analytics** or **Cash** field.
4. Click **Save settings**, then **Save Flex token**, and paste the token.
5. Click **Import history**. First try a short past range with known activity; then import the older range you need.

[IBKR token setup](https://www.ibkrguides.com/clientportal/performanceandstatements/flex-web-service.htm)

Use **Sync IBKR** for later updates. **Pause import** saves the current report before stopping; **Resume import** skips history already saved.

If dates are missing, open **Settings > History & Flex > Show all date ranges**. IBKR may not supply older periods. Use **Import older trades** for an older Activity Statement CSV; follow its instructions. This adds trades and verified transferred holdings only.

## Importer support contract v1

The app handles one selected account per portfolio database and never places
orders. An import finishing successfully means its validated report windows
were saved. It does **not** prove a complete account, full lifetime history or
tax return. Worker results explicitly record `complete_result=false` until
the account, query and lifecycle acceptance gates below are implemented and
verified. Old internal `complete` coverage/status values are scoped to that
report source/window; do not promote them to a whole-account claim.

### Defined scope

| Dimension | v1 scope | Outside the complete-result contract |
| --- | --- | --- |
| Account structure | One Individual (Single) account; customer Individual or Joint; Cash or ordinary Margin | Advisor/broker/institution masters, consolidated/model/linked multi-account reports, Trust/IRA/company/fund structures and Portfolio Margin |
| Environment | Live account, or separate paper account for testing | Paper tests do not prove live statement correctness; never mix their IDs |
| Base currency | Explicit USD evidence in broker reports | Non-USD, absent/conflicting base declarations; do not infer from account ID or traded currency |
| Instruments | Stocks/ETFs as STK; individually identified equity/index OPT contracts; ordinary BOND executions with verified multiplier/quote convention | CFDs, futures options, crypto, funds with unknown pricing, combos/bags, commodities, complex bonds/TIPS and unsupported corporate actions |
| Futures | FUT executions can be stored/previewed with explicit multiplier | Futures cash settlement/variation margin and complete performance are outside v1; a parsed execution does not imply full lifecycle support |
| Native currencies | Currency codes carried by the report; USD values require a verified positive FX rate for each relevant date/fee | No claim that all traded currencies are verified; missing FX/fees/taxes/basis remain provisional |
| Activity Flex | XML Activity reports; executions/closed lots/open lots and configured event sections; dated daily Analytics; whole-period Cash with successful arithmetic reconciliation | Trade Confirmation Flex, PDF, HTML, summarized/order-level rows, symbol-filtered or aliased/consolidated reports |
| Older recovery | English Activity Statement CSV with the expected repeated section headers and contract identity; individual STK/OPT/FUT/BOND executions and verified transferred stock lots | Localized/custom CSV; automatic continuous coverage; older Analytics/Cash reconstruction |

This is a narrow release support boundary, not certification of every row in
an otherwise supported report. Unproven short-sale opening basis, option exercise and
assignment, reverse splits, mergers/spinoffs, redemptions, transfers without
verified lots and other unsupported lifecycle events must stay in review.
The existing parser retains such records for investigation. Declared scope
does not bypass any account/date/amount/reconciliation checks.

Closed Lots first validate fee- and tax-inclusive basis against the current
opening execution. A narrowly observed stock-lot representation may omit
commission only when exact raw quantity, price, proceeds, cost, commission and
tax evidence proves the proportional difference; execution fees and taxes
remain in P&L. This is not a general cost-basis convention: [IBKR defines cost
as including commissions and taxes](https://www.ibkrguides.com/reportingreference/reportguide/financialinformation_fq.htm).
One proven 2-for-1 split can allocate each closing lot between its original
purchase and the linked zero-basis additional shares, retaining original
execution prices and costs. Negative Closed Lots can protect a verified short
opening SELL and later closing BUY on the exact contract, including expiry at
zero. Ambiguous, unsupported or conflicting evidence remains in review.

IBKR distinguishes Account Type, Customer Type and Account Capabilities.
The existing queries do not request or validate all three. Missing evidence
therefore blocks an overall completeness claim; never infer these fields from
an ID prefix. Before enabling `complete_result=true`, request the three fields,
validate their actual XML schema using broker fixtures, and persist per-report
provenance consistently across Trades, Analytics and Cash. The helper in
`app/imports/import_contract.py` assesses normalized explicit evidence only; it is not
a new parser and passing it alone cannot prove completeness.

### Time and report limits

Activity Flex availability is the current calendar year plus four previous
calendar years; on 10 October 2026 the lower bound is 1 January 2022. Each
requested `fd`/`td` range is at most 365 inclusive days, even in a leap year.
Automatic Trades, Analytics and Cash requests end on the previous Monday to Friday
calendar date strictly before today in America/New_York. Hong Kong midnight
therefore does not advance a request while New York is still on the same day.
This conservative application policy has no holiday calendar and does not
guarantee broker publication. IBKR describes Activity data through the previous business day in
its [Activity Flex configuration guidance](https://www.ibkrguides.com/clientportal/configure-flex-with-ai.htm).
Deferred weekend dates remain gaps until later reports cover them.
An explicit `today` supplied to a runner retains calendar-yesterday behavior,
including weekends. Explicit Import history ranges are not clipped to the
automatic cutoff. Missing local timezone data stops automatic planning with an
actionable error; explicit dated history remains available.

Account opening, query configuration, broker availability, errors and local 30,000,000-byte report
limits may narrow actual coverage further. A bounded split/retry can recover
smaller intervals; every unavailable, failed or unchecked interval stays a gap.
Never treat pre-account dates or unavailable reports as verified empty periods
without broker evidence.

CSV recovery has no app-imposed Flex cutoff but depends on statements the user
can obtain and their required fields. Imported executions are discrete records,
not continuous report coverage. A year with a few trades imported is not a
verified year of activity. Broker corrections require replay/reconciliation.

Settings > History & Flex > Saved history includes saved CSV trade dates and
verified transferred-holding snapshot dates in the Trades row, alongside Flex
report ranges. The CSV span runs from the earliest to the latest saved record;
the date tooltip and expanded details list the actual dates. It does not claim
that the statement's whole period was imported or fill gaps in Flex coverage.
Records with pending transaction tax still have saved dates and retain their
pending-cost warning. Pending transfers without saved holdings have no saved date.

The older-trades importer uses the unchanged English Activity Statement CSV.
For ordinary opening stock purchases, its existing Trades Basis can establish
purchase cost including commission and tax; closing or adjusted Basis cannot
establish transaction tax. Available independent cost evidence must agree.
Unknown transaction tax stays unknown, so affected costs and net P&L stay incomplete.
This follows IBKR's [opening versus closing Basis definition](https://www.ibkrguides.com/reportingreference/reportguide/trades_default.htm).

Transfers are verified against the statement and a complete eligible saved broker
lot set, including later partial sales reconstructed against one snapshot. The
importer never substitutes transfer market value or invents missing cost history.
Valid executions and verified transfers save atomically with unresolved transfers
as pending source records containing the statement rows, hash, period, portfolio,
quantities and reason. Pending-only statements can also save. History and import
dialogs show pending stocks and shares; holdings and affected P&L remain incomplete.
After importing available broker lot history, retry the same CSV: its pending
record completes once when evidence suffices. Quantity or basis contradictions,
overlapping transfers and edited saved holdings still block and roll back the import.

### Unavailable reports and retries

When a history import stops requesting a source after repeated unavailable reports,
the result is **History imported with gaps**. It identifies the affected sources
and dates and directs the user to check IBKR availability before retrying
**Import history**. **History import paused** and **Resume import** are reserved
for a pause requested by the user. Available reports remain saved in both cases.

Settings shows current record issues with their reasons and next steps. A historical
review flag stops appearing when complete saved reports cover its entire date
range; unresolved execution or lot records still appear even when coverage is saved.

An observed Flex 1003 at either request or retrieval starts a persistent six-hour
local cooldown for automatic attempts in that portfolio database, scoped to the
account and query ID. Six hours is an application backoff choice, not an IBKR
recovery interval. IBKR identifies 1003 as unavailable in its
[Flex error codes](https://www.interactivebrokers.com/docs/web-api/flex-web-service/error-codes);
[report generation guidance](https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/generate-the-report)
documents date overrides and pacing. The existing request pacing and generation
polling remain unchanged. Restarting a worker, changing the requested dates or
using Sync IBKR does not bypass it. Skipped attempts perform no credential loading,
backup or broker request and do not extend the deadline. Successful older slices
or other queries do not clear it. Invalid saved cooldown state becomes one
bounded six-hour deferral instead of an indefinite pause.

The current invocation retains its existing bounded splitting/retry budget and
saves validated partial success. Use **Import history** with explicit dates for
an immediate intentional retry; history keeps its existing recovery budgets and
records any subsequent 1003. Expiry allows another bounded automatic attempt,
but neither local midnight nor expiry proves broker availability. Configuration
errors and 1025 still stop immediately and remain paused until an explicit retry.
Every unavailable or unverified interval remains a gap, never verified empty
history. The observed live 1025 restriction remains unresolved; this prevention
change makes no claim to remove or recover from a broker restriction.

### Conditions for a future complete result

All of these must be demonstrated for a clearly labelled account/source/date
scope: supported explicit account/base provenance; unfiltered expected sections
and execution-level data; continuous actual report coverage (including proven
empty intervals); no unsupported/skipped instruments/events; all original
opening holdings and balances; linked purchase/closed lots and fees/taxes/FX;
cash and holdings reconciliation; and no review/pending/failed/unverified rows.
Full lifetime performance additionally needs proven account-opening history.
Trades-only success cannot prove Analytics or Cash completeness. Existing
legacy rows without this evidence do not gain it through schema migration.

Tests reject unsupported normalized scope, rollover of Flex availability,
unknown instrument ingestion and a stale `complete_result=true` worker flag.
Existing wrong-account/base/date, partial report, gap, replay and reconciliation
tests remain required. No live broker request was used for this change.

### Calculation check details

The Summary warning row and Cash tab now open the same compact **Details**
view. **Verified**, **Difference**, **Partial**, and **Unavailable** apply to
the displayed account, portfolio, currency and dated comparison. Expand a cash
check for its component amounts; earlier saved periods and individual holdings
are collapsed by default. Missing prices, FX, prior closes, fees, taxes and lot
basis are identified by holding or lot. Existing subtotal exclusions remain.

**Independent cash** starts from the broker opening balance and calculates
sales, purchases, both FX conversion legs, original signed native commissions,
dated refunds, execution taxes and non-trade cash movements from the saved local
ledger. It compares each component and the closing balance with the matching
Cash Report, using Decimal and a one-cent tolerance per native currency.
Component errors remain differences even when their closing effects cancel.
Detailed transaction tax is counted once, not again as a non-trade movement.
Canonical executions and their accepted correction prevent Flex/TWS replay
from counting a trade twice; ambiguous cross-source FX conversions require
review. Supported older CSV FX rows are retained as cash evidence without
creating security holdings or lots.

**Statement arithmetic** checks the broker report against its own transactions;
it does not establish agreement with the independent ledger. Missing execution
windows (including unproven empty periods), original native fee/tax/proceeds
fields, unresolved events, unlinked refunds or missing report accounting-basis
evidence keep the independent check Partial. To repair older saved evidence or
retrieve broker corrections, open **Settings > Import history**, select the
affected source and dates, and enable **Recheck saved reports**. Select `all` when
both execution and Cash evidence need repair. This fetches fresh reports instead
of reusing the local report cache; the default option still skips completed dates.
Cash rechecks replay each original saved period in full and import intervening
gaps. A range cutting a saved Cash period is rejected with its required dates
before any source is changed. Pause and Resume retain the option and skip windows
already rechecked in that run. Reports remain subject to IBKR historical
availability and the existing validation/replay checks. Refunds use their actual posting
dates and currencies. Splits and verified transferred opening lots are noncash.
Unsupported lifecycle or margin cash cannot silently become a zero movement.

Latest failed, unavailable, paused or unfinished imports keep the overall cash
summary Partial while preserving explicitly dated checks for saved intervals.
Each holding retains its own broker snapshot date; a live update for another
holding does not refresh it. Quote changes do not rebuild cash comparisons.
Full NAV, valuation and P&L broker verification remains Partial or Unavailable
because the retained live account totals do not supply compatible dated
valuation or P&L snapshots. These checks do not prove complete lifetime history
or unfiltered account reporting.

Calculation-check explanations group missing report details, unverified lot
matches, unsupported events and inconsistent amounts separately. They show
affected securities and dates only when saved source records establish them.
A pending lot record alone does not prove that an earlier statement is missing.
An acquisition date is identified as outside Trades coverage only when its
opening trade is absent and that date has no complete saved execution window.
**Technical details** and **Copy diagnostics** retain the original source IDs
and reasons without listing them in the main explanation. Reimporting does not
add app support for unknown option exercise, assignment or settlement cash.
An expiration already linked to an unchanged option trade with explicit zero
price and proceeds is accounted for; ambiguous or changed links remain Partial.

### Primary references

- [IBKR Cash Report trade-date and settled-cash fields](https://www.ibkrguides.com/reportingreference/reportguide/cashreport.htm)
- [IBKR account information fields](https://www.ibkrguides.com/reportingreference/reportguide/account%20information%20activity%20flex.htm)
- [Execution and lot detail fields](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)
- [Flex historical availability](https://www.ibkrguides.com/clientportal/performanceandstatements/flex.htm)
- [Flex date override limit](https://www.ibkrguides.com/advisorportal/ug/flex3.htm)
