"""Merchants end to end: aliases, discovered stores, merchant categories, PostGIS `near`, geocoding.

Real Postgres with PostGIS (the daten image), fakebank syncs, fakegeo as the geocoder — no mocks.
"""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

SYNC = "mutation($id: ID!) { syncAccount(id: $id) { created } }"
TXS = "query($a: ID!) { transactions(filters: {accounts: [$a]}, ordering: [{bookingDate: ASC}]) { id counterparty merchantSource merchant { id name } merchantLocation { id storeCode } categorySource category { key } } }"
CREATE = "mutation($input: CreateMerchantInput!) { createMerchant(input: $input) { id name key aliases { pattern } locations { storeCode source } } }"

SHOPS = [
    tx("-12.00", "2026-09-01", "Spar Dankt 3418"),
    tx("-30.00", "2026-09-03", "Spar Dankt 3428"),
    tx("-8.00", "2026-09-05", "EUROSPAR ES1 BZ VITIPE"),
    tx("-500.00", "2026-09-06", "Sparkasse Rate"),
    tx("-6.50", "2026-09-07", "MCDONALDS 01476"),
    tx("-7.50", "2026-09-08", "MCDONALDS 01476"),
]


async def _bank(link, aexecute, fakebank, rows, context=None) -> tuple[str, str]:  # noqa: ANN001
    ident = f"m-{fakebank.aspsp.replace(' ', '-')}"
    fakebank.scenario([account(ident, transactions=rows)])
    acc = (await link(context))["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc}, context=context)
    return acc, ident


async def _rows(aexecute, acc, context=None) -> dict[str, list[dict]]:  # noqa: ANN001
    out: dict[str, list[dict]] = {}
    for t in (await aexecute(TXS, {"a": acc}, context=context)).data["transactions"]:
        out.setdefault(t["counterparty"], []).append(t)
    return out


def _ids(rows: dict[str, list[dict]], *names: str) -> list[str]:
    return [t["id"] for name in names for t in rows[name]]


async def test_a_merchant_from_transactions_learns_its_alias_and_discovers_stores(link, aexecute, fakebank):
    acc, ident = await _bank(link, aexecute, fakebank, SHOPS)
    rows = await _rows(aexecute, acc)

    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "fromTransactions": _ids(rows, "Spar Dankt 3418", "Spar Dankt 3428")}})).data["createMerchant"]
    rows = await _rows(aexecute, acc)

    assert [a["pattern"] for a in spar["aliases"]] == ["spar"]
    assert sorted((l["storeCode"], l["source"]) for l in spar["locations"]) == [("3418", "DISCOVERED"), ("3428", "DISCOVERED")]
    assert rows["Spar Dankt 3418"][0]["merchant"]["name"] == "Spar" and rows["Spar Dankt 3418"][0]["merchantSource"] == "AUTO"
    assert rows["Spar Dankt 3418"][0]["merchantLocation"]["storeCode"] == "3418"
    # "eurospar" and "sparkasse" do not start with the word "spar"
    assert rows["EUROSPAR ES1 BZ VITIPE"][0]["merchant"] is None and rows["Sparkasse Rate"][0]["merchant"] is None

    # A later sync of the same store lands on the same location.
    fakebank.set_account(ident, transactions=[*SHOPS, tx("-3.00", "2026-09-10", "Spar Dankt 3418")])
    await aexecute(SYNC, {"id": acc})
    again = (await _rows(aexecute, acc))["Spar Dankt 3418"]
    assert len(again) == 2 and {t["merchantLocation"]["id"] for t in again} == {rows["Spar Dankt 3418"][0]["merchantLocation"]["id"]}
    assert await models.MerchantLocation.objects.filter(merchant_id=spar["id"]).acount() == 2


async def test_the_longest_alias_wins(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [*SHOPS, tx("-40.00", "2026-09-09", "SPAR GOURMET 1070")])
    await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})
    await aexecute(CREATE, {"input": {"name": "Spar Gourmet", "aliases": ["Spar Gourmet"]}})
    await aexecute(CREATE, {"input": {"name": "Eurospar", "aliases": ["EUROSPAR"]}})

    rows = await _rows(aexecute, acc)

    assert rows["SPAR GOURMET 1070"][0]["merchant"]["name"] == "Spar Gourmet"
    assert rows["Spar Dankt 3418"][0]["merchant"]["name"] == "Spar"
    assert rows["EUROSPAR ES1 BZ VITIPE"][0]["merchant"]["name"] == "Eurospar"
    assert rows["Sparkasse Rate"][0]["merchant"] is None


async def test_a_merchants_category_categorizes_after_rules_and_never_over_manual(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    eating = str((await models.Category.objects.aget(key="food.eating_out")).id)
    leisure = str((await models.Category.objects.aget(key="leisure")).id)
    rows = await _rows(aexecute, acc)
    first, second = _ids(rows, "MCDONALDS 01476")
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": [second], "c": leisure})

    mcd = (await aexecute(CREATE, {"input": {"name": "McDonald's", "aliases": ["mcdonalds"], "category": eating}})).data["createMerchant"]
    rows = {t["id"]: t for t in (await aexecute(TXS, {"a": acc})).data["transactions"]}

    assert (rows[first]["categorySource"], rows[first]["category"]["key"]) == ("MERCHANT", "food.eating_out")
    assert (rows[second]["categorySource"], rows[second]["category"]["key"]) == ("MANUAL", "leisure")

    # A rule overrides the merchant's category.
    await aexecute('mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "mcdonalds"}) { id } }', {"c": leisure})
    ruled = (await aexecute("query($t: ID!) { transaction(id: $t) { categorySource category { key } } }", {"t": first})).data["transaction"]
    assert (ruled["categorySource"], ruled["category"]["key"]) == ("RULE", "leisure")

    # Without the rule and without the merchant's category, the row is handed back.
    rule = await models.CategoryRule.objects.aget(pattern="mcdonalds")
    await aexecute("mutation($id: ID!) { deleteCategoryRule(id: $id) }", {"id": str(rule.id)})
    back = (await aexecute("query($t: ID!) { transaction(id: $t) { categorySource } }", {"t": first})).data["transaction"]
    assert back["categorySource"] == "MERCHANT"
    await aexecute("mutation($id: ID!) { updateMerchant(input: {id: $id, category: null}) { id } }", {"id": mcd["id"]})
    cleared = (await aexecute("query($t: ID!) { transaction(id: $t) { categorySource } }", {"t": first})).data["transaction"]
    assert cleared["categorySource"] in ("NONE", "SEMANTIC")


async def test_candidates_list_recurring_counterparties_without_a_merchant(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, SHOPS)

    candidates = (await aexecute("{ merchantCandidates { key count samples storeCodes transactionIds totals { currency amount } } }")).data["merchantCandidates"]

    by_key = {c["key"]: c for c in candidates}
    assert set(by_key) == {"spar", "mcdonalds"}
    assert by_key["spar"]["count"] == 2 and by_key["spar"]["storeCodes"] == 2
    assert by_key["mcdonalds"]["totals"] == [{"currency": "EUR", "amount": "-14.00"}]


async def test_geocoding_fills_a_discovered_store_and_near_finds_it(link, aexecute, fakebank, fakegeo):
    fakegeo.places([
        {"name": "Spar", "lat": 48.1985, "lon": 16.3495, "road": "Mariahilfer Straße", "house_number": "120", "postcode": "1070", "city": "Wien", "state": "Wien", "country_code": "at", "osm_id": 42},
        {"name": "Spar", "lat": 47.0707, "lon": 15.4395, "road": "Annenstraße", "house_number": "1", "postcode": "8020", "city": "Graz", "state": "Steiermark", "country_code": "at", "osm_id": 43},
    ])
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    rows = await _rows(aexecute, acc)
    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "fromTransactions": _ids(rows, "Spar Dankt 3418", "Spar Dankt 3428")}})).data["createMerchant"]
    stores = {l.store_code: l.id async for l in models.MerchantLocation.objects.filter(merchant_id=spar["id"])}

    wien = (await aexecute("mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: \"Mariahilfer Straße 120 Wien\") { city street postalCode country latitude longitude source osmId } }", {"id": str(stores["3418"])})).data["geocodeMerchantLocation"]
    await aexecute("mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: \"Annenstraße Graz\") { id } }", {"id": str(stores["3428"])})

    here = {"latitude": 48.2000, "longitude": 16.3500, "radiusMeters": 2000}
    locations = (await aexecute("query($n: NearInput!) { merchantLocations(filters: {near: $n}) { storeCode distanceMeters } }", {"n": here})).data["merchantLocations"]
    merchants = (await aexecute("query($n: NearInput!) { merchants(filters: {near: $n}) { name distanceMeters } }", {"n": here})).data["merchants"]
    txs = (await aexecute("query($n: NearInput!) { transactions(filters: {near: $n}) { counterparty } }", {"n": here})).data["transactions"]
    far = (await aexecute("query($n: NearInput!) { merchantLocations(filters: {near: $n}) { storeCode } }", {"n": {**here, "radiusMeters": 300000}})).data["merchantLocations"]
    unlocated = (await aexecute("{ merchantLocations(filters: {unlocated: true}) { id } }")).data["merchantLocations"]

    assert (wien["city"], wien["street"], wien["postalCode"], wien["country"], wien["source"], wien["osmId"]) == ("Wien", "Mariahilfer Straße 120", "1070", "AT", "GEOCODED", "N42")
    assert [l["storeCode"] for l in locations] == ["3418"] and 100 < locations[0]["distanceMeters"] < 400
    assert [m["name"] for m in merchants] == ["Spar"] and merchants[0]["distanceMeters"] == pytest.approx(locations[0]["distanceMeters"])
    assert [t["counterparty"] for t in txs] == ["Spar Dankt 3418"]
    assert [l["storeCode"] for l in far] == ["3418", "3428"]  # nearest first
    assert unlocated == []
    assert all(entry["user_agent"] for entry in fakegeo.log())


async def test_merge_moves_aliases_locations_and_transactions(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [*SHOPS, tx("-9.00", "2026-09-09", "SPAR MARKT 3418")])
    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar dankt"]}})).data["createMerchant"]
    markt = (await aexecute(CREATE, {"input": {"name": "Spar Markt", "aliases": ["spar markt"]}})).data["createMerchant"]

    merged = (await aexecute("mutation($s: ID!, $t: ID!) { mergeMerchants(merchant: $s, into: $t) { name aliases { pattern } locations { storeCode } } }", {"s": markt["id"], "t": spar["id"]})).data["mergeMerchants"]
    rows = await _rows(aexecute, acc)

    assert sorted(a["pattern"] for a in merged["aliases"]) == ["spar", "spar markt"]  # "Spar Dankt" normalizes to "spar"
    assert sorted(l["storeCode"] for l in merged["locations"]) == ["3418", "3428"]  # the two 3418s became one
    assert rows["SPAR MARKT 3418"][0]["merchantLocation"]["id"] == rows["Spar Dankt 3418"][0]["merchantLocation"]["id"]
    assert not await models.Merchant.objects.filter(id=markt["id"]).aexists()


async def test_manual_assignment_survives_rematching_and_null_hands_back(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})
    bank = (await aexecute(CREATE, {"input": {"name": "Sparkasse", "aliases": ["sparkasse"]}})).data["createMerchant"]
    row = (await _rows(aexecute, acc))["Spar Dankt 3418"][0]["id"]

    await aexecute("mutation($ids: [ID!]!, $m: ID) { assignMerchant(input: {transactions: $ids, merchant: {id: $m}}) { id } }", {"ids": [row], "m": bank["id"]})
    await aexecute('mutation($m: ID!) { addMerchantAlias(merchant: $m, text: "Sparkasse Rate") { pattern } }', {"m": bank["id"]})  # re-matches everything
    await aexecute("mutation { reapplyRules }")  # so does this
    pinned = (await aexecute("query($t: ID!) { transaction(id: $t) { merchant { name } merchantSource } }", {"t": row})).data["transaction"]
    await aexecute("mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids}) { id } }", {"ids": [row]})
    back = (await aexecute("query($t: ID!) { transaction(id: $t) { merchant { name } merchantSource } }", {"t": row})).data["transaction"]

    assert pinned == {"merchant": {"name": "Sparkasse"}, "merchantSource": "MANUAL"}
    assert back == {"merchant": {"name": "Spar"}, "merchantSource": "AUTO"}


async def test_an_alias_means_one_merchant(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, SHOPS)
    await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})

    clash = await aexecute(CREATE, {"input": {"name": "Other", "aliases": ["SPAR"]}}, allow_errors=True)
    empty = await aexecute(CREATE, {"input": {"name": "Numbers", "aliases": ["1234 DANKT"]}}, allow_errors=True)
    boilerplate = await aexecute(CREATE, {"input": {"name": "Spar Two", "aliases": ["Spar Dankt"]}}, allow_errors=True)  # normalizes to "spar"

    assert clash.errors[0].extensions["code"] == "VALIDATION_ERROR" and "already means 'Spar'" in clash.errors[0].message
    assert empty.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert boilerplate.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert not await models.Merchant.objects.filter(name="Other").aexists()


async def test_merchants_stay_in_their_organization(link, aexecute, fakebank, other_org_context):
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    other_acc, _ = await _bank(link, aexecute, fakebank, SHOPS, context=other_org_context)
    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})).data["createMerchant"]
    await models.MerchantLocation.objects.filter(merchant_id=spar["id"]).aupdate(latitude=48.2, longitude=16.35)

    theirs = await _rows(aexecute, other_acc, context=other_org_context)
    probe = await aexecute("query($id: ID!) { merchant(id: $id) { name } }", {"id": spar["id"]}, context=other_org_context, allow_errors=True)
    listed = (await aexecute("{ merchants { id } merchantLocations(filters: {near: {latitude: 48.2, longitude: 16.35}}) { id } }", context=other_org_context)).data
    hijack = await aexecute("mutation($ids: [ID!]!, $m: ID) { assignMerchant(input: {transactions: $ids, merchant: {id: $m}}) { id } }", {"ids": [theirs["Spar Dankt 3418"][0]["id"]], "m": spar["id"]}, context=other_org_context, allow_errors=True)
    by_key = await aexecute('mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids, merchant: {key: "spar"}}) { id } }', {"ids": [theirs["Spar Dankt 3418"][0]["id"]]}, context=other_org_context, allow_errors=True)

    assert theirs["Spar Dankt 3418"][0]["merchant"] is None  # another organization's aliases never match here
    assert probe.errors[0].extensions["code"] == "NOT_FOUND"
    assert listed == {"merchants": [], "merchantLocations": []}
    assert hijack.errors[0].extensions["code"] == "NOT_FOUND"
    assert by_key.errors[0].extensions["code"] == "NOT_FOUND"  # keys resolve in the caller's organization only


async def test_spending_by_merchant(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, SHOPS)
    await aexecute(CREATE, {"input": {"name": "McDonald's", "aliases": ["mcdonalds"]}})

    totals = (await aexecute("{ spendingByMerchant { merchant { name } currency expense count } }")).data["spendingByMerchant"]

    by_name = {(t["merchant"] or {}).get("name"): t for t in totals}
    assert by_name["McDonald's"] == {"merchant": {"name": "McDonald's"}, "currency": "EUR", "expense": "14.00", "count": 2}
    assert by_name[None]["count"] == 4


GEOJSON = """
query($f: MerchantLocationFilter) { merchantLocationsGeojson(filters: $f) {
  type bbox
  features { type id geometry { type coordinates } properties { name storeCode merchantName city transactionCount lastVisit net currency distanceMeters } }
} }
"""


async def test_located_places_come_as_typed_geojson(link, aexecute, fakebank, fakegeo):
    fakegeo.places([
        {"name": "Spar", "lat": 48.1985, "lon": 16.3495, "road": "Mariahilfer Straße", "postcode": "1070", "city": "Wien", "country_code": "at"},
        {"name": "Spar", "lat": 47.0707, "lon": 15.4395, "road": "Annenstraße", "postcode": "8020", "city": "Graz", "country_code": "at"},
    ])
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    rows = await _rows(aexecute, acc)
    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "fromTransactions": _ids(rows, "Spar Dankt 3418", "Spar Dankt 3428")}})).data["createMerchant"]
    stores = {l.store_code: str(l.id) async for l in models.MerchantLocation.objects.filter(merchant_id=spar["id"])}
    await aexecute('mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: "Mariahilfer Wien") { id } }', {"id": stores["3418"]})
    await aexecute('mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: "Annenstraße Graz") { id } }', {"id": stores["3428"]})

    everything = (await aexecute(GEOJSON)).data["merchantLocationsGeojson"]
    vienna = (await aexecute(GEOJSON, {"f": {"within": {"south": 48.1, "west": 16.2, "north": 48.3, "east": 16.5}}})).data["merchantLocationsGeojson"]
    near = (await aexecute(GEOJSON, {"f": {"near": {"latitude": 48.2, "longitude": 16.35, "radiusMeters": 1000}}})).data["merchantLocationsGeojson"]

    assert everything["type"] == "FeatureCollection" and len(everything["features"]) == 2
    feature = next(f for f in vienna["features"])
    assert feature["type"] == "Feature" and feature["geometry"] == {"type": "Point", "coordinates": [16.3495, 48.1985]}  # [lon, lat]
    assert feature["properties"] | {"distanceMeters": None} == {
        "name": "Spar 3418", "storeCode": "3418", "merchantName": "Spar", "city": "Wien",
        "transactionCount": 1, "lastVisit": "2026-09-01", "net": "-12.00", "currency": "EUR", "distanceMeters": None,
    }
    assert len(vienna["features"]) == 1 and vienna["bbox"] == [16.3495, 48.1985, 16.3495, 48.1985]
    assert [f["properties"]["storeCode"] for f in near["features"]] == ["3418"] and 100 < near["features"][0]["properties"]["distanceMeters"] < 400



async def test_link_by_key_and_store_number_and_upsert(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [*SHOPS, tx("-20.00", "2026-09-09", "Kiosk am Eck")])
    kiosk = (await _rows(aexecute, acc))["Kiosk am Eck"][0]["id"]

    created = (await aexecute('mutation { upsertMerchant(input: {key: "Kiosk", name: "Kiosk am Eck", categoryKey: "food.eating_out"}) { id key name category { key } aliases { pattern } } }')).data["upsertMerchant"]
    updated = (await aexecute('mutation { upsertMerchant(input: {key: "kiosk", website: "https://kiosk.test", aliases: ["Eckkiosk"]}) { id name website aliases { pattern } } }')).data["upsertMerchant"]
    renamed = (await aexecute('mutation($id: ID!) { updateMerchant(input: {id: $id, name: "Der Kiosk"}) { key name } }', {"id": created["id"]})).data["updateMerchant"]
    linked = (await aexecute(
        'mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids, merchant: {key: "Kiosk"}, location: {storeCode: "0815"}}) { merchantSource merchant { key } merchantLocation { storeCode source } category { key } } }',
        {"ids": [kiosk]},
    )).data["assignMerchant"]
    both = await aexecute('mutation($ids: [ID!]!, $m: ID!) { assignMerchant(input: {transactions: $ids, merchant: {id: $m, key: "kiosk"}}) { id } }', {"ids": [kiosk], "m": created["id"]}, allow_errors=True)
    missing = await aexecute('mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids, merchant: {key: "nope"}}) { id } }', {"ids": [kiosk]}, allow_errors=True)

    assert (created["key"], created["name"], created["category"]["key"], [a["pattern"] for a in created["aliases"]]) == ("kiosk", "Kiosk am Eck", "food.eating_out", ["kiosk"])
    assert updated["id"] == created["id"] and updated["website"] == "https://kiosk.test" and sorted(a["pattern"] for a in updated["aliases"]) == ["eckkiosk", "kiosk"]
    assert renamed == {"key": "kiosk", "name": "Der Kiosk"}  # the key survives a rename
    assert linked == [{"merchantSource": "MANUAL", "merchant": {"key": "kiosk"}, "merchantLocation": {"storeCode": "0815", "source": "DISCOVERED"}, "category": {"key": "food.eating_out"}}]
    assert both.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert missing.errors[0].extensions["code"] == "NOT_FOUND"


MRULE = "mutation($input: CreateMerchantRuleInput!) { createMerchantRule(input: $input) { id merchant { key } location { storeCode } } }"


async def test_merchant_rules_map_before_aliases_and_never_over_manual(link, aexecute, fakebank, other_org_context):
    rows = [
        *SHOPS,
        tx("-850.00", "2026-09-01", "Hausverwaltung Muster", iban="AT611904300234573201", remittance="Miete Oktober"),
        tx("-4.20", "2026-09-02", "SumUp *Stand 12", remittance="Naschmarkt Obst Huber"),
    ]
    acc, _ = await _bank(link, aexecute, fakebank, rows)
    await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})
    await aexecute('mutation { upsertMerchant(input: {key: "landlord", name: "Landlord", categoryKey: "housing.rent"}) { id } }')
    await aexecute('mutation { upsertMerchant(input: {key: "obst huber", name: "Obst Huber", aliases: []}) { id } }')
    await aexecute('mutation { upsertMerchant(input: {key: "spar gourmet", name: "Spar Gourmet"}) { id } }')
    manual_row = (await _rows(aexecute, acc))["Spar Dankt 3428"][0]["id"]
    await aexecute('mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids, merchant: {key: "spar"}}) { id } }', {"ids": [manual_row]})

    by_iban = (await aexecute(MRULE, {"input": {"merchant": {"key": "landlord"}, "field": "IBAN", "match": "EQUALS", "pattern": "AT61 1904 3002 3457 3201"}})).data["createMerchantRule"]
    await aexecute(MRULE, {"input": {"merchant": {"key": "obst huber"}, "field": "REMITTANCE", "pattern": "obst huber", "location": {"storeCode": "12"}}})
    # A rule beats the "spar" alias for one store, and never touches the manual link.
    await aexecute(MRULE, {"input": {"merchant": {"key": "spar gourmet"}, "field": "COUNTERPARTY", "match": "REGEX", "pattern": r"spar dankt 34\d8"}})
    got = await _rows(aexecute, acc)

    assert by_iban["merchant"]["key"] == "landlord"
    landlord = got["Hausverwaltung Muster"][0]
    assert (landlord["merchant"]["name"], landlord["merchantSource"], landlord["category"]["key"], landlord["categorySource"]) == ("Landlord", "RULE", "housing.rent", "MERCHANT")
    stand = got["SumUp *Stand 12"][0]
    assert (stand["merchant"]["name"], stand["merchantSource"], stand["merchantLocation"]["storeCode"]) == ("Obst Huber", "RULE", "12")
    assert (got["Spar Dankt 3418"][0]["merchant"]["name"], got["Spar Dankt 3418"][0]["merchantSource"]) == ("Spar Gourmet", "RULE")
    assert (got["Spar Dankt 3428"][0]["merchant"]["name"], got["Spar Dankt 3428"][0]["merchantSource"]) == ("Spar", "MANUAL")

    # Deleting the regex rule hands its row back to the alias.
    rule = await models.MerchantRule.objects.aget(pattern=r"spar dankt 34\d8")
    await aexecute("mutation($id: ID!) { deleteMerchantRule(id: $id) }", {"id": str(rule.id)})
    after = (await _rows(aexecute, acc))["Spar Dankt 3418"][0]
    assert (after["merchant"]["name"], after["merchantSource"]) == ("Spar", "AUTO")

    listed = (await aexecute("{ merchantRules { pattern merchant { key } transactionCount } }")).data["merchantRules"]
    assert {(r["merchant"]["key"], r["transactionCount"]) for r in listed} == {("landlord", 1), ("obst huber", 1)}
    theirs = (await aexecute("{ merchantRules { id } }", context=other_org_context)).data["merchantRules"]
    assert theirs == []


async def test_merchant_rules_validate_like_category_rules(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, SHOPS)
    await aexecute(CREATE, {"input": {"name": "Spar", "aliases": ["spar"]}})

    bad_regex = await aexecute(MRULE, {"input": {"merchant": {"key": "spar"}, "field": "COUNTERPARTY", "match": "REGEX", "pattern": "("}}, allow_errors=True)
    bad_range = await aexecute(MRULE, {"input": {"merchant": {"key": "spar"}, "field": "COUNTERPARTY", "pattern": "x", "amountMin": "10", "amountMax": "5"}}, allow_errors=True)
    unknown = await aexecute(MRULE, {"input": {"merchant": {"key": "nobody"}, "field": "COUNTERPARTY", "pattern": "x"}}, allow_errors=True)

    assert bad_regex.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert bad_range.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert unknown.errors[0].extensions["code"] == "NOT_FOUND"


# --- §9: the merchant and place join the transaction's embedding --------------------------------

SEMANTIC = "query($t: ID!) { transaction(id: $t) { semanticInput } }"


def _distance(a: models.Transaction, b: models.Transaction) -> float:
    import numpy as np

    va, vb = np.array(a.embedding, dtype=float), np.array(b.embedding, dtype=float)
    return 1 - float(va @ vb)


async def test_lines_of_one_merchant_become_neighbours(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-4.20", "2026-09-02", "SUMUP *STAND 12"), tx("-6.10", "2026-09-09", "IZETTLE *OBSTKISTE")])
    rows = await _rows(aexecute, acc)
    a_id, b_id = rows["SUMUP *STAND 12"][0]["id"], rows["IZETTLE *OBSTKISTE"][0]["id"]
    before = _distance(await models.Transaction.objects.aget(id=a_id), await models.Transaction.objects.aget(id=b_id))

    await aexecute('mutation { upsertMerchant(input: {key: "obst huber", name: "Obst Huber", description: "Obst und Gemüse am Naschmarkt", categoryKey: "food.groceries"}) { id } }')
    await aexecute('mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids, merchant: {key: "obst huber"}}) { id } }', {"ids": [a_id, b_id]})
    after = _distance(await models.Transaction.objects.aget(id=a_id), await models.Transaction.objects.aget(id=b_id))
    similar = (await aexecute("query($t: ID!) { transaction(id: $t) { similarTransactions(limit: 1) { id } } }", {"t": a_id})).data["transaction"]["similarTransactions"]

    assert before > 0.6 and after < before / 2, (before, after)
    assert similar == [{"id": b_id}]


async def test_the_context_says_merchant_category_and_place_but_not_street_or_store(link, aexecute, fakebank, fakegeo):
    fakegeo.places([{"name": "Spar", "lat": 48.1985, "lon": 16.3495, "road": "Mariahilfer Straße", "house_number": "120", "postcode": "1070", "city": "Wien", "country_code": "at"}])
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    rows = await _rows(aexecute, acc)
    spar = (await aexecute(CREATE, {"input": {"name": "Spar", "description": "Supermarkt", "categoryKey": "food.groceries", "fromTransactions": _ids(rows, "Spar Dankt 3418")}})).data["createMerchant"]
    row = rows["Spar Dankt 3418"][0]["id"]
    created = (await aexecute(SEMANTIC, {"t": row})).data["transaction"]["semanticInput"]

    store = str((await models.MerchantLocation.objects.aget(merchant_id=spar["id"], store_code="3418")).id)
    await aexecute('mutation($id: ID!) { geocodeMerchantLocation(id: $id, query: "Mariahilfer Wien") { id } }', {"id": store})
    geocoded = (await aexecute(SEMANTIC, {"t": row})).data["transaction"]["semanticInput"]
    await aexecute('mutation($id: ID!) { updateMerchant(input: {id: $id, description: "Lebensmittelhandel"}) { id } }', {"id": spar["id"]})
    described = (await aexecute(SEMANTIC, {"t": row})).data["transaction"]["semanticInput"]
    await aexecute('mutation($ids: [ID!]!) { assignMerchant(input: {transactions: $ids}) { id } }', {"ids": [row]})  # back to the alias: same merchant
    await aexecute('mutation($id: ID!) { deleteMerchant(id: $id) }', {"id": spar["id"]})
    cleared = (await aexecute(SEMANTIC, {"t": row})).data["transaction"]["semanticInput"]

    assert created == "spar spar supermarkt groceries spar"  # line "spar", merchant, description, category once, place name
    assert geocoded == "spar spar supermarkt groceries spar wien"  # the city joins; street, postcode and store number never do
    assert "mariahilfer" not in geocoded and "3418" not in geocoded and "1070" not in geocoded
    assert described == "spar spar lebensmittelhandel groceries spar wien"
    assert cleared == "spar"  # the bank line alone


async def test_a_hidden_default_category_stays_out(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, SHOPS)
    await models.Category.objects.filter(key="food.eating_out").aupdate(hidden=True)
    await aexecute('mutation { upsertMerchant(input: {key: "mcdonalds", name: "McDonalds", categoryKey: "food.eating_out"}) { id } }')

    text = (await aexecute(SEMANTIC, {"t": (await _rows(aexecute, acc))["MCDONALDS 01476"][0]["id"]})).data["transaction"]["semanticInput"]

    assert text == "mcdonalds mcdonalds mcdonalds"  # line, merchant, place "McDonalds 01476" — no "eating out"
