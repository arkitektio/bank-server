"""Security prices: stored series, live quotes, and how each depot position moved."""

import datetime
from decimal import Decimal
from typing import Optional

import strawberry
from django.db.models import Max
from kante.errors import NotFound
from kante.types import Info

from finance import enums, models
from finance.graphql.errors import translate
from finance.prices import service
from finance.scoping import for_org
from finance.types import prices as t

__all__ = ["security_listings", "security_prices", "security_quote", "position_performance"]


def _isin(value: str) -> str:
    return value.strip().upper()


def security_listings(info: Info, isin: Optional[str] = None) -> list[t.SecurityListing]:
    """The organization's listings (which symbol prices an ISIN, per source)."""
    rows = for_org(models.SecurityListing, info)
    if isin:
        rows = rows.filter(isin=_isin(isin))
    return list(rows.order_by("isin", "source"))  # type: ignore[return-value]


def _series(organization_id: int, isin: str, date_from, date_to, source) -> t.PriceSeries:  # noqa: ANN001
    found = service.series(organization_id, isin, date_from, date_to, source.value if source else None)
    return t.PriceSeries(
        isin=isin,
        source=enums.PriceSource(found.source) if found.source else None,
        symbol=found.symbol,
        currency=found.currency,
        points=[t.PricePoint(date=p.date, close=p.close) for p in found.points],
    )


def security_prices(info: Info, isin: str, date_from: Optional[datetime.date] = None, date_to: Optional[datetime.date] = None, price_source: Optional[enums.PriceSource] = None) -> t.PriceSeries:
    """An ISIN's stored daily closes (refresh them with `refreshSecurityPrices`); from `priceSource`, else the first source with prices."""
    return _series(info.context.request.organization.id, _isin(isin), date_from, date_to, price_source)


async def security_quote(info: Info, isin: str, price_source: Optional[enums.PriceSource] = None) -> t.SecurityQuote:
    """The latest price of an ISIN, fetched now from the first source (in preference order) that answers."""
    code = _isin(isin)
    try:
        found = await service.quote(info.context.request.organization.id, code, price_source.value if price_source else None)
    except Exception as error:
        raise translate(error) from error
    if found is None:
        raise NotFound(f"No source could price {code}.")
    listing, q = found
    return t.SecurityQuote(isin=code, source=enums.PriceSource(listing.source), symbol=listing.symbol, name=q.name or listing.name, price=q.price, bid=q.bid, ask=q.ask, currency=q.currency, time=q.time)


def position_performance(info: Info, date_from: Optional[datetime.date] = None, date_to: Optional[datetime.date] = None, accounts: Optional[list[strawberry.ID]] = None) -> list[t.PositionPerformance]:
    """How each current depot position's price moved over a window (the last 30 days by default), biggest move in value first."""
    organization_id = info.context.request.organization.id
    end = date_to or datetime.date.today()
    start = date_from or end - datetime.timedelta(days=30)
    depots = for_org(models.BankAccount, info).filter(kind=models.AccountKind.DEPOT)
    if accounts:
        depots = depots.filter(id__in=accounts)
    out = []
    for depot in depots:
        latest = depot.holdings.aggregate(day=Max("date"))["day"]
        if not latest:
            continue
        for position in depot.holdings.filter(date=latest):
            found = service.series(organization_id, position.isin, start, end)
            first = found.points[0] if found.points else None
            last = found.points[-1] if found.points else None
            change = (last.close - first.close) if first and last else None
            out.append(
                t.PositionPerformance(
                    isin=position.isin,
                    name=position.name,
                    quantity=position.quantity,
                    source=enums.PriceSource(found.source) if found.source else None,
                    currency=found.currency,
                    first_date=first.date if first else None,
                    first_close=first.close if first else None,
                    last_date=last.date if last else None,
                    last_close=last.close if last else None,
                    change=change,
                    change_ratio=round(float(change / first.close), 4) if change is not None and first.close else None,
                    value_change=(position.quantity * change).quantize(Decimal("0.01")) if change is not None else None,
                )
            )
    return sorted(out, key=lambda p: -(abs(p.value_change) if p.value_change is not None else -1))
