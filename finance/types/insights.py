"""Typed results of the insight views (``finance.insights``).

Money is ``Decimal`` (a string on the wire) and always split per currency — there is no FX.
Only shares, ratios and rates are ``Float``. Expense amounts are positive (money out).
"""

import datetime
from decimal import Decimal

import strawberry
from asgiref.sync import sync_to_async
from django.db.models import QuerySet

from finance import enums
from finance.filters import BoundsInput, NearInput
from finance.types import (
    BankAccount,
    BudgetStatus,
    CashflowBucket,
    Category,
    CounterpartyTotal,
    CurrencyTotal,
    HoldingSnapshot,
    Merchant,
    MerchantLocation,
    PointGeometry,
    RecurringPayment,
    Transaction,
)

# --- inputs ------------------------------------------------------------------------------------------


@strawberry.input(
    description="Which transactions a view looks at: a booking-date range (the last 12 months by default) and optionally some accounts."
)
class StatsWindowInput:
    date_from: datetime.date | None = None
    date_to: datetime.date | None = strawberry.field(
        default=None, description="Inclusive; today by default."
    )
    accounts: list[strawberry.ID] | None = None
    include_transfers: bool = strawberry.field(
        default=False,
        description="Count transfers between own accounts (and investing) as spending/income.",
    )
    include_pending: bool = False


@strawberry.input(
    description="A map area: a circle (`near`) or a viewport (`within`) — give one."
)
class AreaInput:
    near: NearInput | None = None
    within: BoundsInput | None = None


# --- building blocks ---------------------------------------------------------------------------------


@strawberry.type(description="Money in and out in one currency; `expense` is positive.")
class MoneyTotals:
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int


@strawberry.type(description="How one metric moved against the comparison window.")
class Change:
    metric: enums.StatMetric
    currency: str
    current: Decimal
    previous: Decimal
    delta: Decimal = strawberry.field(description="current − previous.")
    ratio: float | None = strawberry.field(
        description="delta / previous; null when previous is zero."
    )


@strawberry.type(description="Totals for one ISO weekday (1 = Monday … 7 = Sunday).")
class WeekdayTotals:
    weekday: int
    currency: str
    income: Decimal
    expense: Decimal
    count: int


@strawberry.type(description="Totals for one day (only days with activity).")
class DayTotals:
    date: datetime.date
    currency: str
    income: Decimal
    expense: Decimal
    count: int


@strawberry.type(description="Size of the outgoing payments, in one currency.")
class TicketStats:
    currency: str
    average: Decimal
    median: Decimal
    largest: Decimal
    smallest: Decimal


@strawberry.type(description="A share (0–1) in one currency.")
class Share:
    currency: str
    share: float


@strawberry.type(
    description="When and how often: visits are distinct days with a transaction."
)
class VisitStats:
    visits: int
    first_visit: datetime.date | None
    last_visit: datetime.date | None
    days_since_last_visit: int | None
    average_days_between_visits: float | None
    visits_per_month: float | None = strawberry.field(
        description="Visits per 30.44 days of the window."
    )


@strawberry.type(
    description="A merchant's totals; `share` is its part of all expense in that currency."
)
class RankedMerchant:
    merchant: Merchant | None
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(
    description="A category's totals; `share` is its part of all expense in that currency."
)
class RankedCategory:
    category: Category | None
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(
    description="A place's totals; `share` is its part of all expense in that currency."
)
class RankedLocation:
    location: MerchantLocation | None
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(
    description="The window a view covered, and the one it was compared with."
)
class WindowInfo:
    start: datetime.date
    end: datetime.date
    previous_start: datetime.date | None
    previous_end: datetime.date | None


# --- views -------------------------------------------------------------------------------------------


@strawberry.type(description="Everything about one merchant over a window.")
class MerchantInsights:
    merchant: Merchant
    window: WindowInfo
    totals: list[MoneyTotals]
    previous: list[MoneyTotals]
    changes: list[Change]
    tickets: list[TicketStats]
    visits: VisitStats
    monthly: list[CashflowBucket]
    weekdays: list[WeekdayTotals]
    locations: list[RankedLocation]
    share_of_category: list[Share] = strawberry.field(
        description="Its part of the expense in its default category (and children)."
    )


@strawberry.type(description="Everything about one merchant place over a window.")
class LocationInsights:
    location: MerchantLocation
    window: WindowInfo
    totals: list[MoneyTotals]
    tickets: list[TicketStats]
    visits: VisitStats
    monthly: list[CashflowBucket]
    weekdays: list[WeekdayTotals]


@strawberry.type(description="Spending at located places inside a map area.")
class AreaInsights:
    window: WindowInfo
    totals: list[MoneyTotals]
    merchants: list[RankedMerchant]
    categories: list[RankedCategory]
    monthly: list[CashflowBucket]
    rows: strawberry.Private[QuerySet]
    limit: strawberry.Private[int]

    @strawberry.field(
        description="Its places by expense, largest first (ranked only when asked for)."
    )
    async def locations(self) -> list[RankedLocation]:
        from finance.insights.common import ranked_locations

        return await sync_to_async(ranked_locations)(self.rows, self.limit)


@strawberry.type(description="What a map styles a spending-grid cell by.")
class GridCellProperties:
    currency: str
    expense: Decimal
    income: Decimal
    count: int
    merchants: int
    locations: int


@strawberry.type(
    description="A GeoJSON Feature: one grid cell (its point is the cell's snapped center)."
)
class GridCell:
    type: str
    id: strawberry.ID
    geometry: PointGeometry
    properties: GridCellProperties


@strawberry.type(
    description="A GeoJSON FeatureCollection of spending-grid cells — valid GeoJSON as returned, and typed."
)
class GridCellCollection:
    type: str
    features: list[GridCell]
    bbox: list[float] | None
    cell_meters: float


@strawberry.type(
    description="Everything about one category (children rolled up) over a window."
)
class CategoryInsights:
    category: Category
    window: WindowInfo
    totals: list[MoneyTotals]
    previous: list[MoneyTotals]
    changes: list[Change]
    monthly: list[CashflowBucket]
    monthly_average: list[MoneyTotals] = strawberry.field(
        description="Totals divided by the months in the window (`count` is the number of months)."
    )
    share_of_spending: list[Share] = strawberry.field(
        description="Its part of all expense."
    )
    children: list[RankedCategory]
    top_merchants: list[RankedMerchant]
    budgets: list[BudgetStatus] = strawberry.field(
        description="This month's status of budgets on it."
    )
    tickets: list[TicketStats]
    rows: strawberry.Private[QuerySet]

    @strawberry.field(
        description="The counterparties it pays most, largest first (ranked only when asked for)."
    )
    async def top_counterparties(self) -> list[CounterpartyTotal]:
        from finance import stats

        totals = await sync_to_async(stats.top_counterparties)(self.rows, "OUT", 10)
        return [
            CounterpartyTotal(
                counterparty=x.counterparty,
                currency=x.currency,
                total=x.total,
                count=x.count,
            )
            for x in totals
        ]


@strawberry.type(
    description="A category that moved most against the comparison window."
)
class CategoryMove:
    category: Category | None
    change: Change


@strawberry.type(
    description="A merchant that moved most against the comparison window."
)
class MerchantMove:
    merchant: Merchant | None
    change: Change


@strawberry.type(
    description="A dashboard for a window, compared with the previous period or the same period last year."
)
class PeriodOverview:
    window: WindowInfo
    totals: list[MoneyTotals]
    previous: list[MoneyTotals]
    changes: list[Change]
    savings_rate: list[Share] = strawberry.field(
        description="(income − expense) / income per currency; can be negative."
    )
    category_movers: list[CategoryMove]
    merchant_movers: list[MerchantMove]
    largest_transactions: list[Transaction]
    new_merchants: list[Merchant] = strawberry.field(
        description="Merchants whose first transaction falls in the window."
    )
    daily: list[DayTotals]
    weekdays: list[WeekdayTotals]
    recurring_due: list[RecurringPayment] = strawberry.field(
        description="Confirmed recurring payments expected within the window."
    )


@strawberry.type(
    description="The depot on one day: its value, what was put in, and the gain."
)
class ValuationPoint:
    date: datetime.date
    currency: str
    valuation: Decimal
    invested: Decimal = strawberry.field(
        description="Net money put into securities up to this day (buys and savings plans minus sells)."
    )
    gain: Decimal


@strawberry.type(description="How much of the depot one security type is.")
class Allocation:
    security_type: str
    currency: str
    valuation: Decimal
    share: float


@strawberry.type(
    description="One security across every depot that holds it, in one currency (the same ETF in two depots is one position)."
)
class MergedPosition:
    isin: str
    name: str
    security_type: str | None
    currency: str
    quantity: Decimal = strawberry.field(description="Σ quantity over the depots.")
    price: Decimal | None = strawberry.field(
        description="The last quoted mid price per unit (of the largest holding that reports one)."
    )
    valuation: Decimal = strawberry.field(description="Σ valuation over the depots.")
    cost_basis: Decimal | None = strawberry.field(
        description="Σ quantity × FIFO price; null when any lot reports no buy-in (e.g. transferred in)."
    )
    fifo_price: Decimal | None = strawberry.field(
        description="costBasis / quantity: the quantity-weighted buy-in per unit; null with costBasis."
    )
    unrealized_gain: Decimal | None = strawberry.field(
        description="valuation − costBasis; null with costBasis."
    )
    weight: float = strawberry.field(
        description="Share of the portfolio's valuation in the same currency, 0..1."
    )
    depots: int = strawberry.field(description="How many depots hold it.")


@strawberry.type(description="Investment income or cost in one year.")
class InvestmentIncome:
    year: int
    kind: enums.TransactionKind
    currency: str
    amount: Decimal = strawberry.field(
        description="Signed: payouts positive, fees and taxes negative."
    )
    count: int


@strawberry.type(
    description="The depot: value over time, allocation, positions and investment income."
)
class PortfolioInsights:
    valuation_history: list[ValuationPoint]
    allocation: list[Allocation]
    positions: list[HoldingSnapshot] = strawberry.field(
        description="Each depot's positions on its latest day, one per depot and ISIN."
    )
    merged_positions: list[MergedPosition] = strawberry.field(
        description="The positions summed by ISIN and currency across depots, largest first. Exact (decimal) arithmetic."
    )
    valuation: list[CurrencyTotal]
    cost_basis: list[CurrencyTotal] = strawberry.field(
        description="Σ quantity × FIFO price of the current positions."
    )
    unrealized_gain: list[CurrencyTotal]
    income: list[InvestmentIncome]


@strawberry.type(description="What a category commits per month in recurring payments.")
class CategoryCommitment:
    category: Category | None
    currency: str
    monthly: Decimal = strawberry.field(
        description="Money per month (positive for payments out)."
    )
    count: int


@strawberry.type(description="A recurring payment whose amount changed.")
class PriceChange:
    recurring: RecurringPayment
    currency: str
    previous: Decimal
    current: Decimal
    delta: Decimal


@strawberry.type(
    description="Recurring payments: what they commit, what is due, what did not come, what got pricier."
)
class RecurringInsights:
    monthly_committed: list[MoneyTotals] = strawberry.field(
        description="Normalized to a month (× 30.44 / interval); `count` is the number of payments."
    )
    by_category: list[CategoryCommitment]
    due_soon: list[RecurringPayment] = strawberry.field(
        description="Expected within the next 30 days."
    )
    missed: list[RecurringPayment] = strawberry.field(
        description="Expected more than 3 days ago and not seen since."
    )
    price_changes: list[PriceChange]


@strawberry.type(description="An end-of-day balance.")
class BalanceExtreme:
    date: datetime.date
    currency: str
    amount: Decimal


@strawberry.type(description="Everything about one account over a window.")
class AccountInsights:
    account: BankAccount
    window: WindowInfo
    totals: list[MoneyTotals]
    monthly: list[CashflowBucket]
    average_balance: list[CurrencyTotal]
    lowest_balance: BalanceExtreme | None
    highest_balance: BalanceExtreme | None
    largest_in: list[Transaction]
    largest_out: list[Transaction]
    top_categories: list[RankedCategory]
    top_merchants: list[RankedMerchant]
