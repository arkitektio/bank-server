"""What every insight view is built from: a window of transactions, and the aggregates over it.

A window is the organization's transactions (``for_org``) narrowed by :func:`finance.stats.window`
— booked only, transfers excluded unless asked, optionally some accounts — between two booking
dates, plus the same filter over the comparison window. Everything is computed in the database
and split per currency (no FX); expense is reported positive. The median is the one thing done
in Python (over one view's amounts, which stays small).
"""

import datetime
import statistics
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, QuerySet
from django.db.models.functions import ExtractIsoWeekDay
from django.utils import timezone

from finance import enums, models, stats
from finance.graphql.utils import get_many
from finance.scoping import for_org
from finance.types import CashflowBucket
from finance.types import insights as t

CENT = Decimal("0.01")
DEFAULT_DAYS = 365
MONTH_DAYS = 30.44


@dataclass
class Window:
    """The resolved window of a view, with its comparison window."""

    start: datetime.date
    end: datetime.date
    qs: QuerySet
    previous_start: datetime.date | None
    previous_end: datetime.date | None
    previous_qs: QuerySet | None
    base: QuerySet  # every org transaction the window's account/transfer filters allow, any date
    organization_id: int

    @property
    def info(self) -> t.WindowInfo:
        return t.WindowInfo(start=self.start, end=self.end, previous_start=self.previous_start, previous_end=self.previous_end)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def months(self) -> float:
        return self.days / MONTH_DAYS


def _shift_year(day: datetime.date, years: int) -> datetime.date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def previous_window(start: datetime.date, end: datetime.date, compare: enums.Comparison) -> tuple[datetime.date, datetime.date] | None:
    """The comparison window: the same length right before, or the same dates a year earlier."""
    if compare == enums.Comparison.PREVIOUS_PERIOD:
        previous_end = start - datetime.timedelta(days=1)
        return previous_end - (end - start), previous_end
    if compare == enums.Comparison.SAME_PERIOD_LAST_YEAR:
        return _shift_year(start, -1), _shift_year(end, -1)
    return None


def resolve_window(info, window: t.StatsWindowInput | None, compare: enums.Comparison = enums.Comparison.NONE) -> Window:  # noqa: ANN001
    """The org-scoped window a view asked for (the last 12 months by default)."""
    window = window or t.StatsWindowInput()
    end = window.date_to or timezone.now().date()
    start = window.date_from or end - datetime.timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        from kante.errors import ValidationError

        raise ValidationError("dateFrom must not be after dateTo.")
    account_ids = [a.id for a in get_many(models.BankAccount, info, window.accounts)] if window.accounts else None
    transactions = for_org(models.Transaction, info)
    base = stats.window(transactions, None, None, account_ids, window.include_transfers, window.include_pending)
    previous = previous_window(start, end, compare)
    return Window(
        start=start,
        end=end,
        qs=stats.window(transactions, start, end, account_ids, window.include_transfers, window.include_pending),
        previous_start=previous[0] if previous else None,
        previous_end=previous[1] if previous else None,
        previous_qs=stats.window(transactions, previous[0], previous[1], account_ids, window.include_transfers, window.include_pending) if previous else None,
        base=base,
        organization_id=info.context.request.organization.id,
    )


def _sums(qs: QuerySet, *group: str) -> QuerySet:
    return qs.values(*group, "currency").annotate(income=stats._income(), expense=stats._expense(), net=stats._money_sum(), count=Count("id"))


def totals(qs: QuerySet | None) -> list[t.MoneyTotals]:
    if qs is None:
        return []
    return [t.MoneyTotals(currency=r["currency"], income=r["income"], expense=-r["expense"], net=r["net"], count=r["count"]) for r in _sums(qs).order_by("currency")]


def monthly(qs: QuerySet) -> list[CashflowBucket]:
    return [CashflowBucket(period_start=b.period_start, currency=b.currency, income=b.income, expense=b.expense, net=b.net, count=b.count) for b in stats.cashflow(qs, "MONTH")]


def weekdays(qs: QuerySet) -> list[t.WeekdayTotals]:
    rows = _sums(qs.annotate(_weekday=ExtractIsoWeekDay("booking_date")), "_weekday").order_by("_weekday", "currency")
    return [t.WeekdayTotals(weekday=r["_weekday"], currency=r["currency"], income=r["income"], expense=-r["expense"], count=r["count"]) for r in rows]


def daily(qs: QuerySet) -> list[t.DayTotals]:
    rows = _sums(qs, "booking_date").order_by("booking_date", "currency")
    return [t.DayTotals(date=r["booking_date"], currency=r["currency"], income=r["income"], expense=-r["expense"], count=r["count"]) for r in rows]


def tickets(qs: QuerySet) -> list[t.TicketStats]:
    """Size of the outgoing payments per currency (average, median, largest, smallest)."""
    amounts: dict[str, list[Decimal]] = defaultdict(list)
    for currency, amount in qs.filter(amount__lt=0).values_list("currency", "amount"):
        amounts[currency].append(-amount)
    out = []
    for currency, values in sorted(amounts.items()):
        out.append(
            t.TicketStats(
                currency=currency,
                average=(sum(values) / len(values)).quantize(CENT),
                median=Decimal(statistics.median(values)).quantize(CENT),
                largest=max(values),
                smallest=min(values),
            )
        )
    return out


def visits(qs: QuerySet, window: Window) -> t.VisitStats:
    """Distinct days with a transaction, and how they are spaced."""
    days = sorted(set(qs.exclude(booking_date=None).values_list("booking_date", flat=True)))
    if not days:
        return t.VisitStats(visits=0, first_visit=None, last_visit=None, days_since_last_visit=None, average_days_between_visits=None, visits_per_month=0.0)
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    today = timezone.now().date()
    return t.VisitStats(
        visits=len(days),
        first_visit=days[0],
        last_visit=days[-1],
        days_since_last_visit=(today - days[-1]).days,
        average_days_between_visits=round(sum(gaps) / len(gaps), 2) if gaps else None,
        visits_per_month=round(len(days) / window.months, 3),
    )


def changes(current: list[t.MoneyTotals], previous: list[t.MoneyTotals]) -> list[t.Change]:
    """Income, expense and net against the comparison window, per currency."""
    before = {p.currency: p for p in previous}
    now = {c.currency: c for c in current}
    out = []
    for currency in sorted(set(now) | set(before)):
        for metric, field in ((enums.StatMetric.INCOME, "income"), (enums.StatMetric.EXPENSE, "expense"), (enums.StatMetric.NET, "net")):
            a = getattr(now[currency], field) if currency in now else Decimal("0.00")
            b = getattr(before[currency], field) if currency in before else Decimal("0.00")
            out.append(change(metric, currency, a, b))
    return out


def change(metric: enums.StatMetric, currency: str, current: Decimal, previous: Decimal) -> t.Change:
    return t.Change(metric=metric, currency=currency, current=current, previous=previous, delta=current - previous, ratio=round(float((current - previous) / abs(previous)), 4) if previous else None)


def expense_by_currency(qs: QuerySet) -> dict[str, Decimal]:
    return {r["currency"]: -r["expense"] for r in _sums(qs)}


def ranked(qs: QuerySet, field: str, limit: int | None = 10) -> list[dict]:
    """Totals grouped by ``field`` (merchant_id, category_id, merchant_location_id), biggest expense first, with each group's share of expense."""
    all_expense = expense_by_currency(qs)
    rows = _sums(qs, field).order_by("expense", field)
    if limit:
        rows = rows[:limit]
    out = []
    for r in rows:
        expense = -r["expense"]
        whole = all_expense.get(r["currency"]) or Decimal(0)
        out.append({"id": r[field], "currency": r["currency"], "income": r["income"], "expense": expense, "net": r["net"], "count": r["count"], "share": round(float(expense / whole), 4) if whole else 0.0})
    return out


def ranked_merchants(qs: QuerySet, limit: int | None = 10) -> list[t.RankedMerchant]:
    rows = ranked(qs, "merchant_id", limit)
    found = models.Merchant.objects.in_bulk([r["id"] for r in rows if r["id"]])
    return [t.RankedMerchant(merchant=found.get(r.pop("id")), **r) for r in rows]  # type: ignore[arg-type]


def ranked_categories(qs: QuerySet, limit: int | None = 10) -> list[t.RankedCategory]:
    rows = ranked(qs, "category_id", limit)
    found = models.Category.objects.in_bulk([r["id"] for r in rows if r["id"]])
    return [t.RankedCategory(category=found.get(r.pop("id")), **r) for r in rows]  # type: ignore[arg-type]


def ranked_locations(qs: QuerySet, limit: int | None = 10) -> list[t.RankedLocation]:
    rows = ranked(qs.exclude(merchant_location=None), "merchant_location_id", limit)
    found = models.MerchantLocation.objects.in_bulk([r["id"] for r in rows if r["id"]])
    return [t.RankedLocation(location=found.get(r.pop("id")), **r) for r in rows]  # type: ignore[arg-type]


def shares(part: dict[str, Decimal], whole: dict[str, Decimal]) -> list[t.Share]:
    return [t.Share(currency=c, share=round(float(part.get(c, Decimal(0)) / w), 4)) for c, w in sorted(whole.items()) if w]
