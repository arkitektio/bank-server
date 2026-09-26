"""Detecting recurring payments (rent, salary, subscriptions).

A heuristic, not a model: booked, non-transfer transactions of one account are grouped by
normalized counterparty, direction and currency; the amounts within ±``AMOUNT_TOLERANCE`` of
the group's median are kept; and if at least ``MIN_OCCURRENCES`` of them fall at a regular
interval — weekly, fortnightly, monthly, quarterly or yearly — it is a recurring payment.

Re-detection updates what it found before (keyed by account, key and currency). A pattern a
user ignored stays ignored; a confirmed one stays confirmed and keeps its dates current.
"""

import re
import statistics
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from finance import models

AMOUNT_TOLERANCE = Decimal("0.10")
MIN_OCCURRENCES = 3
LOOKBACK_DAYS = 800
# (interval in days, how far the mean interval may be off, how much the intervals may spread).
PERIODS = [(7, 1.5, 2.0), (14, 2.5, 3.0), (30, 4.0, 5.0), (91, 10.0, 12.0), (365, 20.0, 25.0)]

_NOISE = re.compile(r"[^a-z]+")


def counterparty_key(tx: models.Transaction) -> str | None:
    """The counterparty, lower-cased and stripped of digits and punctuation (card numbers, dates)."""
    name = tx.counterparty or tx.counterparty_iban or (tx.remittance or "")[:40]
    key = _NOISE.sub(" ", name.lower()).strip()
    return key or None


def classify(dates: list[date]) -> int | None:
    """The period the dates recur at, or None if they are not regular."""
    if len(dates) < MIN_OCCURRENCES:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    if any(gap == 0 for gap in gaps):
        return None
    mean = statistics.mean(gaps)
    spread = statistics.pstdev(gaps)
    for period, mean_slack, spread_slack in PERIODS:
        if abs(mean - period) <= mean_slack and spread <= spread_slack:
            return period
    return None


def _groups(account: models.BankAccount) -> dict[tuple[str, str, str], list[models.Transaction]]:
    since = timezone.now().date() - timedelta(days=LOOKBACK_DAYS)
    groups: dict[tuple[str, str, str], list[models.Transaction]] = {}
    rows = account.transactions.filter(status=models.TransactionStatus.BOOKED, is_transfer=False, booking_date__gte=since).order_by("booking_date", "id")
    for tx in rows:
        key = counterparty_key(tx)
        if key is None or tx.amount == 0:
            continue
        direction = "in" if tx.amount > 0 else "out"
        groups.setdefault((f"{direction}:{key}", tx.currency, direction), []).append(tx)
    return groups


def detect_for_account(account: models.BankAccount) -> int:
    """(Re-)detect the account's recurring payments; returns how many patterns were found."""
    found = 0
    for (key, currency, _direction), txs in _groups(account).items():
        median = statistics.median([tx.amount for tx in txs])
        close = [tx for tx in txs if abs(tx.amount - median) <= abs(median) * AMOUNT_TOLERANCE]
        # One occurrence per day at most: a second same-day charge is not part of the rhythm.
        by_day: dict[date, models.Transaction] = {}
        for tx in close:
            by_day.setdefault(tx.booking_date, tx)
        dates = sorted(by_day)
        period = classify(dates)
        if period is None:
            continue
        found += 1
        members = [by_day[d] for d in dates]
        with db_transaction.atomic():
            pattern, created = models.RecurringPayment.objects.select_for_update().get_or_create(
                account=account,
                key=key[:500],
                currency=currency,
                defaults={
                    "organization_id": account.organization_id,
                    "label": members[-1].counterparty or key,
                    "amount": statistics.median([tx.amount for tx in members]),
                    "interval_days": period,
                    "occurrences": len(members),
                    "last_seen": dates[-1],
                    "next_expected": dates[-1] + timedelta(days=period),
                },
            )
            if pattern.status == models.RecurringStatus.IGNORED:
                continue
            if not created:
                pattern.label = members[-1].counterparty or key
                pattern.amount = statistics.median([tx.amount for tx in members])
                pattern.interval_days = period
                pattern.occurrences = len(members)
                pattern.last_seen = dates[-1]
                pattern.next_expected = dates[-1] + timedelta(days=period)
                pattern.save()
            pattern.transactions.set(members)
    return found


def detect(accounts) -> int:  # noqa: ANN001 - an iterable of BankAccount
    """Detect for several accounts."""
    return sum(detect_for_account(account) for account in accounts)
