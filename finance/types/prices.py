"""Typed security prices: listings, daily series, live quotes, per-position performance."""

import datetime
from decimal import Decimal
from typing import List, Optional

import kante
import strawberry

from finance import enums, models
from finance.types import OrgScoped


@kante.django_type(models.SecurityListing, description="Which listing (a source's symbol) the organization prices an ISIN by. `pinned` ones were chosen by a user.")
class SecurityListing(OrgScoped):
    id: strawberry.ID
    isin: str
    source: enums.PriceSource
    symbol: str
    exchange: Optional[str]
    name: Optional[str]
    currency: Optional[str]
    pinned: bool
    resolved_at: Optional[datetime.datetime]
    fetched_at: Optional[datetime.datetime]
    last_error: Optional[str]


@strawberry.type(description="A closing price on a trading day.")
class PricePoint:
    date: datetime.date
    close: Decimal


@strawberry.type(description="An ISIN's daily closes from one source (the first, in preference order, that has any in the window).")
class PriceSeries:
    isin: str
    source: Optional[enums.PriceSource]
    symbol: Optional[str]
    currency: Optional[str]
    points: List[PricePoint]


@strawberry.type(description="The latest price of an ISIN, fetched now.")
class SecurityQuote:
    isin: str
    source: enums.PriceSource
    symbol: str
    name: Optional[str]
    price: Decimal
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    currency: str
    time: Optional[datetime.datetime]


@strawberry.type(description="How one depot position's price moved over a window.")
class PositionPerformance:
    isin: str
    name: str
    quantity: Decimal
    source: Optional[enums.PriceSource]
    currency: Optional[str]
    first_date: Optional[datetime.date]
    first_close: Optional[Decimal]
    last_date: Optional[datetime.date]
    last_close: Optional[Decimal]
    change: Optional[Decimal] = strawberry.field(description="last − first close, per unit.")
    change_ratio: Optional[float] = strawberry.field(description="change / first close.")
    value_change: Optional[Decimal] = strawberry.field(description="quantity × change: what the position gained or lost in the window at today's quantity.")


@strawberry.type(description="What refreshing one ISIN from one source did.")
class PriceRefresh:
    isin: str
    source: enums.PriceSource
    symbol: Optional[str]
    points: int
    error: Optional[str]


@strawberry.input(description="Choose the listing an ISIN is priced by for one source (it is never re-resolved).")
class PinSecurityListingInput:
    isin: str
    source: enums.PriceSource
    symbol: str = strawberry.field(description="The source's symbol: Yahoo VWCE.DE, Twelve Data VWCE (with `exchange` XETR), Scalable the ISIN.")
    exchange: Optional[str] = None
