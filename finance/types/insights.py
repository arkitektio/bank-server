"""Typed results of the insight views (``finance.insights``).

Money is ``Decimal`` (a string on the wire) and always split per currency — there is no FX.
Only shares, ratios and rates are ``Float``. Expense amounts are positive (money out).
"""

import datetime
from decimal import Decimal
from typing import List, Optional

import strawberry

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


@strawberry.input(description="Which transactions a view looks at: a booking-date range (the last 12 months by default) and optionally some accounts.")
class StatsWindowInput:
    date_from: Optional[datetime.date] = None
    date_to: Optional[datetime.date] = strawberry.field(default=None, description="Inclusive; today by default.")
    accounts: Optional[List[strawberry.ID]] = None
    include_transfers: bool = strawberry.field(default=False, description="Count transfers between own accounts (and investing) as spending/income.")
    include_pending: bool = False


@strawberry.input(description="A map area: a circle (`near`) or a viewport (`within`) — give one.")
class AreaInput:
    near: Optional[NearInput] = None
    within: Optional[BoundsInput] = None


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
    ratio: Optional[float] = strawberry.field(description="delta / previous; null when previous is zero.")


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


@strawberry.type(description="When and how often: visits are distinct days with a transaction.")
class VisitStats:
    visits: int
    first_visit: Optional[datetime.date]
    last_visit: Optional[datetime.date]
    days_since_last_visit: Optional[int]
    average_days_between_visits: Optional[float]
    visits_per_month: Optional[float] = strawberry.field(description="Visits per 30.44 days of the window.")


@strawberry.type(description="A merchant's totals; `share` is its part of all expense in that currency.")
class RankedMerchant:
    merchant: Optional[Merchant]
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(description="A category's totals; `share` is its part of all expense in that currency.")
class RankedCategory:
    category: Optional[Category]
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(description="A place's totals; `share` is its part of all expense in that currency.")
class RankedLocation:
    location: Optional[MerchantLocation]
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int
    share: float


@strawberry.type(description="The window a view covered, and the one it was compared with.")
class WindowInfo:
    start: datetime.date
    end: datetime.date
    previous_start: Optional[datetime.date]
    previous_end: Optional[datetime.date]


# --- views -------------------------------------------------------------------------------------------


@strawberry.type(description="Everything about one merchant over a window.")
class MerchantInsights:
    merchant: Merchant
    window: WindowInfo
    totals: List[MoneyTotals]
    previous: List[MoneyTotals]
    changes: List[Change]
    tickets: List[TicketStats]
    visits: VisitStats
    monthly: List[CashflowBucket]
    weekdays: List[WeekdayTotals]
    locations: List[RankedLocation]
    share_of_category: List[Share] = strawberry.field(description="Its part of the expense in its default category (and children).")


@strawberry.type(description="Everything about one merchant place over a window.")
class LocationInsights:
    location: MerchantLocation
    window: WindowInfo
    totals: List[MoneyTotals]
    tickets: List[TicketStats]
    visits: VisitStats
    monthly: List[CashflowBucket]
    weekdays: List[WeekdayTotals]


@strawberry.type(description="Spending at located places inside a map area.")
class AreaInsights:
    window: WindowInfo
    totals: List[MoneyTotals]
    merchants: List[RankedMerchant]
    categories: List[RankedCategory]
    locations: List[RankedLocation]
    monthly: List[CashflowBucket]


@strawberry.type(description="What a map styles a spending-grid cell by.")
class GridCellProperties:
    currency: str
    expense: Decimal
    income: Decimal
    count: int
    merchants: int
    locations: int


@strawberry.type(description="A GeoJSON Feature: one grid cell (its point is the cell's snapped center).")
class GridCell:
    type: str
    id: strawberry.ID
    geometry: PointGeometry
    properties: GridCellProperties


@strawberry.type(description="A GeoJSON FeatureCollection of spending-grid cells — valid GeoJSON as returned, and typed.")
class GridCellCollection:
    type: str
    features: List[GridCell]
    bbox: Optional[List[float]]
    cell_meters: float


@strawberry.type(description="Everything about one category (children rolled up) over a window.")
class CategoryInsights:
    category: Category
    window: WindowInfo
    totals: List[MoneyTotals]
    previous: List[MoneyTotals]
    changes: List[Change]
    monthly: List[CashflowBucket]
    monthly_average: List[MoneyTotals] = strawberry.field(description="Totals divided by the months in the window (`count` is the number of months).")
    share_of_spending: List[Share] = strawberry.field(description="Its part of all expense.")
    children: List[RankedCategory]
    top_merchants: List[RankedMerchant]
    top_counterparties: List[CounterpartyTotal]
    budgets: List[BudgetStatus] = strawberry.field(description="This month's status of budgets on it.")
    tickets: List[TicketStats]


@strawberry.type(description="A category that moved most against the comparison window.")
class CategoryMove:
    category: Optional[Category]
    change: Change


@strawberry.type(description="A merchant that moved most against the comparison window.")
class MerchantMove:
    merchant: Optional[Merchant]
    change: Change


@strawberry.type(description="A dashboard for a window, compared with the previous period or the same period last year.")
class PeriodOverview:
    window: WindowInfo
    totals: List[MoneyTotals]
    previous: List[MoneyTotals]
    changes: List[Change]
    savings_rate: List[Share] = strawberry.field(description="(income − expense) / income per currency; can be negative.")
    category_movers: List[CategoryMove]
    merchant_movers: List[MerchantMove]
    largest_transactions: List[Transaction]
    new_merchants: List[Merchant] = strawberry.field(description="Merchants whose first transaction falls in the window.")
    daily: List[DayTotals]
    weekdays: List[WeekdayTotals]
    recurring_due: List[RecurringPayment] = strawberry.field(description="Confirmed recurring payments expected within the window.")


@strawberry.type(description="The depot on one day: its value, what was put in, and the gain.")
class ValuationPoint:
    date: datetime.date
    currency: str
    valuation: Decimal
    invested: Decimal = strawberry.field(description="Net money put into securities up to this day (buys and savings plans minus sells).")
    gain: Decimal


@strawberry.type(description="How much of the depot one security type is.")
class Allocation:
    security_type: str
    currency: str
    valuation: Decimal
    share: float


@strawberry.type(description="Investment income or cost in one year.")
class InvestmentIncome:
    year: int
    kind: enums.TransactionKind
    currency: str
    amount: Decimal = strawberry.field(description="Signed: payouts positive, fees and taxes negative.")
    count: int


@strawberry.type(description="The depot: value over time, allocation, positions and investment income.")
class PortfolioInsights:
    valuation_history: List[ValuationPoint]
    allocation: List[Allocation]
    positions: List[HoldingSnapshot]
    valuation: List[CurrencyTotal]
    cost_basis: List[CurrencyTotal] = strawberry.field(description="Σ quantity × FIFO price of the current positions.")
    unrealized_gain: List[CurrencyTotal]
    income: List[InvestmentIncome]


@strawberry.type(description="What a category commits per month in recurring payments.")
class CategoryCommitment:
    category: Optional[Category]
    currency: str
    monthly: Decimal = strawberry.field(description="Money per month (positive for payments out).")
    count: int


@strawberry.type(description="A recurring payment whose amount changed.")
class PriceChange:
    recurring: RecurringPayment
    currency: str
    previous: Decimal
    current: Decimal
    delta: Decimal


@strawberry.type(description="Recurring payments: what they commit, what is due, what did not come, what got pricier.")
class RecurringInsights:
    monthly_committed: List[MoneyTotals] = strawberry.field(description="Normalized to a month (× 30.44 / interval); `count` is the number of payments.")
    by_category: List[CategoryCommitment]
    due_soon: List[RecurringPayment] = strawberry.field(description="Expected within the next 30 days.")
    missed: List[RecurringPayment] = strawberry.field(description="Expected more than 3 days ago and not seen since.")
    price_changes: List[PriceChange]


@strawberry.type(description="An end-of-day balance.")
class BalanceExtreme:
    date: datetime.date
    currency: str
    amount: Decimal


@strawberry.type(description="Everything about one account over a window.")
class AccountInsights:
    account: BankAccount
    window: WindowInfo
    totals: List[MoneyTotals]
    monthly: List[CashflowBucket]
    average_balance: List[CurrencyTotal]
    lowest_balance: Optional[BalanceExtreme]
    highest_balance: Optional[BalanceExtreme]
    largest_in: List[Transaction]
    largest_out: List[Transaction]
    top_categories: List[RankedCategory]
    top_merchants: List[RankedMerchant]
