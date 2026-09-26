"""Aggregates over transactions.

Everything is computed in the database (``values()`` + ``Sum``), grouped by currency: there
is no FX conversion, so a sum never mixes currencies. Transfers between own accounts
(``is_transfer`` or a TRANSFER-kind category) are excluded unless asked for.

Every function takes an already org-scoped queryset (see :func:`finance.scoping.for_org`),
so none of them can see another organization's rows.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, QuerySet, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth, TruncWeek

from finance import models

ZERO = Decimal("0.00")


def _money_sum(expression: Q | None = None) -> Coalesce:
    return Coalesce(Sum("amount", filter=expression), Value(ZERO), output_field=DecimalField(max_digits=18, decimal_places=2))


def _income() -> Coalesce:
    return _money_sum(Q(amount__gt=0))


def _expense() -> Coalesce:
    return _money_sum(Q(amount__lt=0))


def window(transactions: QuerySet, start: date | None, end: date | None, account_ids: list[int] | None = None, include_transfers: bool = False, include_pending: bool = False) -> QuerySet:
    """Narrow transactions to a booking-date window (inclusive), accounts, and stats-relevant rows."""
    qs = transactions
    if not include_pending:
        qs = qs.filter(status=models.TransactionStatus.BOOKED)
    if start:
        qs = qs.filter(booking_date__gte=start)
    if end:
        qs = qs.filter(booking_date__lte=end)
    if account_ids:
        qs = qs.filter(account_id__in=account_ids)
    if not include_transfers:
        qs = qs.exclude(is_transfer=True).exclude(category__kind=models.CategoryKind.TRANSFER)
    return qs


@dataclass
class CategoryTotal:
    """Money in and out of one category in one currency."""

    category_id: int | None
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int


def spending_by_category(qs: QuerySet) -> list[CategoryTotal]:
    """Income, expense (positive) and net per category and currency, biggest expense first."""
    rows = qs.values("category_id", "currency").annotate(income=_income(), expense=_expense(), net=_money_sum(), count=Count("id")).order_by("expense", "category_id")
    return [CategoryTotal(r["category_id"], r["currency"], r["income"], -r["expense"], r["net"], r["count"]) for r in rows]


@dataclass
class MerchantTotal:
    """Money in and out with one merchant in one currency."""

    merchant_id: int | None
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int


def spending_by_merchant(qs: QuerySet, limit: int | None = None) -> list[MerchantTotal]:
    """Income, expense (positive) and net per merchant and currency, biggest expense first."""
    rows = qs.values("merchant_id", "currency").annotate(income=_income(), expense=_expense(), net=_money_sum(), count=Count("id")).order_by("expense", "merchant_id")
    if limit:
        rows = rows[:limit]
    return [MerchantTotal(r["merchant_id"], r["currency"], r["income"], -r["expense"], r["net"], r["count"]) for r in rows]


@dataclass
class CashflowBucket:
    """Money in and out during one period in one currency."""

    period_start: date
    currency: str
    income: Decimal
    expense: Decimal
    net: Decimal
    count: int


def cashflow(qs: QuerySet, granularity: str) -> list[CashflowBucket]:
    """Income, expense (positive) and net per month or week and currency, oldest first."""
    trunc = TruncWeek if granularity == "WEEK" else TruncMonth
    rows = (
        qs.exclude(booking_date=None)
        .annotate(period=trunc("booking_date"))
        .values("period", "currency")
        .annotate(income=_income(), expense=_expense(), net=_money_sum(), count=Count("id"))
        .order_by("period", "currency")
    )
    return [CashflowBucket(r["period"], r["currency"], r["income"], -r["expense"], r["net"], r["count"]) for r in rows]


@dataclass
class CounterpartyTotal:
    """Money to or from one counterparty in one currency."""

    counterparty: str
    currency: str
    total: Decimal
    count: int


def top_counterparties(qs: QuerySet, direction: str, limit: int) -> list[CounterpartyTotal]:
    """The counterparties with the most money out (``OUT``) or in (``IN``), largest first."""
    if direction == "IN":
        qs, order = qs.filter(amount__gt=0), "-total"
    else:
        qs, order = qs.filter(amount__lt=0), "total"
    rows = qs.exclude(counterparty=None).values("counterparty", "currency").annotate(total=_money_sum(), count=Count("id")).order_by(order)[: max(1, min(limit, 500))]
    return [CounterpartyTotal(r["counterparty"], r["currency"], abs(r["total"]), r["count"]) for r in rows]


# Preferred balance types, most authoritative first (ISO 20022 codes).
BALANCE_PREFERENCE = ["CLBD", "ITBD", "XPCD", "CLAV", "ITAV", "OPBD", "PRCD", "OTHR", "IMPT"]  # IMPT: a running balance from an imported statement


def latest_balance(account: models.BankAccount) -> models.BalanceSnapshot | None:
    """The newest snapshot, of the most authoritative type on that day."""
    snapshots = list(account.balances.order_by("-date")[:20])
    if not snapshots:
        return None
    newest = [s for s in snapshots if s.date == snapshots[0].date]
    return min(newest, key=lambda s: BALANCE_PREFERENCE.index(s.balance_type) if s.balance_type in BALANCE_PREFERENCE else len(BALANCE_PREFERENCE))


@dataclass
class BalancePoint:
    """An account balance at the end of one day."""

    date: date
    amount: Decimal
    currency: str
    reported: bool


def balance_history(account: models.BankAccount, start: date, end: date) -> list[BalancePoint]:
    """The end-of-day balance for every day in ``[start, end]``.

    Anchored on the latest snapshot the bank reported and walked backwards (and forwards)
    through the booked transactions, since snapshots only exist for days a sync ran. Days a
    snapshot exists for use it (``reported``).
    """
    anchor = latest_balance(account)
    if anchor is None or start > end:
        return []
    if account.kind == models.AccountKind.DEPOT:
        # A depot's value moves with the market, not with transactions: only reported days are known.
        return [
            BalancePoint(s.date, s.amount, s.currency, True)
            for s in account.balances.filter(balance_type=anchor.balance_type, date__gte=start, date__lte=end).order_by("date")
        ]
    daily = dict(
        account.transactions.filter(status=models.TransactionStatus.BOOKED, currency=anchor.currency)
        .exclude(booking_date=None)
        .values("booking_date")
        .annotate(total=Sum("amount"))
        .values_list("booking_date", "total")
    )
    reported = {s.date: s.amount for s in account.balances.filter(balance_type=anchor.balance_type, date__gte=start, date__lte=end)}

    balances: dict[date, Decimal] = {anchor.date: anchor.amount}
    day, amount = anchor.date, anchor.amount
    while day > start:
        amount -= daily.get(day, ZERO)
        day -= timedelta(days=1)
        balances[day] = amount
    day, amount = anchor.date, anchor.amount
    while day < end:
        day += timedelta(days=1)
        amount += daily.get(day, ZERO)
        balances[day] = amount

    points = []
    day = start
    while day <= end:
        if day in reported:
            points.append(BalancePoint(day, reported[day], anchor.currency, True))
        elif day in balances:
            points.append(BalancePoint(day, balances[day], anchor.currency, False))
        day += timedelta(days=1)
    return points


def month_bounds(month: date) -> tuple[date, date]:
    """The first and last day of ``month``'s month."""
    first = month.replace(day=1)
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])
