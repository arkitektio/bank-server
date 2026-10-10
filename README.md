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

## Providers

Banks are reached through a **provider** the organization set up: a row holding a kind, its
settings and credentials, and the capabilities switched on for it. Only an admin of the
organization creates, changes or deletes one; any member links through it.

- `providerKinds` lists what this server can run, each kind with its capabilities
  (`TRANSACTIONS`, `BALANCES`, `HOLDINGS`, `PRICES`, `SCHEDULED_SYNC`), so a client offers them
  without knowing the kinds itself.
- `createEnableBankingProvider(input: {name, appId, privateKey, …})` takes the application id and
  the text of its `.pem`. Both are proven at Enable Banking (`GET /application`) before anything
  is stored; the key is kept Fernet-encrypted and never returned (`keyFingerprint` tells which
  one is in use). `createScalableProvider` needs no credentials.
- `updateProvider` renames, enables or disables any provider and sets its `capabilities`: a
  capability switched off is neither fetched nor stored, `SCHEDULED_SYNC` decides whether
  `sync_all_accounts` touches its accounts, `PRICES` whether its logins price securities.
- `deleteProvider` is refused while connections are active or pending.

A new kind is one module in `finance/providers/` implementing `ProviderBackend`
(`finance/providers/base.py`) and one entry in `finance/providers/registry.py`; its settings live
in the row's JSON, so it needs no migration.

## Linking a bank

The login is completed by the client; the server has no HTTP callback route.

1. `bankProviders` lists the organization's providers. For a kind with institutions
   (`kindInfo.hasInstitutions`), `bankInstitutions(provider, country: "AT")` lists its banks.
2. `startLink(input: {provider, institution?, country?, redirectUrl?})` returns an `AuthSession`.
   Send the user to its `openUrl`.
3. By the session's `finish`: `REDIRECT` — the bank redirects to `redirectUrl` with
   `?code=...&state=...`; the client catches it (or the user pastes the URL) and calls
   `completeAuth(input: {state, code})`. `POLL` — the client calls
   `completeAuth(input: {state})` every `interval` seconds until `status` is no longer `PENDING`.

The login follows the external auth flow contract every service shares: `completeAuth`,
`resumeAuth(state)`, `cancelAuth(state)` and `authSession(state)` all answer with the
`AuthSession` (`status` PENDING, DONE, FAILED, EXPIRED or CANCELLED; `step`; `errorCode` and
`errorMessage`; `result`, the linked connection). A state is only answered to the member who
started the login, in their organization. A settled login is answered again as it is, so
`completeAuth` can be called twice; a bank's refusal is passed on with
`completeAuth(input: {state, error, errorDescription})`.

The consented accounts appear under `bankAccounts`. Sync one with `syncAccount(id)`; an
organization can also have the hub's rekuest sync all of them unattended (see below).

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
- **Unattended syncs come from rekuest**: with `rekuest_hook` configured, the service offers
  the hub's rekuest a `sync_all_accounts` action. It syncs every active account of one
  organization, through the same lease and the same daily budget, and leaves
  `sync.scheduled_reserve` syncs of the day's budget for users. The action is only offered:
  nothing schedules it by default. Put it on a schedule or a trigger in rekuest, or run it
  there by hand to sync everything now.
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
- **Auth sessions**: see *Linking a bank*. A connection you started and have not finished
  carries `pendingAuth`; continue it with `resumeAuth(state)` or drop it with `cancelAuth(state)`.

## Importing a Finanzguru export

History from before a bank was linked can be imported from a Finanzguru transaction export
(`.xlsx`, or CSV with the same columns).

1. Upload the file with `requestBigfileUpload` (temporary S3 credentials; needs the
   `datalayer` block).
2. `createFinanzguruImport` previews it: which account each source account lands in, what
   applying would do, and the proposed category mappings (`importCategoryMappings`), which
   can be edited first with `setImportCategoryMappings`. Nothing is written into the accounts yet.
3. `applyStatementImport` writes it, and is safe to repeat. A row that pairs with a synced
   booking (`imports.match_window_days`) enriches that row instead of duplicating it; a
   later sync that finds an imported booking takes it over.

`python manage.py import_finanzguru export.xlsx --org <id or slug> [--apply]` runs the same
code path on a local file.

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

## Hub integration

Declared in [`bank_server/contract.py`](bank_server/contract.py):

- **Scopes**: `bank_read`, `bank_write`.
- **Needs**: rekuest 6 or newer, an instance key, `bigfile` storage, tokens issued by lok.

bank is known to the hub's rekuest in two separate ways:

- as a **service** (`_rekuest/service`): it hosts structures such as `@bank/bankaccount`,
  `@bank/transaction`, `@bank/category` and `@bank/merchant`
  ([`bank_server/service.py`](bank_server/service.py));
- as a **hook agent** (`_rekuest/hook`): it offers two actions, `sync_all_accounts` and
  `reembed_stale` ([`finance/scheduled.py`](finance/scheduled.py)). Both are only offered;
  whether and when they run is the organization's own automation in rekuest.

## Running

The image is `jhnnsrs/bank`. It has no default command, and starting it takes two steps:

```bash
arkitekt-service run migrate   # wait for the database, migrate, ensureadmin
arkitekt-service serve                          # serve on :80 (daphne), and nothing else
```

`arkitekt-service debug` does both in one go with Django's autoreloading server, for development.

It needs Postgres with pgvector and PostGIS
([`jhnnsrs/daten`](https://github.com/arkitektio/daten-server)) and Redis, plus S3 (RustFS)
for imports. GraphQL is served at `/graphql`, with the SDL at `/schema`.

## Development

```bash
uv sync
uv run --no-sync pytest       # brings up the stack below via dokker; needs Docker
uv run python manage.py validate_settings
```

The suite runs against a real stack, from `tests/integration/docker-compose.yaml`:

- Postgres (`jhnnsrs/daten:next`, override with `DATEN_IMAGE`) and RustFS;
- `fakebank`, a stand-in for the Enable Banking API. It serves real HTTP, verifies each
  request's RS256 application JWT, and is driven per test through its `/_admin` endpoints;
- `fakescalable`, `fakegeo` and `fakemarket`, stand-ins for Scalable, the geocoder and the
  price sources.

Nothing in the service is mocked.

Configuration is documented in [CONFIG.md](CONFIG.md). The Enable Banking private key is
mounted, never committed: `*.pem` is git-ignored.

## Releases

Releases are tags: a push to `main` cuts a stable version, a push to `next` a release
candidate. Each one publishes `jhnnsrs/bank` under its version (`X.Y.Z`, `X.Y`, `X`), plus
`latest` from `main` and `next` from `next`. The `version` in `pyproject.toml` is a
placeholder. Release notes are on
[GitHub Releases](https://github.com/arkitektio/bank-server/releases).
