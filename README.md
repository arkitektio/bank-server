# Bank-Server

> [!WARNING]
> **Experimental, for personal use.** Bank is one of the most experimental Arkitekt services,
> and much of it was written quickly with an AI assistant ("vibecoded"). It works for its author's
> own setup, but it has not been reviewed or hardened the way the core services have. Expect
> breaking changes, and think twice before trusting it with data or credentials that matter.

A backend for financial planning and stats, following the design principles of the
[Arkitekt](https://arkitekt.live) framework. It links bank accounts through the
[Enable Banking](https://enablebanking.com) PSD2 API, keeps their transactions and balances
in Postgres, and serves categories, rules, stats, budgets, recurring-payment detection and
balance forecasts over GraphQL.

Everything belongs to an **organization**: every read and write is scoped to the caller's
active organization, and any member may link banks, sync, categorize and budget.

## Linking a bank

The consent flow is completed by the client; the server has no HTTP callback route.

1. `bankInstitutions(country: "AT")` lists the banks that can be linked.
2. `startBankLink(input: {aspspName, country, redirectUrl?})` returns `authUrl`. Send the user
   there. `redirectUrl` must be one of the server's registered `enablebanking.redirect_urls`.
3. After approval, the bank redirects to the redirect URL with `?code=...&state=...`. The
   client catches it (or the user pastes the URL) and calls
   `completeBankLink(input: {code, state})`. A state is only accepted in the organization that
   started the link, and only once.

The consented accounts appear under `bankAccounts`. Sync one immediately with `syncAccount(id)`;
the hub's rekuest also syncs every account on a schedule (see below).

When a consent expires, its connection turns `EXPIRED` (`needsReauth: true`). Linking the
same bank again re-attaches the same accounts: they are keyed by Enable Banking's
cross-session identification hash, so history, categories and notes stay.

## Syncing

- **Booked transactions are upserted**, not replaced. Each one is keyed by the bank's
  `entry_reference`, or else by a content hash plus an occurrence index, so two identical
  coffees on one day stay two rows. A re-sync updates only the bank's fields; `category`,
  `note` and `isTransfer` belong to users and are never overwritten.
- **Pending transactions are replaced on every sync**, because their content changes when
  they book. A note or category set on a *pending* transaction is lost when it books.
- **Transfers** between the organization's own accounts are detected from the counterparty
  IBAN and left out of the stats. Pin or un-pin one with `markTransfer`.
- After every sync, rules are applied to new and changed rows and recurring payments are
  re-detected for the account.
- Syncing a revoked connection fails with `CONNECTION_INACTIVE`; an expired consent fails with
  `CONSENT_EXPIRED` and marks the connection `needsReauth`.
- **Request/response only**: nothing loops in this service — no sweep, scheduler or worker.
  An account syncs when a client calls `syncAccount` / `syncConnection`, inside that request,
  sending the user's IP and user agent to the bank (the user is present).
- **Scheduled syncs come from rekuest**: with `rekuest_hook` configured, the hub's rekuest runs
  the `sync_all_accounts` action on a schedule (default every 12 h, `sync.scheduled_every_seconds`).
  It syncs every active account unattended, through the same lease and the same daily budget,
  and leaves `sync.scheduled_reserve` syncs of the day's budget for users. Change or pause the
  schedule in rekuest, or trigger it there to sync everything now.
- A sync takes a lease on the account first, so concurrent replicas — and a scheduled sync
  racing a user's — never sync the same account twice.
- **Sync budget**: each account may sync `daily_sync_limit` times per UTC day (Enable Banking:
  4, PSD2; Scalable: unlimited). `syncsRemainingToday` and `nextSyncAllowedAt` are computed on
  read; a sync before `nextSyncAllowedAt` fails with `RATE_LIMITED` without contacting the
  provider. A provider's own rate-limit answer blocks the account until its `Retry-After` (else
  the next UTC day).
- **Error codes**: every GraphQL error carries `extensions.code`, and accounts and connections
  carry `lastErrorCode` next to `lastError` — one `BankErrorCode` enum (`CONSENT_EXPIRED`,
  `RATE_LIMITED`, `MFA_REJECTED`, `CODE_EXPIRED`, `INVALID_STATE`, `BANK_UNAVAILABLE`, …).
- **Auth sessions**: `startBankLink` / `startScalableLink` return an `AuthSession` — what to
  open (`openUrl`) and how it finishes (`REDIRECT` → `completeBankLink`, `POLL` →
  `completeScalableLink` every `interval` s). `resumeLink` returns a pending session again,
  `cancelLink` deletes it (creator only).

## Categories

- **Base categories**: a two-level tree (Housing › Rent, Food › Groceries, …, Income ›
  Salary, Transfers & Investing › Investing) seeded on the first link, each with a stable
  `key` and an English name + bilingual description. Everything is editable — rename,
  describe, recolor, move, hide, delete, add your own. `syncBaseCategories` adds base
  categories new in an upgrade without touching edits; a deleted base category stays deleted
  until `restoreBaseCategory`. A subtree always has its root's kind.
- **Rules** (explicit, regex/substring) categorize first; **semantic** categorization fills
  the rest: similar transactions the organization already categorized, and category *terms*
  (name + each comma-separated description phrase — add a merchant to a description to
  teach the category). Confident guesses are assigned during sync as `SEMANTIC`; rules
  override them, a user overrides everything. `suggestCategories` / `suggestedCategories`
  explain each suggestion (`reason`, `neighbours`, `evidence`).
- **Semantic filters**: `transactions(filters: {search})` also matches by meaning
  ("supermarket" → what is in Groceries), `similarTo`, `nearCategory`, `categorySource`;
  `Transaction.similarTransactions`, `Category.candidates`.
- `deleteCategory(id, reassignTo, dryRun)` never strands transactions: they move to
  `reassignTo`, or go back to rules and suggestions.

## Merchants and places

- A **merchant** ("Spar", "Wiener Linien", the landlord) is recognized on bank lines by its
  **aliases** — normalized counterparty prefixes: "Spar Dankt 3418" has the key "spar"; the
  longest alias whose words start the key wins ("spar gourmet" over "spar"; "eurospar" and
  "sparkasse" match neither). `merchantCandidates` lists recurring counterparties without a
  merchant; `createMerchant(input: {name, fromTransactions})` turns one into a merchant.
- A **store number** on a line ("3418") becomes a **location** of the merchant (DISCOVERED,
  no address yet), so every later line from that store lands on it. Fill the address in
  (`updateMerchantLocation`) or look it up on OpenStreetMap (`geocodeMerchantLocation`,
  `geocodeSearch` — only when asked, inside that request).
- Locations are stored in **PostGIS** (a generated `geography` point, GiST-indexed; no
  GeoDjango/GDAL — see `finance/geo.py`): `merchants`, `merchantLocations` and `transactions`
  take `near: {latitude, longitude, radiusMeters}` and return `distanceMeters`, nearest first.
- A merchant's **default category** categorizes its transactions (source `MERCHANT`) after
  rules and before semantic guesses; never over a manual or imported category.
- **Merchant rules** work like category rules (counterparty / remittance / IBAN; contains,
  equals, regex; direction; amount range; priority) and map to a merchant, optionally pinning a
  place. Precedence: a manual link, then the first matching rule (RULE), then the longest
  alias (AUTO).
- Merchants are addressed by `id` or by their stable `key` (never changes on rename):
  `upsertMerchant(input: {key, …})` creates or updates; `assignMerchant(input: {transactions,
  merchant: {key}, location: {storeCode}})` links by hand (MANUAL; a new store number is created);
  `mergeMerchants(merchant, into)` folds duplicates; `spendingByMerchant` totals per merchant.
- A transaction's **embedding includes its merchant and place**: the bank line (normalized) plus
  the merchant's name, a short description, its hand-set default category (once — a light
  touch), the place's name and city (street and store number stay out). Kept in step inside the
  request that changes them (matching, `assignMerchant`, `createMerchant`, `mergeMerchants`,
  `updateMerchant`, `updateMerchantLocation`, `geocodeMerchantLocation`, deleting a merchant);
  `Transaction.semanticInput` shows the exact embedded text. Two unrelated card lines of the
  same market stall become neighbours once they share a merchant.
- `merchantLocationsGeojson(filters: {within: viewport})` returns a typed GeoJSON
  FeatureCollection for a map renderer.

## Security prices

Scalable (the organization's own login), Twelve Data (API key) and Yahoo (unofficial) behind one
interface (`finance/prices/sources.py`). Prices belong to a *listing* — VWCE on Xetra (EUR) and
VWRA in London (USD) are one ISIN at two prices — so each organization keeps a
`SecurityListing` per ISIN and source (resolved through OpenFIGI and `preferred_exchanges`, or
pinned with `pinSecurityListing`), and `SecurityPrice` rows are (source, symbol, day) — public
data shared by all organizations. A depot sync stores Scalable's last month; `refreshSecurityPrices`
backfills. Reads: `securityPrices(isin, priceSource)`, `securityQuote(isin)` (live),
`positionPerformance(dateFrom)`, and `portfolioInsights.valuationHistory` becomes daily
(quantity × close; quantities walked back through trades before the first snapshot).

## Insights (typed stats per view)

Each view is one query with a typed result (`finance/insights`, `finance/types/insights.py`),
computed on read from `window: StatsWindowInput {dateFrom, dateTo, accounts, includeTransfers,
includePending}` (the last 12 months by default). Money is Decimal and split per currency;
`compareTo: PREVIOUS_PERIOD | SAME_PERIOD_LAST_YEAR | NONE` adds `previous` and `changes`.

| Query | What it answers |
|---|---|
| `merchantInsights(merchant: {key})` | totals, tickets (avg/median/max/min), visits and spacing, monthly, weekdays, top places, share of its category |
| `locationInsights(location)` | the same for one place |
| `areaInsights(area: {near \| within})` | spend at places in a circle/viewport: top merchants, categories, places, monthly |
| `spendingGrid(within, cellMeters)` | a typed GeoJSON grid of spend for a map heatmap (PostGIS `ST_SnapToGrid`) |
| `categoryInsights(category)` | children rolled up: trend, monthly average, share of spending, children, top merchants/counterparties, budgets |
| `periodOverview(window)` | income/expense/net, savings rate, category and merchant movers, largest transactions, new merchants, calendar, recurring due |
| `portfolioInsights` | depot value vs. money put in over time, allocation, positions, cost basis, unrealized gain, investment income per year |
| `recurringInsights` | monthly commitment, by category, due soon, missed, price changes |
| `accountInsights(account)` | monthly flow, average/lowest/highest balance, largest in/out, top categories and merchants |

## Planning and stats

| Query / mutation | What it does |
|---|---|
| `spendingByCategory`, `cashflow`, `topCounterparties` | Aggregates over booked, non-transfer transactions, grouped by currency (no FX conversion). |
| `balanceHistory(account, dateFrom)` | End-of-day balances, anchored on the last reported balance and walked through the transactions. |
| `createCategoryRule`, `reapplyRules` | Rules categorize on import. The first active one by priority wins, and a manual category is never overridden. |
| `createBudget`, `budgetStatus(month)` | Monthly limits per category. Child categories count towards their parent's budget. |
| `detectRecurring`, `setRecurringStatus` | A heuristic finds weekly, monthly, quarterly and yearly payments. Confirm or ignore each one. |
| `forecast(account, horizonDays)` | The latest balance plus the confirmed recurring payments, projected forward. |

Amounts are `Decimal` scalars (strings on the wire), signed: negative is money out.

## Development

```bash
uv sync
uv run --no-sync pytest       # brings up postgres + a fake Enable Banking API via dokker
uv run python manage.py validate_settings
```

The suite runs against a real Postgres and `tests/integration/fakebank`, a stand-in for the
Enable Banking API. It serves real HTTP, verifies each request's RS256 application JWT, and is
driven per test through its `/_admin` endpoints. Nothing in the service is mocked.

Configuration is documented in [CONFIG.md](CONFIG.md). The Enable Banking private key is
mounted, never committed: `*.pem` is git-ignored.

### Connecting from an app

```bash
arkitekt-server service connect --url http://localhost:8000 --identifier live.arkitekt.bank
```
