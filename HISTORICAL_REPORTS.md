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
an otherwise supported report. Short-sale opening basis, option exercise and
assignment, reverse splits, mergers/spinoffs, redemptions, transfers without
verified lots and other unsupported lifecycle events must stay in review.
The existing parser retains such records for investigation. Declared scope
does not bypass any account/date/amount/reconciliation checks.

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
The app imports completed dates ending before today. Account opening, query
configuration, broker availability, errors and local 30,000,000-byte report
limits may narrow actual coverage further. A bounded split/retry can recover
smaller intervals; every unavailable, failed or unchecked interval stays a gap.
Never treat pre-account dates or unavailable reports as verified empty periods
without broker evidence.

CSV recovery has no app-imposed Flex cutoff but depends on statements the user
can obtain and their required fields. Imported executions are discrete records,
not continuous report coverage. A year with a few trades imported is not a
verified year of activity. Broker corrections require replay/reconciliation.

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

### Primary references

- [IBKR account information fields](https://www.ibkrguides.com/reportingreference/reportguide/account%20information%20activity%20flex.htm)
- [Execution and lot detail fields](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)
- [Flex historical availability](https://www.ibkrguides.com/clientportal/performanceandstatements/flex.htm)
- [Flex date override limit](https://www.ibkrguides.com/advisorportal/ug/flex3.htm)
