"""Single-object and lookup queries. Lists are plain paginated fields in the schema."""

import datetime
from typing import Optional

import strawberry
import strawberry_django
from kante.types import Info

from finance import filters as filters_module
from finance import models, types
from finance.scoping import for_org
from finance.enablebanking.client import EnableBankingClient
from finance.graphql.errors import translate
from finance.graphql.utils import get_or_404

__all__ = [
    "bank_connection",
    "bank_account",
    "transaction",
    "category",
    "category_rule",
    "budget",
    "recurring_payment",
    "bank_institutions",
    "holdings",
    "transactions_count",
    "suggest_categories",
]


def bank_connection(info: Info, id: strawberry.ID) -> types.BankConnection:
    """A bank connection by id."""
    return get_or_404(models.BankConnection, info, id)


def bank_account(info: Info, id: strawberry.ID) -> types.BankAccount:
    """A bank account by id."""
    return get_or_404(models.BankAccount, info, id)


def transaction(info: Info, id: strawberry.ID) -> types.Transaction:
    """A transaction by id."""
    return get_or_404(models.Transaction, info, id)


def category(info: Info, id: strawberry.ID) -> types.Category:
    """A category by id."""
    return get_or_404(models.Category, info, id)


def category_rule(info: Info, id: strawberry.ID) -> types.CategoryRule:
    """A categorization rule by id."""
    return get_or_404(models.CategoryRule, info, id)


def budget(info: Info, id: strawberry.ID) -> types.Budget:
    """A budget by id."""
    return get_or_404(models.Budget, info, id)


def recurring_payment(info: Info, id: strawberry.ID) -> types.RecurringPayment:
    """A recurring payment by id."""
    return get_or_404(models.RecurringPayment, info, id)


async def bank_institutions(info: Info, country: str) -> list[types.Institution]:
    """The banks Enable Banking can link in a country (ISO code, e.g. ``AT``)."""
    try:
        async with EnableBankingClient() as eb:
            aspsps = await eb.aspsps(country.upper())
    except Exception as error:
        raise translate(error) from error
    return [
        types.Institution(
            name=a["name"],
            country=a.get("country", country.upper()),
            logo=a.get("logo"),
            bic=a.get("bic"),
            maximum_consent_days=(a.get("maximum_consent_validity") or 0) // 86400 or None,
        )
        for a in aspsps
    ]


def holdings(info: Info, account: strawberry.ID, date: Optional[datetime.date] = None) -> list[types.HoldingSnapshot]:
    """A depot's positions on a day (its latest synced day by default), largest first."""
    depot = get_or_404(models.BankAccount, info, account)
    rows = depot.holdings.all()
    day = date or rows.order_by("-date").values_list("date", flat=True).first()
    return list(rows.filter(date=day).order_by("-valuation")) if day else []  # type: ignore[return-value]


def transactions_count(info: Info, filters: Optional[filters_module.TransactionFilter] = None) -> int:
    """How many transactions match (the same filters as `transactions`), e.g. for page numbers or an "uncategorized" badge."""
    rows = for_org(models.Transaction, info)
    if filters is not None:
        rows = strawberry_django.filters.apply(filters, rows, info)
    return rows.count()


def suggest_categories(info: Info, transaction: strawberry.ID, limit: int = 3) -> list[types.CategorySuggestion]:
    """The categories a transaction most likely belongs to (the same as `Transaction.suggestedCategories`)."""
    tx = get_or_404(models.Transaction, info, transaction)
    return types.category_suggestions(tx, limit)
