"""Listings, fetching and reading security prices for an organization.

* :func:`resolve` — make sure each ISIN has a listing per enabled source (OpenFIGI + the
  preferred exchange; pinned listings are kept as they are).
* :func:`refresh` — fetch daily closes for listings and upsert them (``SecurityPrice``).
* :func:`series` — read an ISIN's prices from the first source (in ``prices.sources`` order)
  that has any in the window.
* :func:`quote` — the latest price, live, from the first source that answers.

All of it runs inside the request that asks; a depot sync refreshes Scalable's recent prices.
"""

import datetime
import logging
from dataclasses import dataclass, field

from asgiref.sync import sync_to_async
from django.utils import timezone

from finance import models
from finance.prices import sources as s

logger = logging.getLogger(__name__)


def enabled_sources(only: list[str] | None = None) -> list[str]:
    order = [name for name in (s.conf().get("sources") or list(models.PriceSource.values)) if name in models.PriceSource.values]
    return [name for name in order if only is None or name in only]


def held_isins(organization_id: int) -> list[str]:
    """ISINs of the organization's latest depot positions."""
    isins: set[str] = set()
    for account in models.BankAccount.objects.filter(organization_id=organization_id, kind=models.AccountKind.DEPOT):
        latest = account.holdings.order_by("-date").values_list("date", flat=True).first()
        if latest:
            isins.update(account.holdings.filter(date=latest).values_list("isin", flat=True))
    return sorted(isins)


@dataclass
class Outcome:
    """What one refresh did, per ISIN and source."""

    isin: str
    source: str
    symbol: str | None
    points: int = 0
    error: str | None = None


async def resolve(organization_id: int, isins: list[str], only: list[str] | None = None) -> list[models.SecurityListing]:
    """A listing per ISIN and enabled source, resolved through OpenFIGI (pinned listings stay)."""
    wanted = enabled_sources(only)
    out = []
    async with s.session() as http:
        for isin in isins:
            existing = {listing.source: listing async for listing in models.SecurityListing.objects.filter(organization_id=organization_id, isin=isin)}
            figi_rows: list[s.Figi] | None = None
            for name in wanted:
                listing = existing.get(name)
                if listing is not None and listing.pinned:
                    out.append(listing)
                    continue
                source = s.source_for(name, http, organization_id)
                if source is None:
                    continue
                figi = None
                if name != models.PriceSource.SCALABLE:
                    if figi_rows is None:
                        try:
                            figi_rows = await s.figi_listings(http, isin)
                        except s.PriceError as error:
                            logger.warning("OpenFIGI failed for %s: %s", isin, error)
                            figi_rows = []
                    figi = s.preferred(figi_rows, name)
                found = source.symbol_for(isin, figi)
                if found is None:
                    continue
                symbol, exchange = found
                listing, _ = await models.SecurityListing.objects.aupdate_or_create(
                    organization_id=organization_id,
                    isin=isin,
                    source=name,
                    defaults={"symbol": symbol, "exchange": exchange, "name": figi.name if figi else None, "resolved_at": timezone.now()},
                )
                out.append(listing)
    return out


def _upsert(listing: models.SecurityListing, points: list[s.PricePoint]) -> int:
    rows = [models.SecurityPrice(source=listing.source, symbol=listing.symbol, date=p.date, close=p.close, currency=p.currency) for p in points]
    models.SecurityPrice.objects.bulk_create(rows, update_conflicts=True, unique_fields=["source", "symbol", "date"], update_fields=["close", "currency", "fetched_at"])
    return len(rows)


async def refresh(organization_id: int, isins: list[str] | None, start: datetime.date, end: datetime.date, only: list[str] | None = None) -> list[Outcome]:
    """Fetch and store daily closes for the ISINs (the held ones by default) from each enabled source."""
    isins = isins or await sync_to_async(held_isins)(organization_id)
    listings = await resolve(organization_id, isins, only)
    outcomes = []
    async with s.session() as http:
        for listing in listings:
            source = s.source_for(listing.source, http, organization_id)
            if source is None:
                continue
            try:
                points = await source.history(listing, start, end)
            except s.PriceError as error:
                listing.last_error = str(error)[:1000]
                await listing.asave(update_fields=["last_error"])
                outcomes.append(Outcome(listing.isin, listing.source, listing.symbol, error=str(error)))
                continue
            stored = await sync_to_async(_upsert)(listing, points)
            listing.fetched_at, listing.last_error = timezone.now(), None
            if points:
                listing.currency = points[-1].currency
            await listing.asave(update_fields=["fetched_at", "last_error", "currency"])
            outcomes.append(Outcome(listing.isin, listing.source, listing.symbol, points=stored))
    return outcomes


@dataclass
class Series:
    isin: str
    source: str | None
    symbol: str | None
    currency: str | None
    points: list[models.SecurityPrice] = field(default_factory=list)


def series(organization_id: int, isin: str, start: datetime.date | None = None, end: datetime.date | None = None, source: str | None = None) -> Series:
    """An ISIN's stored closes from the first source (in preference order) that has any in the window."""
    listings = {listing.source: listing for listing in models.SecurityListing.objects.filter(organization_id=organization_id, isin=isin)}
    for name in enabled_sources([source] if source else None):
        listing = listings.get(name)
        if listing is None:
            continue
        rows = models.SecurityPrice.objects.filter(source=name, symbol=listing.symbol)
        if start:
            rows = rows.filter(date__gte=start)
        if end:
            rows = rows.filter(date__lte=end)
        points = list(rows.order_by("date"))
        if points:
            return Series(isin, name, listing.symbol, points[-1].currency, points)
    return Series(isin, None, None, None, [])


async def quote(organization_id: int, isin: str, source: str | None = None) -> tuple[models.SecurityListing, s.Quote] | None:
    """The latest price, fetched now from the first source that answers."""
    listings = {listing.source: listing for listing in await resolve(organization_id, [isin], [source] if source else None)}
    async with s.session() as http:
        for name in enabled_sources([source] if source else None):
            listing = listings.get(name)
            implementation = s.source_for(name, http, organization_id) if listing else None
            if implementation is None:
                continue
            try:
                found = await implementation.quote(listing)
            except s.PriceError as error:
                logger.info("Quote of %s from %s failed: %s", isin, name, error)
                continue
            if found is not None:
                return listing, found
    return None
