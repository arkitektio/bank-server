"""Resolving listings, pinning one, and fetching prices — all inside the request that asks."""

import datetime

from channels.db import database_sync_to_async
from kante.types import Info

from finance import enums, models
from finance.graphql.errors import translate
from finance.prices import service
from finance.types import prices as t

__all__ = [
    "resolve_security_listings",
    "pin_security_listing",
    "refresh_security_prices",
]


def _isins(values: list[str] | None) -> list[str] | None:
    return [v.strip().upper() for v in values] if values else None


def _names(sources: list[enums.PriceSource] | None) -> list[str] | None:
    return [s.value for s in sources] if sources else None


async def resolve_security_listings(
    info: Info,
    isins: list[str] | None = None,
    sources: list[enums.PriceSource] | None = None,
) -> list[t.SecurityListing]:
    """Find a listing per ISIN (the held ones by default) and source through OpenFIGI and the preferred exchanges. Pinned listings stay."""
    organization_id = info.context.request.organization.id
    wanted = _isins(isins) or await database_sync_to_async(service.held_isins)(
        organization_id
    )
    try:
        return await service.resolve(organization_id, wanted, _names(sources))  # type: ignore[return-value]
    except Exception as error:
        raise translate(error) from error


def pin_security_listing(
    info: Info, input: t.PinSecurityListingInput
) -> t.SecurityListing:
    """Price an ISIN by this symbol for this source, from now on (never re-resolved)."""
    listing, _ = models.SecurityListing.objects.update_or_create(
        organization=info.context.request.organization,
        isin=input.isin.strip().upper(),
        source=input.source.value,
        defaults={
            "symbol": input.symbol.strip(),
            "exchange": input.exchange,
            "pinned": True,
            "last_error": None,
        },
    )
    return listing  # type: ignore[return-value]


async def refresh_security_prices(
    info: Info,
    isins: list[str] | None = None,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    sources: list[enums.PriceSource] | None = None,
) -> list[t.PriceRefresh]:
    """Fetch daily closes (the held ISINs and the last year by default) from every enabled source and store them."""
    end = date_to or datetime.date.today()
    start = date_from or end - datetime.timedelta(days=365)
    try:
        outcomes = await service.refresh(
            info.context.request.organization.id,
            _isins(isins),
            start,
            end,
            _names(sources),
        )
    except Exception as error:
        raise translate(error) from error
    return [
        t.PriceRefresh(
            isin=o.isin,
            source=enums.PriceSource(o.source),
            symbol=o.symbol,
            points=o.points,
            error=o.error,
        )
        for o in outcomes
    ]
