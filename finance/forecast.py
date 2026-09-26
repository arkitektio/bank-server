"""Projecting an account's balance forward.

Start at the latest reported balance and add every expected occurrence of the account's
recurring payments. Only CONFIRMED patterns count by default; DETECTED ones on request.
Optionally, the remaining monthly budgets are spread evenly over the rest of each month —
note that a recurring payment categorized into a budgeted category is then counted twice.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import QuerySet
from django.utils import timezone

from finance import models
from finance.budgets import budget_status
from finance.stats import ZERO, latest_balance, month_bounds


@dataclass
class ForecastPoint:
    """The expected end-of-day balance on one day."""

    date: date
    amount: Decimal
    currency: str


def forecast(
    account: models.BankAccount,
    horizon_days: int,
    include_detected: bool = False,
    budgets: QuerySet | None = None,
    transactions: QuerySet | None = None,
) -> list[ForecastPoint]:
    """One point per day from today to ``today + horizon_days``. Empty without a balance."""
    anchor = latest_balance(account)
    if anchor is None:
        return []
    today = timezone.now().date()
    end = today + timedelta(days=max(1, min(horizon_days, 3 * 365)))

    statuses = [models.RecurringStatus.CONFIRMED] + ([models.RecurringStatus.DETECTED] if include_detected else [])
    changes: dict[date, Decimal] = {}
    for pattern in account.recurring_payments.filter(status__in=statuses, currency=anchor.currency):
        occurrence = pattern.next_expected
        if occurrence <= today:
            # Overdue by less than one interval: assume it still comes, tomorrow. Longer
            # overdue means the pattern has probably stopped; skip the missed ones.
            if (today - occurrence).days < pattern.interval_days:
                tomorrow = today + timedelta(days=1)
                changes[tomorrow] = changes.get(tomorrow, ZERO) + pattern.amount
            while occurrence <= today:
                occurrence += timedelta(days=pattern.interval_days)
        while occurrence <= end:
            changes[occurrence] = changes.get(occurrence, ZERO) + pattern.amount
            occurrence += timedelta(days=pattern.interval_days)

    if budgets is not None and transactions is not None:
        month = today.replace(day=1)
        while month <= end:
            first, last = month_bounds(month)
            month_statuses = budget_status(budgets.filter(currency=anchor.currency), transactions.filter(account=account), account.organization_id, month)
            span_start = max(first, today + timedelta(days=1))
            days = (last - span_start).days + 1
            for status in month_statuses:
                if days <= 0 or status.remaining <= 0:
                    continue
                per_day = (status.remaining / days).quantize(Decimal("0.01"))
                day = span_start
                while day <= min(last, end):
                    changes[day] = changes.get(day, ZERO) - per_day
                    day += timedelta(days=1)
            month = last + timedelta(days=1)

    points = []
    amount = anchor.amount
    day = today
    while day <= end:
        amount += changes.get(day, ZERO)
        points.append(ForecastPoint(day, amount, anchor.currency))
        day += timedelta(days=1)
    return points
