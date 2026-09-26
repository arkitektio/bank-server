"""The insight views on a fixed dataset — every number here is checkable by hand.

Window: 2026-07-01 … 2026-08-31 (62 days); PREVIOUS_PERIOD is 2026-04-30 … 2026-06-30.
Real Postgres + PostGIS, fakebank, fakescalable and fakegeo; nothing mocked.
"""

import datetime

import pytest
from django.utils import timezone

from finance import models
from tests.conftest import account, cash, holding, trade, tx

pytestmark = pytest.mark.django_db(transaction=True)

WINDOW = {"dateFrom": "2026-07-01", "dateTo": "2026-08-31"}
ROWS = [
    tx("-5.00", "2026-05-10", "Spar Dankt 3418"),  # the comparison window
    tx("-10.00", "2026-07-06", "Spar Dankt 3418"),  # Mon
    tx("-25.00", "2026-07-10", "Hofer Dankt"),  # Fri
    tx("-20.00", "2026-07-20", "Spar Dankt 3418"),  # Mon
    tx("2000.00", "2026-07-31", "Firma GmbH", remittance="Gehalt 07"),
    tx("-30.00", "2026-08-03", "Spar Dankt 3418"),  # Mon
    tx("-40.00", "2026-08-15", "Spar Dankt 3428"),  # Sat
    tx("-8.00", "2026-08-16", "MCDONALDS 01476"),  # Sun
    tx("2000.00", "2026-08-31", "Firma GmbH", remittance="Gehalt 08"),
]


async def _setup(link, aexecute, fakebank, context=None) -> dict:  # noqa: ANN001
    fakebank.scenario([account(f"ins-{fakebank.aspsp.replace(' ', '-')}", transactions=ROWS, balance="1000.00")])
    acc = (await link(context))["accounts"][0]["id"]
    await aexecute("mutation($id: ID!) { syncAccount(id: $id) { created } }", {"id": acc}, context=context)
    await aexecute('mutation { upsertMerchant(input: {key: "spar", name: "Spar", categoryKey: "food.groceries"}) { id } }', context=context)
    await aexecute('mutation { upsertMerchant(input: {key: "mcdonalds", name: "McDonald\'s", categoryKey: "food.eating_out"}) { id } }', context=context)
    return {"account": acc}


async def test_merchant_insights(link, aexecute, fakebank):
    await _setup(link, aexecute, fakebank)

    got = (await aexecute(
        """query($w: StatsWindowInput) { merchantInsights(merchant: {key: "spar"}, window: $w) {
          window { start end previousStart previousEnd }
          totals { currency expense count } previous { expense count }
          changes { metric currency current previous delta ratio }
          tickets { currency average median largest smallest }
          visits { visits firstVisit lastVisit averageDaysBetweenVisits }
          weekdays { weekday expense count }
          locations { location { storeCode } expense share }
          shareOfCategory { currency share }
        } }""",
        {"w": WINDOW},
    )).data["merchantInsights"]

    assert got["window"] == {"start": "2026-07-01", "end": "2026-08-31", "previousStart": "2026-04-30", "previousEnd": "2026-06-30"}
    assert got["totals"] == [{"currency": "EUR", "expense": "100.00", "count": 4}] and got["previous"] == [{"expense": "5.00", "count": 1}]
    assert {"metric": "EXPENSE", "currency": "EUR", "current": "100.00", "previous": "5.00", "delta": "95.00", "ratio": 19.0} in got["changes"]
    assert got["tickets"] == [{"currency": "EUR", "average": "25.00", "median": "25.00", "largest": "40.00", "smallest": "10.00"}]
    assert got["visits"] == {"visits": 4, "firstVisit": "2026-07-06", "lastVisit": "2026-08-15", "averageDaysBetweenVisits": 13.33}
    assert got["weekdays"] == [{"weekday": 1, "expense": "60.00", "count": 3}, {"weekday": 6, "expense": "40.00", "count": 1}]
    assert got["locations"] == [{"location": {"storeCode": "3418"}, "expense": "60.00", "share": 0.6}, {"location": {"storeCode": "3428"}, "expense": "40.00", "share": 0.4}]
    assert got["shareOfCategory"] == [{"currency": "EUR", "share": 0.8}]  # Hofer (25) is Groceries too


async def test_category_insights_roll_up_children(link, aexecute, fakebank):
    await _setup(link, aexecute, fakebank)
    food = str((await models.Category.objects.aget(key="food")).id)
    await aexecute('mutation($c: ID!) { createBudget(input: {category: $c, amount: "200.00", currency: "EUR"}) { id } }', {"c": food})

    got = (await aexecute(
        """query($c: ID!, $w: StatsWindowInput) { categoryInsights(category: $c, window: $w) {
          totals { expense count } monthlyAverage { expense count } shareOfSpending { share }
          children { category { key } expense share } topMerchants { merchant { key } expense }
          topCounterparties { counterparty total } budgets { budgeted } tickets { median }
        } }""",
        {"c": food, "w": WINDOW},
    )).data["categoryInsights"]

    assert got["totals"] == [{"expense": "133.00", "count": 6}]
    assert got["monthlyAverage"] == [{"expense": "66.50", "count": 2}]
    assert got["shareOfSpending"] == [{"share": 1.0}]
    assert got["children"] == [
        {"category": {"key": "food.groceries"}, "expense": "125.00", "share": 0.9398},
        {"category": {"key": "food.eating_out"}, "expense": "8.00", "share": 0.0602},
    ]
    assert got["topMerchants"] == [{"merchant": {"key": "spar"}, "expense": "100.00"}, {"merchant": {"key": "mcdonalds"}, "expense": "8.00"}]
    assert got["topCounterparties"][0] == {"counterparty": "Spar Dankt 3418", "total": "60.00"}
    assert got["budgets"] == [{"budgeted": "200.00"}]


async def test_period_overview(link, aexecute, fakebank):
    await _setup(link, aexecute, fakebank)

    got = (await aexecute(
        """query($w: StatsWindowInput) { periodOverview(window: $w) {
          totals { income expense } savingsRate { share }
          categoryMovers { category { key } change { current previous delta } }
          merchantMovers { merchant { key } change { delta } }
          largestTransactions { counterparty amount } newMerchants { key }
          daily { date expense income } weekdays { weekday count }
        } }""",
        {"w": WINDOW},
    )).data["periodOverview"]
    last_year = (await aexecute('query { periodOverview(window: {dateFrom: "2026-07-01", dateTo: "2026-08-31"}, compareTo: SAME_PERIOD_LAST_YEAR) { window { previousStart previousEnd } previous { count } } }')).data["periodOverview"]

    assert got["totals"] == [{"income": "4000.00", "expense": "133.00"}]
    assert got["savingsRate"] == [{"share": 0.9667}]  # (4000 − 133) / 4000
    assert got["categoryMovers"][0] == {"category": {"key": "food.groceries"}, "change": {"current": "125.00", "previous": "5.00", "delta": "120.00"}}
    assert got["merchantMovers"][0] == {"merchant": {"key": "spar"}, "change": {"delta": "95.00"}}
    assert got["largestTransactions"][0] == {"counterparty": "Spar Dankt 3428", "amount": "-40.00"}
    assert got["newMerchants"] == [{"key": "mcdonalds"}]  # Spar was first seen in May
    assert [d["date"] for d in got["daily"]] == ["2026-07-06", "2026-07-10", "2026-07-20", "2026-07-31", "2026-08-03", "2026-08-15", "2026-08-16", "2026-08-31"]
    assert {w["weekday"]: w["count"] for w in got["weekdays"]} == {1: 4, 5: 2, 6: 1, 7: 1}  # 2026-08-31 (salary) is a Monday
    assert last_year["window"] == {"previousStart": "2025-07-01", "previousEnd": "2025-08-31"} and last_year["previous"] == []


async def test_area_insights_and_spending_grid(link, aexecute, fakebank, fakegeo, other_org_context):
    fakegeo.places([
        {"name": "Spar", "lat": 48.1985, "lon": 16.3495, "road": "Mariahilfer Straße", "city": "Wien", "country_code": "at"},
        {"name": "Spar", "lat": 47.0707, "lon": 15.4395, "road": "Annenstraße", "city": "Graz", "country_code": "at"},
    ])
    await _setup(link, aexecute, fakebank)
    await _setup(link, aexecute, fakebank, context=other_org_context)  # the same rows, located the same, in another organization
    for context in (None, other_org_context):
        stores = {l.store_code: str(l.id) async for l in models.MerchantLocation.objects.filter(merchant__organization__slug="other_org" if context else "static_org")}
        await aexecute('mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: "Mariahilfer Wien") { id } }', {"id": stores["3418"]}, context=context)
        await aexecute('mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: "Annenstraße Graz") { id } }', {"id": stores["3428"]}, context=context)
    vienna = {"near": {"latitude": 48.2, "longitude": 16.35, "radiusMeters": 2000}}
    austria = {"south": 46.3, "west": 9.5, "north": 49.1, "east": 17.2}

    area = (await aexecute("query($a: AreaInput!, $w: StatsWindowInput) { areaInsights(area: $a, window: $w) { totals { expense count } merchants { merchant { key } expense share } locations { location { storeCode } } } }", {"a": vienna, "w": WINDOW})).data["areaInsights"]
    grid = (await aexecute("query($b: BoundsInput!, $w: StatsWindowInput) { spendingGrid(within: $b, cellMeters: 50000, window: $w) { type cellMeters features { type geometry { coordinates } properties { expense count merchants locations } } } }", {"b": austria, "w": WINDOW})).data["spendingGrid"]
    place = (await aexecute("query($id: ID!, $w: StatsWindowInput) { locationInsights(location: $id, window: $w) { totals { expense } visits { visits } } }", {"id": stores["3428"], "w": WINDOW}, context=other_org_context)).data["locationInsights"]

    assert area == {"totals": [{"expense": "60.00", "count": 3}], "merchants": [{"merchant": {"key": "spar"}, "expense": "60.00", "share": 1.0}], "locations": [{"location": {"storeCode": "3418"}}]}
    assert grid["type"] == "FeatureCollection" and grid["cellMeters"] == 50000
    cells = sorted((f["properties"]["expense"], f["properties"]["count"]) for f in grid["features"])
    assert cells == [("40.00", 1), ("60.00", 3)]  # this organization's rows only: Graz, Vienna
    assert all(f["type"] == "Feature" and len(f["geometry"]["coordinates"]) == 2 for f in grid["features"])
    assert place == {"totals": [{"expense": "40.00"}], "visits": {"visits": 1}}


async def test_account_insights(link, aexecute, fakebank):
    ids = await _setup(link, aexecute, fakebank)

    got = (await aexecute(
        "query($a: ID!, $w: StatsWindowInput) { accountInsights(account: $a, window: $w, limit: 2) { totals { income expense } lowestBalance { date amount } highestBalance { date amount } largestIn { amount } largestOut { amount } topMerchants { merchant { key } } } }",
        {"a": ids["account"], "w": {"dateFrom": "2026-07-01", "dateTo": timezone.now().date().isoformat()}},
    )).data["accountInsights"]

    assert got["totals"] == [{"income": "4000.00", "expense": "133.00"}]
    assert got["largestIn"] == [{"amount": "2000.00"}, {"amount": "2000.00"}]
    assert got["largestOut"] == [{"amount": "-40.00"}, {"amount": "-30.00"}]
    assert got["highestBalance"]["amount"] == "1000.00"  # the reported balance, after the last salary
    # Walked back from the reported 1000: before both salaries but after the first 30 of spending.
    assert got["lowestBalance"] == {"date": "2026-07-20", "amount": "-2922.00"}
    assert [m["merchant"]["key"] for m in got["topMerchants"]] == ["spar", "mcdonalds"]


async def test_recurring_insights(link, aexecute, fakebank):
    await _setup(link, aexecute, fakebank)
    acc = await models.BankAccount.objects.afirst()
    today = timezone.now().date()
    weekly = await models.RecurringPayment.objects.acreate(organization_id=acc.organization_id, account=acc, key="w", label="Gym", amount="-10.00", currency="EUR", interval_days=7, last_seen=today - datetime.timedelta(days=2), next_expected=today + datetime.timedelta(days=5), status="CONFIRMED")
    await models.RecurringPayment.objects.acreate(organization_id=acc.organization_id, account=acc, key="r", label="Rent", amount="-800.00", currency="EUR", interval_days=30, last_seen=today - datetime.timedelta(days=40), next_expected=today - datetime.timedelta(days=10), status="CONFIRMED")
    await models.RecurringPayment.objects.acreate(organization_id=acc.organization_id, account=acc, key="d", label="Detected only", amount="-5.00", currency="EUR", interval_days=30, last_seen=today, next_expected=today + datetime.timedelta(days=30), status="DETECTED")
    spar_rows = [t async for t in models.Transaction.objects.filter(counterparty="Spar Dankt 3418").order_by("booking_date")]
    await weekly.transactions.aadd(spar_rows[-2], spar_rows[-1])  # 20.00 then 30.00

    got = (await aexecute("{ recurringInsights { monthlyCommitted { expense count } byCategory { category { key } monthly } dueSoon { label } missed { label } priceChanges { recurring { label } previous current delta } } }")).data["recurringInsights"]
    detected = (await aexecute("{ recurringInsights(includeDetected: true) { monthlyCommitted { count } } }")).data["recurringInsights"]

    assert got["monthlyCommitted"] == [{"expense": "855.22", "count": 2}]  # 10 × 30.44 / 7 = 43.49, + 800 × 30.44 / 30 = 811.73
    assert {c["monthly"] for c in got["byCategory"]} == {"43.49", "811.73"}
    assert got["dueSoon"] == [{"label": "Gym"}] and got["missed"] == [{"label": "Rent"}]
    assert got["priceChanges"] == [{"recurring": {"label": "Gym"}, "previous": "-20.00", "current": "-30.00", "delta": "-10.00"}]
    assert detected["monthlyCommitted"] == [{"count": 3}]


async def test_portfolio_insights(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": {
        "cash": 100,
        "holdings": [holding("IE00TEST0001", 10, 100.0, 80.0), holding("US00TEST0002", 5, 20.0, 25.0, kind="STOCK")],
        "transactions": [
            cash(12.34, "2026-09-20T00:00:00.000Z", "DISTRIBUTION"),
            cash(-3.00, "2026-09-20T00:00:00.000Z", "TAX"),
            trade(-800, "2026-09-10T10:00:00.000Z", "IE00TEST0001", quantity=10),
            trade(-125, "2026-09-11T10:00:00.000Z", "US00TEST0002", quantity=5),
            cash(1000, "2026-09-02T00:00:00.000Z", "DEPOSIT"),
        ],
    }})
    connection = await scalable_link()
    await aexecute("mutation($id: ID!) { syncConnection(id: $id) { created } }", {"id": connection["id"]})

    got = (await aexecute("{ portfolioInsights { valuationHistory { valuation invested gain } allocation { securityType valuation share } valuation { amount } costBasis { amount } unrealizedGain { amount } positions { isin } income { year kind amount count } } }")).data["portfolioInsights"]

    assert got["valuationHistory"] == [{"valuation": "1100.00", "invested": "925.00", "gain": "175.00"}]
    assert got["allocation"] == [{"securityType": "ETF", "valuation": "1000.00", "share": 0.9091}, {"securityType": "STOCK", "valuation": "100.00", "share": 0.0909}]
    assert (got["valuation"], got["costBasis"], got["unrealizedGain"]) == ([{"amount": "1100.00"}], [{"amount": "925.00"}], [{"amount": "175.00"}])
    assert [p["isin"] for p in got["positions"]] == ["IE00TEST0001", "US00TEST0002"]
    assert got["income"] == [{"year": 2026, "kind": "DISTRIBUTION", "amount": "12.34", "count": 1}, {"year": 2026, "kind": "TAX", "amount": "-3.00", "count": 1}]


async def test_views_resolve_only_in_the_callers_organization(link, aexecute, fakebank, other_org_context):
    ids = await _setup(link, aexecute, fakebank)
    food = str((await models.Category.objects.aget(key="food", organization__slug="static_org")).id)

    for query, variables in [
        ('{ merchantInsights(merchant: {key: "spar"}) { totals { count } } }', {}),
        ("query($c: ID!) { categoryInsights(category: $c) { totals { count } } }", {"c": food}),
        ("query($a: ID!) { accountInsights(account: $a) { totals { count } } }", {"a": ids["account"]}),
    ]:
        result = await aexecute(query, variables, context=other_org_context, allow_errors=True)
        assert result.errors[0].extensions["code"] == "NOT_FOUND", query
    overview = (await aexecute("query($w: StatsWindowInput) { periodOverview(window: $w) { totals { count } } }", {"w": WINDOW}, context=other_org_context)).data["periodOverview"]
    assert overview["totals"] == []
