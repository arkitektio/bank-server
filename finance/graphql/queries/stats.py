"""Stats, budget status and forecast queries.

Each one starts from an org-scoped queryset, so none can aggregate another organization's
rows — an account id from another organization is NOT_FOUND like any other.
"""

import datetime

import strawberry
from kante.types import Info

from finance import budgets as budget_logic
from finance import enums, models, stats, types
from finance.forecast import forecast as forecast_logic
from finance.graphql.utils import get_many, get_or_404
from finance.scoping import for_org

__all__ = ["spending_by_category", "cashflow", "top_counterparties", "balance_history", "budget_status", "forecast"]


def _scoped_window(info: Info, date_from, date_to, accounts, include_transfers: bool):  # noqa: ANN001, ANN202
    account_ids = [a.id for a in get_many(models.BankAccount, info, accounts)] if accounts else None
    return stats.window(for_org(models.Transaction, info), date_from, date_to, account_ids, include_transfers)


def spending_by_category(
    info: Info,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    accounts: list[strawberry.ID] | None = None,
    include_transfers: bool = False,
) -> list[types.CategoryTotal]:
    """Income, expense and net per category and currency over booked transactions, biggest expense first."""
    totals = stats.spending_by_category(_scoped_window(info, date_from, date_to, accounts, include_transfers))
    categories = {c.id: c for c in for_org(models.Category, info).filter(id__in=[t.category_id for t in totals if t.category_id])}
    return [
        types.CategoryTotal(category=categories.get(t.category_id), currency=t.currency, income=t.income, expense=t.expense, net=t.net, count=t.count)  # type: ignore[arg-type]
        for t in totals
    ]


def cashflow(
    info: Info,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    granularity: enums.Granularity = enums.Granularity.MONTH,
    accounts: list[strawberry.ID] | None = None,
    include_transfers: bool = False,
) -> list[types.CashflowBucket]:
    """Income, expense and net per month (or week) and currency, oldest first."""
    buckets = stats.cashflow(_scoped_window(info, date_from, date_to, accounts, include_transfers), granularity.value)
    return [types.CashflowBucket(period_start=b.period_start, currency=b.currency, income=b.income, expense=b.expense, net=b.net, count=b.count) for b in buckets]


def top_counterparties(
    info: Info,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    direction: enums.Direction = enums.Direction.OUT,
    limit: int = 10,
    accounts: list[strawberry.ID] | None = None,
) -> list[types.CounterpartyTotal]:
    """Who the most money went to (``OUT``) or came from (``IN``)."""
    rows = stats.top_counterparties(_scoped_window(info, date_from, date_to, accounts, False), direction.value, limit)
    return [types.CounterpartyTotal(counterparty=r.counterparty, currency=r.currency, total=r.total, count=r.count) for r in rows]


def balance_history(info: Info, account: strawberry.ID, date_from: datetime.date, date_to: datetime.date | None = None) -> list[types.BalancePoint]:
    """The account's end-of-day balance for every day in the range (up to today by default)."""
    bank_account = get_or_404(models.BankAccount, info, account)
    points = stats.balance_history(bank_account, date_from, date_to or datetime.date.today())
    return [types.BalancePoint(date=p.date, amount=p.amount, currency=p.currency, reported=p.reported) for p in points]


def budget_status(info: Info, month: datetime.date | None = None) -> list[types.BudgetStatus]:
    """Budgeted vs. spent for every budget active in a month (the current one by default)."""
    month = month or datetime.date.today()
    rows = budget_logic.budget_status(for_org(models.Budget, info), for_org(models.Transaction, info), info.context.request.organization.id, month)
    return [types.BudgetStatus(budget=r.budget, month=r.month, budgeted=r.budgeted, spent=r.spent, remaining=r.remaining, ratio=r.ratio) for r in rows]  # type: ignore[arg-type]


def forecast(
    info: Info,
    account: strawberry.ID,
    horizon_days: int = 90,
    include_detected: bool = False,
    include_budgets: bool = False,
) -> list[types.ForecastPoint]:
    """The account's expected daily balance: the latest balance plus confirmed recurring payments.

    ``includeDetected`` adds unreviewed recurring payments; ``includeBudgets`` spreads what is
    left of each monthly budget over the rest of the month (a recurring payment in a budgeted
    category is then counted twice).
    """
    bank_account = get_or_404(models.BankAccount, info, account)
    points = forecast_logic(
        bank_account,
        horizon_days,
        include_detected=include_detected,
        budgets=for_org(models.Budget, info) if include_budgets else None,
        transactions=for_org(models.Transaction, info) if include_budgets else None,
    )
    return [types.ForecastPoint(date=p.date, amount=p.amount, currency=p.currency) for p in points]
