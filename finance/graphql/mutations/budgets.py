"""Budgets and recurring payments."""

import datetime
from decimal import Decimal
from typing import Optional

import strawberry
from kante.errors import ValidationError
from kante.types import Info

from finance import enums, models, recurring, types
from finance.graphql.utils import get_many, get_or_404
from finance.scoping import for_org

__all__ = [
    "CreateBudgetInput",
    "UpdateBudgetInput",
    "SetRecurringStatusInput",
    "create_budget",
    "update_budget",
    "delete_budget",
    "detect_recurring",
    "set_recurring_status",
    "set_recurring_statuses",
]


@strawberry.input(description="A new monthly budget.")
class CreateBudgetInput:
    category: strawberry.ID
    amount: Decimal = strawberry.field(description="The monthly limit, positive.")
    currency: str = "EUR"
    start_month: Optional[datetime.date] = strawberry.field(default=None, description="Any day of the first month; the current month by default.")
    end_month: Optional[datetime.date] = strawberry.field(default=None, description="Any day of the last month; open-ended by default.")


@strawberry.input(description="Changes to a budget; omitted fields stay as they are.")
class UpdateBudgetInput:
    id: strawberry.ID
    amount: Optional[Decimal] = strawberry.UNSET
    currency: Optional[str] = strawberry.UNSET
    start_month: Optional[datetime.date] = strawberry.UNSET
    end_month: Optional[datetime.date] = strawberry.UNSET


def _check(budget: models.Budget) -> models.Budget:
    if budget.amount <= 0:
        raise ValidationError("A budget amount must be positive.")
    if len(budget.currency) != 3:
        raise ValidationError("Currency must be an ISO code like EUR.")
    budget.currency = budget.currency.upper()
    budget.start_month = budget.start_month.replace(day=1)
    if budget.end_month is not None:
        budget.end_month = budget.end_month.replace(day=1)
        if budget.end_month < budget.start_month:
            raise ValidationError("endMonth must not be before startMonth.")
    budget.save()
    return budget


def create_budget(info: Info, input: CreateBudgetInput) -> types.Budget:
    """Budget a category (and its children) per month."""
    return _check(  # type: ignore[return-value]
        models.Budget(
            organization=info.context.request.organization,
            category=get_or_404(models.Category, info, input.category),
            amount=input.amount,
            currency=input.currency,
            start_month=input.start_month or datetime.date.today(),
            end_month=input.end_month,
        )
    )


def update_budget(info: Info, input: UpdateBudgetInput) -> types.Budget:
    """Change a budget."""
    budget = get_or_404(models.Budget, info, input.id)
    for name in ("amount", "currency", "start_month"):
        value = getattr(input, name)
        if value is not strawberry.UNSET and value is not None:
            setattr(budget, name, value)
    if input.end_month is not strawberry.UNSET:
        budget.end_month = input.end_month
    return _check(budget)  # type: ignore[return-value]


def delete_budget(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Delete a budget."""
    get_or_404(models.Budget, info, id).delete()
    return id


def detect_recurring(info: Info, accounts: Optional[list[strawberry.ID]] = None) -> list[types.RecurringPayment]:
    """(Re-)detect recurring payments in the accounts' history (all accounts by default). Ignored ones stay ignored."""
    selected = get_many(models.BankAccount, info, accounts) if accounts else list(for_org(models.BankAccount, info))
    recurring.detect(selected)
    return list(for_org(models.RecurringPayment, info).filter(account__in=selected).exclude(status=models.RecurringStatus.IGNORED).order_by("next_expected"))  # type: ignore[return-value]


@strawberry.input(description="Confirm or ignore a detected recurring payment.")
class SetRecurringStatusInput:
    id: strawberry.ID
    status: enums.RecurringStatus


def set_recurring_status(info: Info, input: SetRecurringStatusInput) -> types.RecurringPayment:
    """Confirm a recurring payment (it then feeds forecasts) or ignore it (it is never proposed again)."""
    pattern = get_or_404(models.RecurringPayment, info, input.id)
    pattern.status = input.status.value
    pattern.save(update_fields=["status", "updated_at"])
    return pattern  # type: ignore[return-value]


def set_recurring_statuses(info: Info, ids: list[strawberry.ID], status: enums.RecurringStatus) -> list[types.RecurringPayment]:
    """Confirm or ignore many recurring payments in one request."""
    patterns = {str(p.id): p for p in get_many(models.RecurringPayment, info, ids)}
    for pattern in patterns.values():
        pattern.status = status.value
        pattern.save(update_fields=["status", "updated_at"])  # save(), not update(): keeps the provenance history
    return [patterns[str(i)] for i in dict.fromkeys(str(i) for i in ids)]  # type: ignore[misc]
