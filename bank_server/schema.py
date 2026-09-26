"""The GraphQL schema for the bank service.

Every field requires an authenticated caller (``AuthExtension``) and every read is limited to
the caller's active organization (``OrgScoped`` types, ``get_for_org`` lookups). Any member
of an organization may link, sync, categorize and budget.

* ``AuthentikateExtension`` — authenticates the request from its bearer token
  and exposes the user/organization/client on ``info.context.request``.
* ``KoherentExtension`` — attributes every model write in a request to that
  identity (provenance).
* ``DjangoOptimizerExtension`` — batches/prefetches ORM access to avoid N+1s.
"""

import strawberry
import strawberry_django
from django.conf import settings
from strawberry.schema.config import StrawberryConfig
from authentikate.strawberry.directives import AuthExtension, AuthSubscribeExtension
from authentikate.strawberry.extension import AuthentikateExtension
from koherent.strawberry.extension import KoherentExtension
from strawberry_django.optimizer import DjangoOptimizerExtension

from bank_server.logs import QuietErrorsSchema
from datalayer import mutations as datalayer_mutations
from datalayer import scalars as datalayer_scalars
from datalayer.datalayer import DatalayerConfig
from finance import types
from finance.types import imports as import_types
from finance.types import insights as insight_types
from finance.types import prices as price_types
from finance.graphql import mutations, queries, subscriptions


def field(**kwargs):  # noqa: ANN201
    """A query field that requires authentication."""
    return strawberry_django.field(extensions=[AuthExtension()], **kwargs)


def mutation(**kwargs):  # noqa: ANN201
    """A mutation that requires authentication."""
    return strawberry_django.mutation(extensions=[AuthExtension()], **kwargs)


def upload_mutation(**kwargs):  # noqa: ANN201
    """A mutation that hands out (or finishes) upload credentials: gated by ``datalayer.upload_roles``.

    Holding any one of the configured roles is enough. Read once, at schema build -- a change to
    the config takes a restart, like every other setting.
    """
    roles = DatalayerConfig(**getattr(settings, "DATALAYER", {})).upload_roles
    return strawberry_django.mutation(extensions=[AuthExtension(any_role_of=roles)], **kwargs)


def subscription(**kwargs):  # noqa: ANN201
    """A subscription that requires authentication."""
    return strawberry.subscription(extensions=[AuthSubscribeExtension()], **kwargs)


@strawberry.type
class Query:
    """The root query type."""

    bank_connections: list[types.BankConnection] = field(description="The organization's bank connections.")
    bank_connection: types.BankConnection = field(resolver=queries.bank_connection, description="A bank connection by id.")
    bank_accounts: list[types.BankAccount] = field(description="The organization's bank accounts.")
    bank_account: types.BankAccount = field(resolver=queries.bank_account, description="A bank account by id.")
    transactions: list[types.Transaction] = field(description="Transactions across the organization's accounts (paginated, filterable, orderable).")
    transaction: types.Transaction = field(resolver=queries.transaction, description="A transaction by id.")
    # Imports
    statement_imports: list[import_types.StatementImport] = field(description="The organization's statement imports (uploaded exports), newest first.")
    statement_import: import_types.StatementImport = field(resolver=queries.statement_import, description="A statement import by id.")
    import_category_mappings: list[import_types.ImportCategoryMapping] = field(description="Which category each imported category pair means.")
    merchants: list[types.Merchant] = field(description="The organization's merchants (paginated, filterable — e.g. `near` a point).")
    merchant: types.Merchant = field(resolver=queries.merchant, description="A merchant by id.")
    merchant_rules: list[types.MerchantRule] = field(description="The organization's merchant rules.")
    merchant_locations: list[types.MerchantLocation] = field(description="Merchant locations (paginated, filterable — `near`, `unlocated`).")
    merchant_location: types.MerchantLocation = field(resolver=queries.merchant_location, description="A merchant location by id.")
    merchant_locations_geojson: types.MerchantLocationFeatureCollection = field(resolver=queries.merchant_locations_geojson, description="Located merchant places as a typed GeoJSON FeatureCollection (valid GeoJSON as returned — hand it to a map renderer); same filters as `merchantLocations`, e.g. `within` a viewport.")
    merchant_candidates: list[types.MerchantCandidate] = field(resolver=queries.merchant_candidates, description="Recurring counterparties without a merchant.")
    spending_by_merchant: list[types.MerchantTotal] = field(resolver=queries.spending_by_merchant, description="Income, expense and net per merchant and currency.")
    geocode_search: list[types.GeocodeResult] = field(resolver=queries.geocode_search, description="Places matching an address or name (OpenStreetMap).")
    # Security prices (finance/prices): Scalable, Twelve Data, Yahoo behind one interface.
    security_listings: list[price_types.SecurityListing] = field(resolver=queries.security_listings, description="Which symbol prices each ISIN, per source.")
    security_prices: price_types.PriceSeries = field(resolver=queries.security_prices, description="An ISIN's stored daily closes.")
    security_quote: price_types.SecurityQuote = field(resolver=queries.security_quote, description="The latest price of an ISIN, fetched now.")
    position_performance: list[price_types.PositionPerformance] = field(resolver=queries.position_performance, description="How each depot position's price moved over a window.")
    # Insights: typed stats per view, computed on read (finance/insights).
    merchant_insights: insight_types.MerchantInsights = field(resolver=queries.merchant_insights, description="Stats for one merchant (by id or key).")
    location_insights: insight_types.LocationInsights = field(resolver=queries.location_insights, description="Stats for one merchant place.")
    area_insights: insight_types.AreaInsights = field(resolver=queries.area_insights, description="Stats for located places inside a circle or viewport.")
    spending_grid: insight_types.GridCellCollection = field(resolver=queries.spending_grid, description="Spending in a viewport binned into map cells (typed GeoJSON).")
    category_insights: insight_types.CategoryInsights = field(resolver=queries.category_insights, description="Stats for one category, children rolled up.")
    period_overview: insight_types.PeriodOverview = field(resolver=queries.period_overview, description="A dashboard for a window against a comparison window.")
    portfolio_insights: insight_types.PortfolioInsights = field(resolver=queries.portfolio_insights, description="The depot: value over time, allocation, positions, investment income.")
    recurring_insights: insight_types.RecurringInsights = field(resolver=queries.recurring_insights, description="Recurring payments: commitment, due, missed, price changes.")
    account_insights: insight_types.AccountInsights = field(resolver=queries.account_insights, description="Stats for one account.")
    suggest_categories: list[types.CategorySuggestion] = field(resolver=queries.suggest_categories, description="The categories a transaction most likely belongs to.")
    transactions_count: int = field(resolver=queries.transactions_count, description="How many transactions match the filters.")
    categories: list[types.Category] = field(description="The organization's categories.")
    category: types.Category = field(resolver=queries.category, description="A category by id.")
    category_rules: list[types.CategoryRule] = field(description="The organization's categorization rules.")
    category_rule: types.CategoryRule = field(resolver=queries.category_rule, description="A categorization rule by id.")
    budgets: list[types.Budget] = field(description="The organization's budgets.")
    budget: types.Budget = field(resolver=queries.budget, description="A budget by id.")
    recurring_payments: list[types.RecurringPayment] = field(description="Detected recurring payments.")
    recurring_payment: types.RecurringPayment = field(resolver=queries.recurring_payment, description="A recurring payment by id.")
    holdings: list[types.HoldingSnapshot] = field(resolver=queries.holdings, description="A depot's positions on a day (its latest synced day by default).")
    bank_institutions: list[types.Institution] = field(resolver=queries.bank_institutions, description="The banks that can be linked in a country.")

    spending_by_category: list[types.CategoryTotal] = field(resolver=queries.spending_by_category, description="Income, expense and net per category and currency.")
    cashflow: list[types.CashflowBucket] = field(resolver=queries.cashflow, description="Income, expense and net per month or week and currency.")
    top_counterparties: list[types.CounterpartyTotal] = field(resolver=queries.top_counterparties, description="Where the most money went, or came from.")
    balance_history: list[types.BalancePoint] = field(resolver=queries.balance_history, description="An account's end-of-day balance over a range.")
    budget_status: list[types.BudgetStatus] = field(resolver=queries.budget_status, description="Budgeted vs. spent for a month.")
    forecast: list[types.ForecastPoint] = field(resolver=queries.forecast, description="An account's expected balance over the coming days.")


@strawberry.type
class Mutation:
    """The root mutation type."""

    start_bank_link = mutation(resolver=mutations.start_bank_link, description="Start linking a bank; returns the auth session (finish: REDIRECT).")
    complete_bank_link = mutation(resolver=mutations.complete_bank_link, description="Finish linking a bank with the redirect's code and state.")
    revoke_bank_connection = mutation(resolver=mutations.revoke_bank_connection, description="Withdraw a bank consent; data is kept.")
    resume_link = mutation(resolver=mutations.resume_link, description="Get the auth session of a pending link you started again (to continue a login).")
    cancel_link = mutation(resolver=mutations.cancel_link, description="Delete a pending link you started.")
    start_scalable_link = mutation(resolver=mutations.start_scalable_link, description="Start linking Scalable Capital; returns the auth session (finish: POLL).")
    complete_scalable_link = mutation(resolver=mutations.complete_scalable_link, description="Advance a Scalable Capital link; call until the connection is ACTIVE.")
    sync_account = mutation(resolver=mutations.sync_account, description="Pull an account from the bank now.")
    sync_connection = mutation(resolver=mutations.sync_connection, description="Pull every account of a connection now.")

    create_merchant = mutation(resolver=mutations.create_merchant, description="Create a merchant (aliases from `fromTransactions` by default) and match it everywhere.")
    resolve_security_listings = mutation(resolver=mutations.resolve_security_listings, description="Find listings for ISINs (OpenFIGI + preferred exchanges).")
    pin_security_listing = mutation(resolver=mutations.pin_security_listing, description="Choose the symbol an ISIN is priced by, for one source.")
    refresh_security_prices = mutation(resolver=mutations.refresh_security_prices, description="Fetch and store daily closes from the enabled sources.")
    create_merchant_rule = mutation(resolver=mutations.create_merchant_rule, description="Create a rule mapping matching transactions to a merchant.")
    update_merchant_rule = mutation(resolver=mutations.update_merchant_rule, description="Change a merchant rule.")
    delete_merchant_rule = mutation(resolver=mutations.delete_merchant_rule, description="Delete a merchant rule.")
    upsert_merchant = mutation(resolver=mutations.upsert_merchant, description="Create a merchant, or update the one with this key (aliases are added).")
    update_merchant = mutation(resolver=mutations.update_merchant, description="Change a merchant; a new default category re-categorizes its transactions.")
    delete_merchant = mutation(resolver=mutations.delete_merchant, description="Delete a merchant; its transactions are categorized again.")
    merge_merchants = mutation(resolver=mutations.merge_merchants, description="Fold one merchant into another.")
    add_merchant_alias = mutation(resolver=mutations.add_merchant_alias, description="Teach a merchant another spelling.")
    remove_merchant_alias = mutation(resolver=mutations.remove_merchant_alias, description="Forget a merchant spelling.")
    create_merchant_location = mutation(resolver=mutations.create_merchant_location, description="Add a place to a merchant.")
    update_merchant_location = mutation(resolver=mutations.update_merchant_location, description="Correct a merchant place.")
    delete_merchant_location = mutation(resolver=mutations.delete_merchant_location, description="Delete a merchant place.")
    geocode_merchant_location = mutation(resolver=mutations.geocode_merchant_location, description="Look a place up (OpenStreetMap) and fill in its address and coordinates.")
    assign_merchant = mutation(resolver=mutations.assign_merchant, description="Link many transactions to a merchant (by id or key) and place (by id or store number), by hand; null hands them back to matching.")
    create_category = mutation(resolver=mutations.create_category, description="Create a category.")
    update_category = mutation(resolver=mutations.update_category, description="Change a category.")
    delete_category = mutation(resolver=mutations.delete_category, description="Delete a category (reassigning its transactions, or handing them back to rules and suggestions); `dryRun` reports what would happen.")
    seed_default_categories = mutation(resolver=mutations.seed_default_categories, description="Add the base categories this organization lacks; returns all categories.")
    sync_base_categories = mutation(resolver=mutations.sync_base_categories, description="Add base categories this organization does not have yet; returns the created ones.")
    restore_base_category = mutation(resolver=mutations.restore_base_category, description="Bring back a deleted base category.")
    create_category_rule = mutation(resolver=mutations.create_category_rule, description="Create a categorization rule.")
    update_category_rule = mutation(resolver=mutations.update_category_rule, description="Change a categorization rule.")
    delete_category_rule = mutation(resolver=mutations.delete_category_rule, description="Delete a categorization rule.")
    reapply_rules = mutation(resolver=mutations.reapply_rules, description="Run the rules over existing transactions.")

    categorize_transaction = mutation(resolver=mutations.categorize_transaction, description="Set or clear a transaction's category.")
    set_transaction_note = mutation(resolver=mutations.set_transaction_note, description="Set or clear a transaction's note.")
    mark_transfer = mutation(resolver=mutations.mark_transfer, description="Pin or un-pin a transaction as a transfer between own accounts.")
    categorize_transactions = mutation(resolver=mutations.categorize_transactions, description="Set or clear the category of many transactions at once.")
    mark_transfers = mutation(resolver=mutations.mark_transfers, description="Pin or un-pin many transactions as transfers at once.")

    create_budget = mutation(resolver=mutations.create_budget, description="Create a monthly budget.")
    update_budget = mutation(resolver=mutations.update_budget, description="Change a budget.")
    delete_budget = mutation(resolver=mutations.delete_budget, description="Delete a budget.")
    detect_recurring = mutation(resolver=mutations.detect_recurring, description="Detect recurring payments.")
    set_recurring_status = mutation(resolver=mutations.set_recurring_status, description="Confirm or ignore a recurring payment.")
    set_recurring_statuses = mutation(resolver=mutations.set_recurring_statuses, description="Confirm or ignore many recurring payments at once.")

    # Uploads (the vendored datalayer): request a grant, write the file to S3, finish, then import it.
    request_bigfile_upload = upload_mutation(resolver=datalayer_mutations.request_bigfile_upload, description="Request temporary S3 credentials to upload one file (e.g. a statement export to import).")
    finish_bigfile_upload = upload_mutation(resolver=datalayer_mutations.finish_bigfile_upload, description="Finalize a file upload after the client has written the object.")
    request_bigfile_access = mutation(resolver=datalayer_mutations.request_bigfile_access, description="Request temporary S3 read credentials for an uploaded file.")

    # Imports: preview an uploaded export, map its categories, apply it.
    create_finanzguru_import = mutation(resolver=mutations.create_finanzguru_import, description="Preview an uploaded Finanzguru export (nothing is written into the accounts yet).")
    apply_statement_import = mutation(resolver=mutations.apply_statement_import, description="Write a previewed import into the accounts (idempotent).")
    set_import_category_mappings = mutation(resolver=mutations.set_import_category_mappings, description="Set which category imported category pairs mean.")


@strawberry.type
class Subscription:
    """The root subscription type."""

    account_syncs = subscription(resolver=subscriptions.account_syncs, description="Events whenever one of the organization's accounts finished syncing.")


# A federation schema is required because the authentikate types (User,
# Organization, …) are federated entities carrying ``@key`` directives.
class Schema(QuietErrorsSchema, strawberry.federation.Schema):
    """strawberry.federation.Schema, logging expected resolver errors as one line and bugs with a traceback (see logs.py)."""


schema = Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    config=StrawberryConfig(scalar_map={**datalayer_scalars.SCALAR_MAP}),
    extensions=[
        DjangoOptimizerExtension,
        AuthentikateExtension,
        KoherentExtension,
    ],
)
