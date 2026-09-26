"""Security prices behind one interface: Scalable, Twelve Data, Yahoo; OpenFIGI for listings.

fakescalable serves BrokerChart / BrokerQuote, fakemarket serves OpenFIGI, Yahoo and Twelve Data.
Nothing reaches the internet (``conftest.prices_offline``) and nothing is mocked.
"""

import datetime

import pytest
from django.utils import timezone

from finance import models
from tests.conftest import TWELVEDATA_KEY, holding, trade

pytestmark = pytest.mark.django_db(transaction=True)

ISIN = "IE00TEST0001"
TODAY = timezone.now().date()


def day(offset: int) -> str:
    return (TODAY + datetime.timedelta(days=offset)).isoformat()


def when(offset: int) -> str:
    return f"{day(offset)}T10:00:00.000Z"


FIGI = {ISIN: [{"ticker": "VWRA", "exchCode": "LN", "name": "VANG FTSE AW USDA"}, {"ticker": "VWCE", "exchCode": "GF", "name": "VANG FTSE AW"}, {"ticker": "VWCE", "exchCode": "GY", "name": "VANG FTSE AW"}]}
SCALABLE_PRICES = {ISIN: [(day(-d), 100.0 + (10 - d)) for d in range(10, 0, -1)]}  # 100 … 109 over the last 10 days
YAHOO = {"YAHOO:VWCE.DE": {"currency": "EUR", "name": "Vanguard FTSE All-World", "points": [[day(-d), 200.0 + (10 - d)] for d in range(10, 0, -1)]}}
TWELVE = {"TWELVEDATA:VWCE@XETR": {"currency": "EUR", "name": "VWCE", "points": [[day(-d), 300.0 + (10 - d)] for d in range(10, 0, -1)]}}

REFRESH = "mutation($f: Date) { refreshSecurityPrices(dateFrom: $f) { isin source symbol points error } }"
SERIES = "query($s: PriceSource) { securityPrices(isin: \"IE00TEST0001\", priceSource: $s) { source symbol currency points { date close } } }"


async def _depot(scalable_link, aexecute, fakescalable) -> dict:  # noqa: ANN001
    fakescalable.seed({"p1": {
        "cash": 0,
        "holdings": [holding(ISIN, 10, 109.0, 104.5)],
        "transactions": [trade(-500, when(-8), ISIN, quantity=5), trade(-525, when(-4), ISIN, quantity=5)],
    }})
    fakescalable.prices(SCALABLE_PRICES)
    connection = await scalable_link()
    await aexecute("mutation($id: ID!) { syncConnection(id: $id) { created } }", {"id": connection["id"]})
    return connection


async def test_a_depot_sync_stores_scalables_recent_prices(scalable_link, aexecute, fakescalable, fakemarket):
    await _depot(scalable_link, aexecute, fakescalable)

    series = (await aexecute(SERIES)).data["securityPrices"]

    assert series["source"] == "SCALABLE" and series["symbol"] == ISIN and series["currency"] == "EUR"
    assert [p["close"] for p in series["points"]] == [f"{100 + i}.00000000" for i in range(10)]


async def test_listings_resolve_through_openfigi_with_the_preferred_exchange(scalable_link, aexecute, fakescalable, fakemarket, settings):
    settings.PRICES = {**settings.PRICES, "twelvedata_api_key": TWELVEDATA_KEY}
    fakemarket.listings(FIGI)
    await _depot(scalable_link, aexecute, fakescalable)

    listings = (await aexecute("mutation { resolveSecurityListings { isin source symbol exchange name pinned } }")).data["resolveSecurityListings"]

    by_source = {l["source"]: l for l in listings}
    assert by_source["SCALABLE"] == {"isin": ISIN, "source": "SCALABLE", "symbol": ISIN, "exchange": None, "name": None, "pinned": False}
    assert (by_source["YAHOO"]["symbol"], by_source["YAHOO"]["exchange"]) == ("VWCE.DE", "GY")  # Xetra before Frankfurt and London
    assert (by_source["TWELVEDATA"]["symbol"], by_source["TWELVEDATA"]["exchange"]) == ("VWCE", "XETR")


async def test_refresh_fetches_every_source_and_is_idempotent(scalable_link, aexecute, fakescalable, fakemarket, settings):
    settings.PRICES = {**settings.PRICES, "twelvedata_api_key": TWELVEDATA_KEY}
    fakemarket.listings(FIGI)
    fakemarket.series({**YAHOO, **TWELVE})
    await _depot(scalable_link, aexecute, fakescalable)

    first = (await aexecute(REFRESH, {"f": day(-30)})).data["refreshSecurityPrices"]
    again = (await aexecute(REFRESH, {"f": day(-30)})).data["refreshSecurityPrices"]
    stored = await models.SecurityPrice.objects.acount()

    assert {(o["source"], o["symbol"], o["points"], o["error"]) for o in first} == {("SCALABLE", ISIN, 10, None), ("YAHOO", "VWCE.DE", 10, None), ("TWELVEDATA", "VWCE", 10, None)}
    assert first == again and stored == 30
    yahoo = (await aexecute(SERIES, {"s": "YAHOO"})).data["securityPrices"]
    twelve = (await aexecute(SERIES, {"s": "TWELVEDATA"})).data["securityPrices"]
    assert yahoo["points"][-1]["close"] == "209.00000000" and twelve["points"][0]["close"] == "300.00000000"
    assert all(e["query"].get("apikey") == TWELVEDATA_KEY for e in fakemarket.log() if e["path"] in ("/time_series", "/quote"))


async def test_sources_fall_back_in_preference_order_and_a_missing_key_skips_twelve_data(scalable_link, aexecute, fakescalable, fakemarket, settings):
    settings.PRICES = {**settings.PRICES, "sources": ["TWELVEDATA", "YAHOO", "SCALABLE"]}  # no key: Twelve Data is skipped
    fakemarket.listings(FIGI)
    fakemarket.series(YAHOO)
    await _depot(scalable_link, aexecute, fakescalable)
    outcomes = (await aexecute(REFRESH, {"f": day(-30)})).data["refreshSecurityPrices"]

    series = (await aexecute(SERIES)).data["securityPrices"]

    assert {o["source"] for o in outcomes} == {"YAHOO", "SCALABLE"}
    assert series["source"] == "YAHOO"  # first in the order that has prices


async def test_a_pinned_listing_is_never_re_resolved(scalable_link, aexecute, fakescalable, fakemarket):
    fakemarket.listings(FIGI)
    fakemarket.series({"YAHOO:VWRA.L": {"currency": "USD", "points": [[day(-1), 150.0]]}})
    await _depot(scalable_link, aexecute, fakescalable)

    await aexecute('mutation { pinSecurityListing(input: {isin: "ie00test0001", source: YAHOO, symbol: "VWRA.L", exchange: "LN"}) { pinned } }')
    await aexecute("mutation { resolveSecurityListings { symbol } }")
    await aexecute(REFRESH, {"f": day(-30)})
    series = (await aexecute(SERIES, {"s": "YAHOO"})).data["securityPrices"]

    assert (series["symbol"], series["currency"], series["points"]) == ("VWRA.L", "USD", [{"date": day(-1), "close": "150.00000000"}])


async def test_quotes_come_live_from_the_first_source_that_answers(scalable_link, aexecute, fakescalable, fakemarket):
    fakemarket.listings(FIGI)
    fakemarket.series(YAHOO)
    await _depot(scalable_link, aexecute, fakescalable)

    scalable = (await aexecute('{ securityQuote(isin: "IE00TEST0001") { source price bid ask currency } }')).data["securityQuote"]
    yahoo = (await aexecute('{ securityQuote(isin: "IE00TEST0001", priceSource: YAHOO) { source symbol price name } }')).data["securityQuote"]
    unknown = await aexecute('{ securityQuote(isin: "XX0000000000", priceSource: YAHOO) { price } }', allow_errors=True)

    assert scalable == {"source": "SCALABLE", "price": "109.0", "bid": "108.95", "ask": "109.05", "currency": "EUR"}
    assert yahoo == {"source": "YAHOO", "symbol": "VWCE.DE", "price": "209.0", "name": "Vanguard FTSE All-World"}
    assert unknown.errors[0].extensions["code"] == "NOT_FOUND"


async def test_the_depot_value_is_daily_from_positions_and_prices(scalable_link, aexecute, fakescalable, fakemarket):
    await _depot(scalable_link, aexecute, fakescalable)  # bought 5 at T−8 and 5 at T−4; holds 10 today

    history = (await aexecute("{ portfolioInsights { valuationHistory { date valuation invested gain } } }")).data["portfolioInsights"]["valuationHistory"]
    performance = (await aexecute(f'{{ positionPerformance(dateFrom: "{day(-10)}") {{ isin quantity firstClose lastClose change changeRatio valueChange source }} }}')).data["positionPerformance"]

    by_day = {p["date"]: p for p in history}
    assert day(-9) not in by_day  # nothing held yet
    assert by_day[day(-8)] == {"date": day(-8), "valuation": "510.00", "invested": "500.00", "gain": "10.00"}  # 5 × 102
    assert by_day[day(-4)]["valuation"] == "1060.00" and by_day[day(-4)]["invested"] == "1025.00"  # 10 × 106
    assert by_day[day(-1)]["valuation"] == "1090.00"  # 10 × 109
    assert performance == [{"isin": ISIN, "quantity": "10.00000000", "firstClose": "100.00000000", "lastClose": "109.00000000", "change": "9.00000000", "changeRatio": 0.09, "valueChange": "90.00", "source": "SCALABLE"}]


async def test_listings_and_series_stay_in_the_organization(scalable_link, aexecute, fakescalable, fakemarket, other_org_context):
    await _depot(scalable_link, aexecute, fakescalable)

    theirs = (await aexecute("{ securityListings { isin } }", context=other_org_context)).data["securityListings"]
    series = (await aexecute(SERIES, context=other_org_context)).data["securityPrices"]
    quote = await aexecute('{ securityQuote(isin: "IE00TEST0001", priceSource: SCALABLE) { price } }', context=other_org_context, allow_errors=True)

    assert theirs == [] and series["points"] == []  # the prices exist, but no listing of theirs points at them
    assert quote.errors[0].extensions["code"] == "NOT_FOUND"  # no Scalable login in that organization
