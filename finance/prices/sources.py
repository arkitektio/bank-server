"""Price sources behind one interface: Scalable, Twelve Data, Yahoo — and OpenFIGI to find listings.

Every source answers the same two questions for a listing (a source-specific ``symbol``):
``history(start, end)`` — one closing price per trading day — and ``quote()`` — the latest price.
Listings are found from an ISIN through OpenFIGI (every exchange it trades on), picking the
first exchange in ``prices.preferred_exchanges``; Scalable needs none (it prices by ISIN).

All calls are async (aiohttp) and happen only inside a request that asks.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import aiohttp
from django.conf import settings

from finance import models


class PriceError(Exception):
    """A source could not answer (unreachable, refused, unknown symbol)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class PricePoint:
    date: datetime.date
    close: Decimal
    currency: str


@dataclass
class Quote:
    price: Decimal
    currency: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    time: datetime.datetime | None = None
    name: str | None = None


@dataclass
class Figi:
    """One listing OpenFIGI knows for an ISIN."""

    ticker: str
    exchange: str  # OpenFIGI exchange code (GY Xetra, LN London, …)
    name: str | None


def conf() -> dict:
    return getattr(settings, "PRICES", None) or {}


# OpenFIGI exchange code → (Yahoo suffix, Twelve Data MIC). German composite/regional codes price like Xetra.
EXCHANGES: dict[str, tuple[str, str | None]] = {
    "GY": (".DE", "XETR"), "GR": (".DE", "XETR"), "GF": (".F", "XFRA"), "GM": (".MU", "XMUN"), "GS": (".SG", "XSTU"),
    "GD": (".DU", "XDUS"), "GH": (".HM", "XHAM"), "GB": (".BE", "XBER"), "GI": (".DE", "XETR"),
    "LN": (".L", "XLON"), "AV": (".VI", "XWBO"), "IM": (".MI", "XMIL"), "SW": (".SW", "XSWX"), "SE": (".SW", "XSWX"),
    "NA": (".AS", "XAMS"), "FP": (".PA", "XPAR"), "BB": (".BR", "XBRU"), "ID": (".IR", "XDUB"), "SM": (".MC", "XMAD"),
    "US": ("", None), "UN": ("", "XNYS"), "UW": ("", "XNGS"), "UQ": ("", "XNGS"), "UP": ("", "ARCX"),
}


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def _get(http: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
    try:
        async with http.get(url, **kwargs) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise PriceError(f"{url} answered HTTP {response.status}", response.status)
            return body
    except aiohttp.ClientError as error:
        raise PriceError(f"{url} unreachable: {error}") from error


def session() -> aiohttp.ClientSession:
    c = conf()
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=c.get("timeout_seconds", 15)), headers={"User-Agent": c.get("user_agent", "arkitekt-bank")})


# --- OpenFIGI -------------------------------------------------------------------------------------


async def figi_listings(http: aiohttp.ClientSession, isin: str) -> list[Figi]:
    """Every listing OpenFIGI knows for an ISIN (equity/fund sectors)."""
    c = conf()
    headers = {"Content-Type": "application/json"}
    if c.get("openfigi_api_key"):
        headers["X-OPENFIGI-APIKEY"] = c["openfigi_api_key"]
    try:
        async with http.post(f"{c.get('openfigi_url', 'https://api.openfigi.com').rstrip('/')}/v3/mapping", json=[{"idType": "ID_ISIN", "idValue": isin}], headers=headers) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise PriceError(f"OpenFIGI answered HTTP {response.status}", response.status)
    except aiohttp.ClientError as error:
        raise PriceError(f"OpenFIGI unreachable: {error}") from error
    rows = (body[0] or {}).get("data") or [] if isinstance(body, list) and body else []
    return [Figi(ticker=r["ticker"], exchange=r.get("exchCode") or "", name=r.get("name")) for r in rows if r.get("ticker")]


def preferred(listings: list[Figi], source: str) -> Figi | None:
    """The first listing on a preferred exchange the source can price."""
    order = conf().get("preferred_exchanges") or list(EXCHANGES)
    for exchange in order:
        for figi in listings:
            if figi.exchange == exchange and exchange in EXCHANGES and (source != models.PriceSource.TWELVEDATA or EXCHANGES[exchange][1] or exchange == "US"):
                return figi
    return None


# --- the sources ----------------------------------------------------------------------------------


class Source(Protocol):
    source: str

    def symbol_for(self, isin: str, figi: Figi | None) -> tuple[str, str | None] | None: ...

    async def history(self, listing: models.SecurityListing, start: datetime.date, end: datetime.date) -> list[PricePoint]: ...

    async def quote(self, listing: models.SecurityListing) -> Quote | None: ...


def _daily(points: list[tuple[datetime.date, Decimal]], currency: str, start: datetime.date, end: datetime.date) -> list[PricePoint]:
    """One point per day (the day's last), inside [start, end]."""
    by_day: dict[datetime.date, Decimal] = {}
    for day, price in points:
        if start <= day <= end:
            by_day[day] = price
    return [PricePoint(day, price, currency) for day, price in sorted(by_day.items())]


class Yahoo:
    """Yahoo Finance's unofficial chart endpoint: `/v8/finance/chart/{symbol}` — no key."""

    source = models.PriceSource.YAHOO

    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.url = conf().get("yahoo_url", "https://query1.finance.yahoo.com").rstrip("/")

    def symbol_for(self, isin: str, figi: Figi | None) -> tuple[str, str | None] | None:
        if figi is None or figi.exchange not in EXCHANGES:
            return None
        return f"{figi.ticker.replace('/', '-')}{EXCHANGES[figi.exchange][0]}", figi.exchange

    async def _chart(self, symbol: str, params: dict) -> dict:
        body = await _get(self.http, f"{self.url}/v8/finance/chart/{symbol}", params=params)
        chart = body.get("chart") or {}
        if chart.get("error") or not chart.get("result"):
            raise PriceError(f"Yahoo has no chart for {symbol}: {(chart.get('error') or {}).get('description', 'no result')}")
        return chart["result"][0]

    async def history(self, listing: models.SecurityListing, start: datetime.date, end: datetime.date) -> list[PricePoint]:
        period1 = int(datetime.datetime.combine(start, datetime.time.min, tzinfo=datetime.timezone.utc).timestamp())
        period2 = int(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.timezone.utc).timestamp())
        result = await self._chart(listing.symbol, {"period1": period1, "period2": period2, "interval": "1d"})
        currency = (result.get("meta") or {}).get("currency") or "EUR"
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        points = [
            (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date(), Decimal(str(round(close, 4))))  # Yahoo sends binary floats
            for ts, close in zip(result.get("timestamp") or [], closes)
            if close is not None
        ]
        return _daily(points, currency, start, end)

    async def quote(self, listing: models.SecurityListing) -> Quote | None:
        meta = (await self._chart(listing.symbol, {"range": "1d", "interval": "1d"})).get("meta") or {}
        if meta.get("regularMarketPrice") is None:
            return None
        when = meta.get("regularMarketTime")
        return Quote(
            price=Decimal(str(meta["regularMarketPrice"])),
            currency=meta.get("currency") or "EUR",
            time=datetime.datetime.fromtimestamp(when, datetime.timezone.utc) if when else None,
            name=meta.get("longName") or meta.get("shortName"),
        )


class TwelveData:
    """Twelve Data's REST API (`/time_series`, `/quote`) — needs `prices.twelvedata_api_key`."""

    source = models.PriceSource.TWELVEDATA

    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.url = conf().get("twelvedata_url", "https://api.twelvedata.com").rstrip("/")
        self.key = conf().get("twelvedata_api_key")

    def symbol_for(self, isin: str, figi: Figi | None) -> tuple[str, str | None] | None:
        if figi is None or figi.exchange not in EXCHANGES:
            return None
        return figi.ticker, EXCHANGES[figi.exchange][1]

    def _params(self, listing: models.SecurityListing, **extra: Any) -> dict:
        if not self.key:
            raise PriceError("Twelve Data needs `prices.twelvedata_api_key`.")
        params = {"symbol": listing.symbol, "apikey": self.key, **extra}
        if listing.exchange:
            params["mic_code"] = listing.exchange
        return params

    @staticmethod
    def _check(body: dict, symbol: str) -> dict:
        if not isinstance(body, dict) or body.get("status") == "error":
            raise PriceError(f"Twelve Data refused {symbol}: {(body or {}).get('message', 'error')}", int((body or {}).get("code") or 0))
        return body

    async def history(self, listing: models.SecurityListing, start: datetime.date, end: datetime.date) -> list[PricePoint]:
        body = self._check(
            await _get(self.http, f"{self.url}/time_series", params=self._params(listing, interval="1day", start_date=start.isoformat(), end_date=end.isoformat(), outputsize="5000", order="asc")),
            listing.symbol,
        )
        currency = (body.get("meta") or {}).get("currency") or "EUR"
        points = [(datetime.date.fromisoformat(v["datetime"][:10]), Decimal(str(v["close"]))) for v in body.get("values") or [] if v.get("close")]
        return _daily(points, currency, start, end)

    async def quote(self, listing: models.SecurityListing) -> Quote | None:
        body = self._check(await _get(self.http, f"{self.url}/quote", params=self._params(listing)), listing.symbol)
        price = body.get("close") or body.get("price")
        if price is None:
            return None
        ts = body.get("timestamp")
        return Quote(price=Decimal(str(price)), currency=body.get("currency") or "EUR", time=datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc) if ts else None, name=body.get("name"))


class Scalable:
    """Scalable Capital's own prices, through the organization's active login (BrokerChart / BrokerQuote)."""

    source = models.PriceSource.SCALABLE
    TIMEFRAMES = [(7, "SEVEN_DAYS"), (31, "ONE_MONTH"), (92, "THREE_MONTHS"), (183, "SIX_MONTHS"), (366, "ONE_YEAR")]

    def __init__(self, organization_id: int) -> None:
        self.organization_id = organization_id

    def symbol_for(self, isin: str, figi: Figi | None) -> tuple[str, str | None] | None:
        return isin, None  # Scalable prices by ISIN

    async def _call(self, operation: str, variables_of) -> dict:  # noqa: ANN001 - (session, portfolio) -> variables
        from finance.scalable.client import ScalableClient
        from finance.scalable.tokens import session_for

        connection = (
            await models.BankConnection.objects.filter(organization_id=self.organization_id, provider=models.Provider.SCALABLE, status=models.ConnectionStatus.ACTIVE)
            # Only a login whose provider an admin lets price securities.
            .filter(bank_provider__enabled=True, bank_provider__capabilities__contains=[models.ProviderCapability.PRICES.value])
            .order_by("id")
            .afirst()
        )
        if connection is None:
            raise PriceError("No active Scalable login with security prices switched on in this organization.")
        portfolio = await models.AccountSyncer.objects.filter(connection=connection, account__kind=models.AccountKind.DEPOT).values_list("remote_id", flat=True).afirst()
        async with ScalableClient() as client:
            active = await session_for(connection.id, client)
            return await client.graphql(active.key, active.access_token, operation, variables_of(active, portfolio))

    async def history(self, listing: models.SecurityListing, start: datetime.date, end: datetime.date) -> list[PricePoint]:
        days = (datetime.date.today() - start).days + 1
        timeframe = next((name for limit, name in self.TIMEFRAMES if days <= limit), "MAX")
        data = await self._call("BrokerChart", lambda s, p: {"isin": listing.symbol, "timeFrames": [timeframe], "includeYearToDate": False})
        series = (data.get("timeSeriesBySecurity") or [{}])[0] or {}
        currency = series.get("currency") or "EUR"
        points = []
        for point in series.get("dataPoints") or []:
            when = ((point.get("timestampUtc") or {}).get("time") or "")[:10]
            if when and point.get("midPrice") is not None:
                points.append((datetime.date.fromisoformat(when), Decimal(str(point["midPrice"]))))
        return _daily(points, currency, start, end)

    async def quote(self, listing: models.SecurityListing) -> Quote | None:
        data = await self._call(
            "BrokerQuote", lambda s, p: {"accountId": s.person_id, "portfolioId": p, "isin": listing.symbol, "includeYearToDate": False, "quoteSource": None}
        )
        security = ((data.get("account") or {}).get("brokerPortfolio") or {}).get("security") or {}
        tick = security.get("quoteTick") or {}
        if tick.get("midPrice") is None:
            return None
        when = (tick.get("timestampUtc") or {}).get("time")
        return Quote(
            price=Decimal(str(tick["midPrice"])),
            currency=tick.get("currency") or "EUR",
            bid=_decimal(tick.get("bidPrice")),
            ask=_decimal(tick.get("askPrice")),
            time=datetime.datetime.fromisoformat(when.replace("Z", "+00:00")) if when else None,
            name=security.get("name"),
        )


def source_for(name: str, http: aiohttp.ClientSession, organization_id: int) -> Source | None:
    """The source implementation, or None when it is disabled or not configured."""
    c = conf()
    if name == models.PriceSource.SCALABLE:
        return Scalable(organization_id)
    if name == models.PriceSource.TWELVEDATA:
        return TwelveData(http) if c.get("twelvedata_api_key") else None
    if name == models.PriceSource.YAHOO:
        return Yahoo(http) if c.get("yahoo_enabled", True) else None
    return None
