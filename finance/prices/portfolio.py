"""The depot over time, from positions and daily prices.

A position's quantity on a day is the quantity of its latest holding snapshot on or before that
day; before the first snapshot it is walked back through the broker's trades (a BUY or savings
plan added units, a SELL removed them). Its price on a day is the latest close on or before it
(weekends and holidays carry the Friday close). The depot's value on a day is the sum of
quantity × price over the positions priced that day.
"""

import datetime
from collections import defaultdict
from decimal import Decimal

from finance import models
from finance.prices import service

BUYS = {models.TransactionKind.BUY, models.TransactionKind.SAVINGS_PLAN}
SELLS = {models.TransactionKind.SELL}


def quantities(organization_id: int, depots: list[models.BankAccount]) -> dict[str, list[tuple[datetime.date, Decimal]]]:
    """Per ISIN: (day, quantity from that day on), ascending — from snapshots, walked back through trades."""
    timeline: dict[str, dict[datetime.date, Decimal]] = defaultdict(dict)
    for depot in depots:
        for isin, day, quantity in depot.holdings.values_list("isin", "date", "quantity"):
            timeline[isin][day] = timeline[isin].get(day, Decimal(0)) + quantity
    trades = models.Transaction.objects.filter(
        account__organization_id=organization_id, kind__in=BUYS | SELLS, isin__isnull=False, quantity__isnull=False, status=models.TransactionStatus.BOOKED
    ).values_list("isin", "booking_date", "kind", "quantity")
    by_isin: dict[str, list[tuple[datetime.date, Decimal]]] = defaultdict(list)
    for isin, day, kind, quantity in trades:
        by_isin[isin].append((day, quantity if kind in BUYS else -abs(quantity)))
    out: dict[str, list[tuple[datetime.date, Decimal]]] = {}
    for isin in set(timeline) | set(by_isin):
        points = sorted(timeline.get(isin, {}).items())
        first_day, first_quantity = points[0] if points else (datetime.date.max, Decimal(0))
        # Before the first snapshot: undo each trade on the way back.
        before = []
        quantity = first_quantity
        for day, delta in sorted((t for t in by_isin.get(isin, []) if t[0] <= first_day), key=lambda t: t[0], reverse=True):
            before.append((day, quantity))
            quantity -= delta
        out[isin] = sorted(before, key=lambda p: p[0]) + points
    return out


def _at(steps: list[tuple[datetime.date, Decimal]], day: datetime.date) -> Decimal | None:
    value = None
    for when, amount in steps:
        if when > day:
            break
        value = amount
    return value


def daily_values(organization_id: int, depots: list[models.BankAccount], start: datetime.date | None, end: datetime.date) -> list[tuple[datetime.date, str, Decimal]]:
    """(day, currency, value) for every trading day with a price of a held position."""
    held = quantities(organization_id, depots)
    series = {isin: service.series(organization_id, isin, None, end) for isin in held}
    closes = {isin: [(p.date, p.close) for p in s.points] for isin, s in series.items() if s.points}
    days = sorted({d for points in closes.values() for d, _ in points if start is None or d >= start})
    out = []
    for day in days:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for isin, points in closes.items():
            quantity = _at(held[isin], day)
            price = _at(points, day)
            if quantity and price is not None:
                totals[series[isin].currency or "EUR"] += (quantity * price).quantize(Decimal("0.01"))
        for currency, value in sorted(totals.items()):
            out.append((day, currency, value))
    return out
