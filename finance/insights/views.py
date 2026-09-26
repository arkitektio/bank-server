"""The insight views: merchant, place, area, spending grid, category, period, portfolio, recurring, account.

Each takes the request's window (:func:`finance.insights.common.resolve_window`) and returns a
typed result (:mod:`finance.types.insights`). Everything is computed on read, inside the request.
"""

import datetime
import json
import math
from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, F, Max, Min, Q, Sum
from django.db.models.functions import ExtractYear
from django.utils import timezone

from finance import budgets as budget_logic
from finance import enums, geo, models, stats
from finance.budgets import descendants
from finance.insights import common as c
from finance.insights.common import Window
from finance.scoping import for_org
from finance.types import BudgetStatus, CounterpartyTotal, CurrencyTotal, PointGeometry
from finance.types import insights as t

INVESTING = [models.TransactionKind.BUY, models.TransactionKind.SELL, models.TransactionKind.SAVINGS_PLAN]
INVESTMENT_INCOME = [models.TransactionKind.DISTRIBUTION, models.TransactionKind.INTEREST, models.TransactionKind.FEE, models.TransactionKind.TAX, models.TransactionKind.TAX_RETURN]


# --- merchant and place ------------------------------------------------------------------------------


def merchant_insights(merchant: models.Merchant, w: Window) -> t.MerchantInsights:
    qs = w.qs.filter(merchant=merchant)
    previous = w.previous_qs.filter(merchant=merchant) if w.previous_qs is not None else None
    totals, before = c.totals(qs), c.totals(previous)
    share: list[t.Share] = []
    if merchant.category_id:
        tree = descendants(w.organization_id).get(merchant.category_id, {merchant.category_id})
        in_category = w.qs.filter(category_id__in=tree)
        share = c.shares(c.expense_by_currency(in_category.filter(merchant=merchant)), c.expense_by_currency(in_category))
    return t.MerchantInsights(
        merchant=merchant,  # type: ignore[arg-type]
        window=w.info,
        totals=totals,
        previous=before,
        changes=c.changes(totals, before) if previous is not None else [],
        tickets=c.tickets(qs),
        visits=c.visits(qs, w),
        monthly=c.monthly(qs),
        weekdays=c.weekdays(qs),
        locations=c.ranked_locations(qs, None),
        share_of_category=share,
    )


def location_insights(location: models.MerchantLocation, w: Window) -> t.LocationInsights:
    qs = w.qs.filter(merchant_location=location)
    return t.LocationInsights(
        location=location,  # type: ignore[arg-type]
        window=w.info,
        totals=c.totals(qs),
        tickets=c.tickets(qs),
        visits=c.visits(qs, w),
        monthly=c.monthly(qs),
        weekdays=c.weekdays(qs),
    )


def _in_area(qs, area: t.AreaInput):  # noqa: ANN001, ANN202
    from kante.errors import ValidationError

    if (area.near is None) == (area.within is None):
        raise ValidationError("Give an area's `near` or its `within`, not both.")
    if area.near is not None:
        annotations, where = geo.near("", "merchant_location__point", area.near.latitude, area.near.longitude, area.near.radius_meters)
    else:
        b = area.within
        annotations, where = geo.within("", "merchant_location__point", b.south, b.west, b.north, b.east)
    return qs.filter(merchant_location__point__isnull=False).annotate(**annotations).filter(**where)


def area_insights(area: t.AreaInput, w: Window, limit: int) -> t.AreaInsights:
    qs = _in_area(w.qs, area)
    return t.AreaInsights(
        window=w.info,
        totals=c.totals(qs),
        merchants=c.ranked_merchants(qs, limit),
        categories=c.ranked_categories(qs, limit),
        locations=c.ranked_locations(qs, limit),
        monthly=c.monthly(qs),
    )


def spending_grid(within: "t.BoundsInput", cell_meters: float, w: Window) -> t.GridCellCollection:  # type: ignore[name-defined]
    """Transactions at located places in the viewport, binned into cells of about ``cell_meters``."""
    from kante.errors import ValidationError

    if not 10 <= cell_meters <= 200_000:
        raise ValidationError("cellMeters must be between 10 and 200000.")
    mid = math.radians((within.south + within.north) / 2)
    dlat = cell_meters / 111_320
    dlon = cell_meters / (111_320 * max(math.cos(mid), 0.01))
    annotations, where = geo.within("", "merchant_location__point", within.south, within.west, within.north, within.east)
    snapped = geo.SnapToGrid(geo.AsGeometry(F("merchant_location__point")), dlon, dlat)
    rows = (
        w.qs.filter(merchant_location__point__isnull=False)
        .annotate(**annotations)
        .filter(**where)
        .annotate(_x=geo.PointX(snapped), _y=geo.PointY(snapped))
        .values("_x", "_y", "currency")
        .annotate(
            income=stats._income(),
            expense=stats._expense(),
            count=Count("id"),
            merchants=Count("merchant", distinct=True),
            locations=Count("merchant_location", distinct=True),
        )
        .order_by("_y", "_x", "currency")
    )
    features = []
    for row in rows:
        x, y = round(row["_x"], 6), round(row["_y"], 6)
        features.append(
            t.GridCell(
                type="Feature",
                id=f"{x}:{y}:{row['currency']}",  # type: ignore[arg-type]
                geometry=PointGeometry(type="Point", coordinates=[x, y]),
                properties=t.GridCellProperties(currency=row["currency"], expense=-row["expense"], income=row["income"], count=row["count"], merchants=row["merchants"], locations=row["locations"]),
            )
        )
    bbox = None
    if features:
        xs = [f.geometry.coordinates[0] for f in features]
        ys = [f.geometry.coordinates[1] for f in features]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
    return t.GridCellCollection(type="FeatureCollection", features=features, bbox=bbox, cell_meters=cell_meters)


# --- category ----------------------------------------------------------------------------------------


def category_insights(category: models.Category, w: Window, include_children: bool) -> t.CategoryInsights:
    tree = descendants(w.organization_id)
    ids = tree.get(category.id, {category.id}) if include_children else {category.id}
    qs = w.qs.filter(category_id__in=ids)
    previous = w.previous_qs.filter(category_id__in=ids) if w.previous_qs is not None else None
    totals, before = c.totals(qs), c.totals(previous)
    months = max(1, round(w.months))
    average = [
        t.MoneyTotals(currency=x.currency, income=(x.income / months).quantize(c.CENT), expense=(x.expense / months).quantize(c.CENT), net=(x.net / months).quantize(c.CENT), count=months)
        for x in totals
    ]
    children = []
    for child in category.children.order_by("name"):
        child_qs = w.qs.filter(category_id__in=tree.get(child.id, {child.id}))
        whole = c.expense_by_currency(qs)
        for x in c.totals(child_qs):
            children.append(t.RankedCategory(category=child, currency=x.currency, income=x.income, expense=x.expense, net=x.net, count=x.count, share=round(float(x.expense / whole[x.currency]), 4) if whole.get(x.currency) else 0.0))  # type: ignore[arg-type]
    today = timezone.now().date()
    statuses = budget_logic.budget_status(
        models.Budget.objects.filter(organization_id=w.organization_id, category=category), models.Transaction.objects.filter(account__organization_id=w.organization_id), w.organization_id, today
    )
    return t.CategoryInsights(
        category=category,  # type: ignore[arg-type]
        window=w.info,
        totals=totals,
        previous=before,
        changes=c.changes(totals, before) if previous is not None else [],
        monthly=c.monthly(qs),
        monthly_average=average,
        share_of_spending=c.shares(c.expense_by_currency(qs), c.expense_by_currency(w.qs)),
        children=sorted(children, key=lambda r: (-r.expense, r.currency)),
        top_merchants=c.ranked_merchants(qs.exclude(merchant=None), 10),
        top_counterparties=[CounterpartyTotal(counterparty=x.counterparty, currency=x.currency, total=x.total, count=x.count) for x in stats.top_counterparties(qs, "OUT", 10)],
        budgets=[BudgetStatus(budget=s.budget, month=s.month, budgeted=s.budgeted, spent=s.spent, remaining=s.remaining, ratio=s.ratio) for s in statuses],  # type: ignore[arg-type]
        tickets=c.tickets(qs),
    )


# --- period ------------------------------------------------------------------------------------------


def _movers(current, previous, field: str, limit: int) -> list[tuple[int | None, t.Change]]:  # noqa: ANN001
    now = {(r[field], r["currency"]): -r["expense"] for r in c._sums(current, field)}
    before = {(r[field], r["currency"]): -r["expense"] for r in c._sums(previous, field)} if previous is not None else {}
    moves = []
    for key in set(now) | set(before):
        a, b = now.get(key, Decimal("0.00")), before.get(key, Decimal("0.00"))
        if a != b:
            moves.append((key[0], c.change(enums.StatMetric.EXPENSE, key[1], a, b)))
    moves.sort(key=lambda m: (-abs(m[1].delta), m[1].currency, m[0] or 0))
    return moves[:limit]


def period_overview(w: Window, limit: int) -> t.PeriodOverview:
    totals, before = c.totals(w.qs), c.totals(w.previous_qs)
    rates = [t.Share(currency=x.currency, share=round(float((x.income - x.expense) / x.income), 4)) for x in totals if x.income]
    category_moves = _movers(w.qs, w.previous_qs, "category_id", limit)
    merchant_moves = _movers(w.qs.exclude(merchant=None), w.previous_qs.exclude(merchant=None) if w.previous_qs is not None else None, "merchant_id", limit)
    categories = models.Category.objects.in_bulk([k for k, _ in category_moves if k])
    merchants = models.Merchant.objects.in_bulk([k for k, _ in merchant_moves if k])
    new = (
        models.Merchant.objects.filter(organization_id=w.organization_id)
        .annotate(_first=Min("transactions__booking_date"))
        .filter(_first__gte=w.start, _first__lte=w.end)
        .order_by("_first", "name")
    )
    due = models.RecurringPayment.objects.filter(organization_id=w.organization_id, status=models.RecurringStatus.CONFIRMED, next_expected__gte=w.start, next_expected__lte=w.end).order_by("next_expected")
    return t.PeriodOverview(
        window=w.info,
        totals=totals,
        previous=before,
        changes=c.changes(totals, before) if w.previous_qs is not None else [],
        savings_rate=rates,
        category_movers=[t.CategoryMove(category=categories.get(k), change=ch) for k, ch in category_moves],  # type: ignore[arg-type]
        merchant_movers=[t.MerchantMove(merchant=merchants.get(k), change=ch) for k, ch in merchant_moves],  # type: ignore[arg-type]
        largest_transactions=list(w.qs.filter(amount__lt=0).order_by("amount", "id")[:limit]),  # type: ignore[arg-type]
        new_merchants=list(new),  # type: ignore[arg-type]
        daily=c.daily(w.qs),
        weekdays=c.weekdays(w.qs),
        recurring_due=list(due),  # type: ignore[arg-type]
    )


# --- portfolio ---------------------------------------------------------------------------------------


def portfolio_insights(info, accounts: list[models.BankAccount] | None, date_from: datetime.date | None) -> t.PortfolioInsights:  # noqa: ANN001
    scoped = for_org(models.BankAccount, info)
    chosen = accounts if accounts is not None else list(scoped)
    depots = [a for a in chosen if a.kind == models.AccountKind.DEPOT]
    account_ids = [a.id for a in chosen]
    txs = for_org(models.Transaction, info).filter(account_id__in=account_ids, status=models.TransactionStatus.BOOKED)

    # What was put into securities, day by day (buys and savings plans negative on the cash side).
    trades = txs.filter(kind__in=INVESTING).values("booking_date", "currency").annotate(total=Sum("amount")).order_by("booking_date")
    flows: dict[str, list[tuple[datetime.date, Decimal]]] = defaultdict(list)
    for row in trades:
        flows[row["currency"]].append((row["booking_date"], -row["total"]))

    def invested_until(currency: str, day: datetime.date) -> Decimal:
        return sum((amount for d, amount in flows.get(currency, []) if d <= day), Decimal("0.00"))

    # Daily from positions × stored prices (finance.prices); the synced depot valuations otherwise.
    from finance.prices.portfolio import daily_values

    organization_id = info.context.request.organization.id
    daily = daily_values(organization_id, depots, date_from, timezone.now().date()) if depots else []
    if not daily:
        snapshots = models.BalanceSnapshot.objects.filter(account__in=depots, balance_type="VALU")
        if date_from:
            snapshots = snapshots.filter(date__gte=date_from)
        daily = [(r["date"], r["currency"], r["total"]) for r in snapshots.values("date", "currency").annotate(total=Sum("amount")).order_by("date", "currency")]
    history = []
    for day, currency, value in daily:
        invested = invested_until(currency, day)
        history.append(t.ValuationPoint(date=day, currency=currency, valuation=value, invested=invested, gain=value - invested))

    positions = []
    for depot in depots:
        latest = depot.holdings.aggregate(day=Max("date"))["day"]
        if latest:
            positions.extend(depot.holdings.filter(date=latest).order_by("-valuation"))
    valuation: dict[str, Decimal] = defaultdict(Decimal)
    cost: dict[str, Decimal] = defaultdict(Decimal)
    by_type: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for p in positions:
        valuation[p.currency] += p.valuation
        by_type[(p.security_type or "OTHER", p.currency)] += p.valuation
        if p.fifo_price is not None:
            cost[p.currency] += (p.quantity * p.fifo_price).quantize(c.CENT)
    allocation = [
        t.Allocation(security_type=kind, currency=cur, valuation=amount, share=round(float(amount / valuation[cur]), 4) if valuation[cur] else 0.0)
        for (kind, cur), amount in sorted(by_type.items(), key=lambda item: -item[1])
    ]

    income_rows = txs.filter(kind__in=INVESTMENT_INCOME)
    if date_from:
        income_rows = income_rows.filter(booking_date__gte=date_from)
    income = [
        t.InvestmentIncome(year=r["_year"], kind=enums.TransactionKind(r["kind"]), currency=r["currency"], amount=r["total"], count=r["count"])
        for r in income_rows.annotate(_year=ExtractYear("booking_date")).values("_year", "kind", "currency").annotate(total=Sum("amount"), count=Count("id")).order_by("-_year", "kind", "currency")
    ]
    return t.PortfolioInsights(
        valuation_history=history,
        allocation=allocation,
        positions=positions,  # type: ignore[arg-type]
        valuation=[CurrencyTotal(currency=k, amount=v) for k, v in sorted(valuation.items())],
        cost_basis=[CurrencyTotal(currency=k, amount=v) for k, v in sorted(cost.items())],
        unrealized_gain=[CurrencyTotal(currency=k, amount=valuation[k] - cost.get(k, Decimal(0))) for k in sorted(valuation) if k in cost],
        income=income,
    )


# --- recurring ---------------------------------------------------------------------------------------

GRACE_DAYS = 3
DUE_DAYS = 30


def recurring_insights(organization_id: int, include_detected: bool) -> t.RecurringInsights:
    statuses = [models.RecurringStatus.CONFIRMED] + ([models.RecurringStatus.DETECTED] if include_detected else [])
    payments = list(models.RecurringPayment.objects.filter(organization_id=organization_id, status__in=statuses).prefetch_related("transactions__category"))
    today = timezone.now().date()
    committed: dict[str, list[Decimal]] = defaultdict(list)
    by_category: dict[tuple[int | None, str], list[Decimal]] = defaultdict(list)
    changes = []
    for p in payments:
        monthly = (p.amount * Decimal(str(c.MONTH_DAYS)) / p.interval_days).quantize(c.CENT)
        committed[p.currency].append(monthly)
        history = sorted(p.transactions.all(), key=lambda tx: (tx.booking_date or datetime.date.min, tx.id))
        category_id = history[-1].category_id if history else None
        by_category[(category_id, p.currency)].append(monthly)
        if len(history) >= 2 and history[-1].amount != history[-2].amount:
            changes.append(t.PriceChange(recurring=p, currency=p.currency, previous=history[-2].amount, current=history[-1].amount, delta=history[-1].amount - history[-2].amount))  # type: ignore[arg-type]
    categories = models.Category.objects.in_bulk([k for k, _ in by_category if k])
    return t.RecurringInsights(
        monthly_committed=[
            t.MoneyTotals(currency=cur, income=sum((v for v in vals if v > 0), Decimal("0.00")), expense=-sum((v for v in vals if v < 0), Decimal("0.00")), net=sum(vals, Decimal("0.00")), count=len(vals))
            for cur, vals in sorted(committed.items())
        ],
        by_category=sorted(
            (t.CategoryCommitment(category=categories.get(k), currency=cur, monthly=-sum(vals, Decimal("0.00")), count=len(vals)) for (k, cur), vals in by_category.items()),  # type: ignore[arg-type]
            key=lambda x: (-x.monthly, x.currency),
        ),
        due_soon=sorted((p for p in payments if today <= p.next_expected <= today + datetime.timedelta(days=DUE_DAYS)), key=lambda p: p.next_expected),  # type: ignore[arg-type]
        missed=sorted((p for p in payments if p.next_expected < today - datetime.timedelta(days=GRACE_DAYS) and p.last_seen < p.next_expected), key=lambda p: p.next_expected),  # type: ignore[arg-type]
        price_changes=changes,
    )


# --- account -----------------------------------------------------------------------------------------


def account_insights(account: models.BankAccount, w: Window, limit: int) -> t.AccountInsights:
    qs = w.qs.filter(account=account)
    points = stats.balance_history(account, w.start, w.end)
    by_currency: dict[str, list] = defaultdict(list)
    for point in points:
        by_currency[point.currency].append(point)
    averages = [CurrencyTotal(currency=cur, amount=(sum(p.amount for p in pts) / len(pts)).quantize(c.CENT)) for cur, pts in sorted(by_currency.items())]
    lowest = min(points, key=lambda p: (p.amount, p.date), default=None)
    highest = max(points, key=lambda p: (p.amount, p.date), default=None)
    return t.AccountInsights(
        account=account,  # type: ignore[arg-type]
        window=w.info,
        totals=c.totals(qs),
        monthly=c.monthly(qs),
        average_balance=averages,
        lowest_balance=t.BalanceExtreme(date=lowest.date, currency=lowest.currency, amount=lowest.amount) if lowest else None,
        highest_balance=t.BalanceExtreme(date=highest.date, currency=highest.currency, amount=highest.amount) if highest else None,
        largest_in=list(qs.filter(amount__gt=0).order_by("-amount", "id")[:limit]),  # type: ignore[arg-type]
        largest_out=list(qs.filter(amount__lt=0).order_by("amount", "id")[:limit]),  # type: ignore[arg-type]
        top_categories=c.ranked_categories(qs, limit),
        top_merchants=c.ranked_merchants(qs.exclude(merchant=None), limit),
    )
