"""Typed security prices: listings, daily series, live quotes, per-position performance."""

import datetime
from decimal import Decimal

import kante
import strawberry

from finance import enums, models
from finance.types import OrgScoped


@kante.django_type(
    models.SecurityListing,
    description="Which listing (a source's symbol) the organization prices an ISIN by. `pinned` ones were chosen by a user.",
)
class SecurityListing(OrgScoped):
    id: strawberry.ID
    isin: str
    source: enums.PriceSource
    symbol: str
    exchange: str | None
    name: str | None
    currency: str | None
    pinned: bool
    resolved_at: datetime.datetime | None
    fetched_at: datetime.datetime | None
    last_error: str | None


@strawberry.type(description="A closing price on a trading day.")
class PricePoint:
    date: datetime.date
    close: Decimal


@strawberry.type(
    description="An ISIN's daily closes from one source (the first, in preference order, that has any in the window)."
)
class PriceSeries:
    isin: str
    source: enums.PriceSource | None
    symbol: str | None
    currency: str | None
    points: list[PricePoint]


@strawberry.type(description="The latest price of an ISIN, fetched now.")
class SecurityQuote:
    isin: str
    source: enums.PriceSource
    symbol: str
    name: str | None
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    currency: str
    time: datetime.datetime | None


@strawberry.type(description="How one depot position's price moved over a window.")
class PositionPerformance:
    isin: str
    name: str
    quantity: Decimal
    source: enums.PriceSource | None
    currency: str | None
    first_date: datetime.date | None
    first_close: Decimal | None
    last_date: datetime.date | None
    last_close: Decimal | None
    change: Decimal | None = strawberry.field(
        description="last − first close, per unit."
    )
    change_ratio: float | None = strawberry.field(description="change / first close.")
    value_change: Decimal | None = strawberry.field(
        description="quantity × change: what the position gained or lost in the window at today's quantity."
    )


@strawberry.type(description="What refreshing one ISIN from one source did.")
class PriceRefresh:
    isin: str
    source: enums.PriceSource
    symbol: str | None
    points: int
    error: str | None


@strawberry.input(
    description="Choose the listing an ISIN is priced by for one source (it is never re-resolved)."
)
class PinSecurityListingInput:
    isin: str
    source: enums.PriceSource
    symbol: str = strawberry.field(
        description="The source's symbol: Yahoo VWCE.DE, Twelve Data VWCE (with `exchange` XETR), Scalable the ISIN."
    )
    exchange: str | None = None
