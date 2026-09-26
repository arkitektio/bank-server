"""Budget status: budgeted vs. spent per category for one month.

A budget covers its category and all of that category's descendants. Spending is the net
outflow (expenses minus refunds) of booked, non-transfer transactions in the budget's currency.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q, QuerySet, Sum

from finance import models
from finance.stats import ZERO, month_bounds, window


def descendants(organization_id: int) -> dict[int, set[int]]:
    """Each category id mapped to itself plus every category below it."""
    children: dict[int | None, list[int]] = {}
    for cid, parent in models.Category.objects.filter(organization_id=organization_id).values_list("id", "parent_id"):
        children.setdefault(parent, []).append(cid)

    def collect(cid: int) -> set[int]:
        out = {cid}
        for child in children.get(cid, []):
            out |= collect(child)
        return out

    return {cid: collect(cid) for ids in children.values() for cid in ids}


def active_budgets(budgets: QuerySet, month: date) -> QuerySet:
    """The budgets that apply in ``month``."""
    first, _ = month_bounds(month)
    return budgets.filter(start_month__lte=first).filter(Q(end_month__isnull=True) | Q(end_month__gte=first))


@dataclass
class BudgetStatus:
    """One budget in one month."""

    budget: models.Budget
    month: date
    budgeted: Decimal
    spent: Decimal
    remaining: Decimal
    ratio: float


def budget_status(budgets: QuerySet, transactions: QuerySet, organization_id: int, month: date) -> list[BudgetStatus]:
    """Every budget active in ``month`` with what was spent against it."""
    first, last = month_bounds(month)
    tree = descendants(organization_id)
    booked = window(transactions, first, last)
    out = []
    for budget in active_budgets(budgets, month).select_related("category"):
        spent_net = booked.filter(category_id__in=tree.get(budget.category_id, {budget.category_id}), currency=budget.currency).aggregate(total=Sum("amount"))["total"] or ZERO
        spent = max(ZERO, -spent_net)
        out.append(
            BudgetStatus(
                budget=budget,
                month=first,
                budgeted=budget.amount,
                spent=spent,
                remaining=budget.amount - spent,
                ratio=float(spent / budget.amount) if budget.amount else 0.0,
            )
        )
    return out
