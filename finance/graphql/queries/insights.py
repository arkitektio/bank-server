"""Insight views: typed stats for one merchant, place, area, category, period, the depot, recurring payments and an account.

Every view resolves its entity in the caller's organization (another organization's is
NOT_FOUND), narrows the organization's transactions to the window, and computes on read.
"""

import datetime
from typing import Optional

import strawberry
from kante.types import Info

from finance import enums, models
from finance.graphql.mutations.merchants import MerchantRef, resolve_merchant
from finance.graphql.utils import get_many, get_or_404
from finance.insights import views
from finance.insights.common import resolve_window
from finance.filters import BoundsInput
from finance.types import insights as t

__all__ = [
    "merchant_insights",
    "location_insights",
    "area_insights",
    "spending_grid",
    "category_insights",
    "period_overview",
    "portfolio_insights",
    "recurring_insights",
    "account_insights",
]


def merchant_insights(info: Info, merchant: MerchantRef, window: Optional[t.StatsWindowInput] = None, compare_to: enums.Comparison = enums.Comparison.PREVIOUS_PERIOD) -> t.MerchantInsights:
    """One merchant (by id or key): totals, tickets, visits, monthly and weekday profile, its places, its share of its category."""
    return views.merchant_insights(resolve_merchant(info, merchant), resolve_window(info, window, compare_to))


def location_insights(info: Info, location: strawberry.ID, window: Optional[t.StatsWindowInput] = None) -> t.LocationInsights:
    """One merchant place: totals, tickets, visits, monthly and weekday profile."""
    return views.location_insights(get_or_404(models.MerchantLocation, info, location), resolve_window(info, window))


def area_insights(info: Info, area: t.AreaInput, window: Optional[t.StatsWindowInput] = None, limit: int = 10) -> t.AreaInsights:
    """Spending at located places inside a circle or a viewport: totals, top merchants, categories and places, monthly."""
    return views.area_insights(area, resolve_window(info, window), limit)


def spending_grid(info: Info, within: BoundsInput, cell_meters: float = 500.0, window: Optional[t.StatsWindowInput] = None) -> t.GridCellCollection:
    """Spending in a map viewport binned into cells of about `cellMeters` — a typed GeoJSON FeatureCollection for a heatmap."""
    return views.spending_grid(within, cell_meters, resolve_window(info, window))


def category_insights(
    info: Info, category: strawberry.ID, window: Optional[t.StatsWindowInput] = None, compare_to: enums.Comparison = enums.Comparison.PREVIOUS_PERIOD, include_children: bool = True
) -> t.CategoryInsights:
    """One category (children rolled up): totals, trend, monthly average, share of spending, children, top merchants and counterparties, budgets."""
    return views.category_insights(get_or_404(models.Category, info, category), resolve_window(info, window, compare_to), include_children)


def period_overview(info: Info, window: Optional[t.StatsWindowInput] = None, compare_to: enums.Comparison = enums.Comparison.PREVIOUS_PERIOD, limit: int = 10) -> t.PeriodOverview:
    """A dashboard for a window against the previous period (or the same one last year): totals, savings rate, movers, largest transactions, new merchants, calendar."""
    return views.period_overview(resolve_window(info, window, compare_to), limit)


def portfolio_insights(info: Info, accounts: Optional[list[strawberry.ID]] = None, date_from: Optional[datetime.date] = None) -> t.PortfolioInsights:
    """The depot (all accounts, or these): value vs. money put in over time, allocation, positions, cost basis, investment income per year."""
    chosen = get_many(models.BankAccount, info, accounts) if accounts else None
    return views.portfolio_insights(info, chosen, date_from)


def recurring_insights(info: Info, include_detected: bool = False) -> t.RecurringInsights:
    """Recurring payments (confirmed; detected too if asked): monthly commitment, by category, due soon, missed, price changes."""
    return views.recurring_insights(info.context.request.organization.id, include_detected)


def account_insights(info: Info, account: strawberry.ID, window: Optional[t.StatsWindowInput] = None, limit: int = 5) -> t.AccountInsights:
    """One account: totals, monthly flow, average/lowest/highest balance, largest in and out, top categories and merchants."""
    return views.account_insights(get_or_404(models.BankAccount, info, account), resolve_window(info, window), limit)
