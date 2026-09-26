"""Semantic categorization end to end: the real embedding model, real pgvector, fakebank syncs.

Nothing is faked. Bank lines are normalized before embedding (store numbers, "DANKT", cities
drop out), so the same merchant lands at distance ~0 and category terms ("Hofer" in
Groceries) meet a never-seen merchant exactly.
"""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

SYNC = "mutation($id: ID!) { syncAccount(id: $id) { created categorized } }"
TXS = "query($a: ID!) { transactions(filters: {accounts: [$a]}, ordering: [{bookingDate: ASC}]) { id counterparty categorySource category { name key } } }"
SUGGEST = "query($t: ID!) { suggestCategories(transaction: $t) { category { key name } score reason neighbours evidence { id } } }"


async def _bank(link, aexecute, fakebank, rows, context=None) -> tuple[str, str]:  # noqa: ANN001
    ident = f"acct-{fakebank.aspsp.replace(' ', '-')}"
    fakebank.scenario([account(ident, transactions=rows)])
    acc = (await link(context))["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc}, context=context)
    return acc, ident


async def _rows(aexecute, acc, context=None) -> dict[str, dict]:  # noqa: ANN001
    return {t["counterparty"]: t for t in (await aexecute(TXS, {"a": acc}, context=context)).data["transactions"]}


async def test_a_known_merchant_term_assigns_on_first_sight(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815")])

    row = (await _rows(aexecute, acc))["HOFER DANKT 0815"]

    assert row["categorySource"] == "SEMANTIC" and row["category"]["key"] == "food.groceries"


async def test_the_organizations_own_choices_outvote_base_terms(link, aexecute, fakebank):
    acc, ident = await _bank(link, aexecute, fakebank, [tx("-12.00", "2026-09-01", "BILLA DANKT 0421"), tx("-30.00", "2026-09-03", "BILLA DANKT 0421 WIEN")])
    shop = (await aexecute('mutation { createCategory(input: {name: "Weekly Shop"}) { id } }')).data["createCategory"]["id"]
    ids = [t["id"] for t in (await _rows(aexecute, acc)).values()]
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": ids, "c": shop})

    fakebank.set_account(ident, transactions=[tx("-12.00", "2026-09-01", "BILLA DANKT 0421"), tx("-30.00", "2026-09-03", "BILLA DANKT 0421 WIEN"), tx("-8.90", "2026-09-10", "BILLA FILIALE 1180 WIEN")])
    await aexecute(SYNC, {"id": acc})
    new = (await _rows(aexecute, acc))["BILLA FILIALE 1180 WIEN"]
    suggestions = (await aexecute(SUGGEST, {"t": new["id"]})).data["suggestCategories"]

    assert new["categorySource"] == "SEMANTIC" and new["category"]["name"] == "Weekly Shop"
    assert suggestions[0]["category"]["name"] == "Weekly Shop" and suggestions[0]["reason"] == "NEIGHBOURS"
    assert suggestions[0]["neighbours"] == 2 and len(suggestions[0]["evidence"]) == 2
    assert any(s["category"]["key"] == "food.groceries" for s in suggestions)  # the base term still shows up


async def test_a_loose_match_is_only_suggested(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-9.80", "2026-09-02", "Apotheke zum Hirschen")])
    row = (await _rows(aexecute, acc))["Apotheke zum Hirschen"]

    suggestions = (await aexecute(SUGGEST, {"t": row["id"]})).data["suggestCategories"]

    assert row["categorySource"] == "NONE"
    assert suggestions[0]["category"]["key"] == "health.pharmacy" and suggestions[0]["reason"] == "TERMS"


async def test_rules_override_semantic_and_nothing_overrides_manual(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815"), tx("-5.00", "2026-09-02", "OMV 4711")])
    rows = await _rows(aexecute, acc)
    leisure = str((await models.Category.objects.aget(key="leisure")).id)
    eating = str((await models.Category.objects.aget(key="food.eating_out")).id)
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": [rows["OMV 4711"]["id"]], "c": leisure})

    await aexecute(
        'mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "hofer"}) { id } }', {"c": eating}
    )
    await aexecute('mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "omv"}) { id } }', {"c": eating})
    rows = await _rows(aexecute, acc)

    assert rows["HOFER DANKT 0815"]["categorySource"] == "RULE" and rows["HOFER DANKT 0815"]["category"]["key"] == "food.eating_out"
    assert rows["OMV 4711"]["categorySource"] == "MANUAL" and rows["OMV 4711"]["category"]["key"] == "leisure"


async def test_with_auto_assign_off_it_only_suggests(link, aexecute, fakebank, settings):
    settings.CATEGORIZATION = {**settings.CATEGORIZATION, "auto_assign": False}
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815")])
    row = (await _rows(aexecute, acc))["HOFER DANKT 0815"]

    suggestions = (await aexecute(SUGGEST, {"t": row["id"]})).data["suggestCategories"]

    assert row["categorySource"] == "NONE"
    assert suggestions[0]["category"]["key"] == "food.groceries"


async def test_with_embeddings_disabled_everything_still_works_lexically(link, aexecute, fakebank, settings):
    settings.EMBEDDINGS = {**settings.EMBEDDINGS, "ENABLED": False}
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815")])
    row = (await _rows(aexecute, acc))["HOFER DANKT 0815"]

    suggestions = (await aexecute(SUGGEST, {"t": row["id"]})).data["suggestCategories"]
    lexical = (await aexecute('{ transactions(filters: {search: "hofer"}) { counterparty } }')).data["transactions"]
    semantic = (await aexecute('{ transactions(filters: {search: "supermarket"}) { counterparty } }')).data["transactions"]

    assert row["categorySource"] == "NONE" and suggestions == []
    assert lexical == [{"counterparty": "HOFER DANKT 0815"}] and semantic == []


async def test_search_finds_by_meaning_through_categories(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815"), tx("-60.00", "2026-09-02", "OMV 4711")])

    groceries = (await aexecute('{ transactions(filters: {search: "supermarket"}) { counterparty } }')).data["transactions"]
    fuel = (await aexecute('{ transactions(filters: {search: "tankstelle"}) { counterparty } }')).data["transactions"]

    assert [t["counterparty"] for t in groceries] == ["HOFER DANKT 0815"]
    assert [t["counterparty"] for t in fuel] == ["OMV 4711"]


async def test_similar_and_near_category_stay_in_the_organization(link, aexecute, fakebank, other_org_context):
    rows = [tx("-12.00", "2026-09-01", "BILLA DANKT 0421"), tx("-30.00", "2026-09-03", "BILLA FILIALE 1180"), tx("-60.00", "2026-09-04", "OMV 4711")]
    acc, _ = await _bank(link, aexecute, fakebank, rows)
    other_acc, _ = await _bank(link, aexecute, fakebank, rows, context=other_org_context)
    mine = await _rows(aexecute, acc)
    theirs = await _rows(aexecute, other_acc, context=other_org_context)
    groceries = str((await models.Category.objects.filter(key="food.groceries", organization__slug="static_org").afirst()).id)

    similar = (await aexecute("query($t: ID!) { transaction(id: $t) { similarTransactions(limit: 5, maxDistance: 0.2) { id } } }", {"t": mine["BILLA DANKT 0421"]["id"]})).data["transaction"]["similarTransactions"]
    by_filter = (await aexecute("query($t: ID!) { transactions(filters: {similarTo: $t}) { id } }", {"t": mine["BILLA DANKT 0421"]["id"]})).data["transactions"]
    probe = (await aexecute("query($t: ID!) { transactions(filters: {similarTo: $t}) { id } }", {"t": theirs["BILLA DANKT 0421"]["id"]})).data["transactions"]
    near = (await aexecute("query($c: ID!) { transactions(filters: {nearCategory: $c}) { counterparty } }", {"c": groceries})).data["transactions"]

    their_ids = {t["id"] for t in theirs.values()}
    assert [s["id"] for s in similar] == [mine["BILLA FILIALE 1180"]["id"]]
    assert by_filter[0]["id"] == mine["BILLA FILIALE 1180"]["id"] and not their_ids & {t["id"] for t in by_filter}
    assert probe == []  # another organization's transaction is no anchor here
    assert {t["counterparty"] for t in near} == {"BILLA DANKT 0421", "BILLA FILIALE 1180"}


async def test_candidates_and_reapply_recompute_guesses(link, aexecute, fakebank):
    acc, _ = await _bank(link, aexecute, fakebank, [tx("-9.80", "2026-09-02", "Apotheke zum Hirschen"), tx("-4.50", "2026-09-03", "APOTHEKE AM MARKT")])
    pharmacy = str((await models.Category.objects.aget(key="health.pharmacy")).id)

    before = (await aexecute("query($c: ID!) { category(id: $c) { candidates { counterparty } } }", {"c": pharmacy})).data["category"]["candidates"]
    # Teaching a category a merchant: put its name in the description.
    await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, description: "Apotheke, Apotheke zum Hirschen, Apotheke am Markt"}) { id } }', {"id": pharmacy})
    changed = (await aexecute("mutation { reapplyRules }")).data["reapplyRules"]
    rows = await _rows(aexecute, acc)

    assert {c["counterparty"] for c in before} == {"Apotheke zum Hirschen", "APOTHEKE AM MARKT"}
    assert changed == 2
    assert {r["categorySource"] for r in rows.values()} == {"SEMANTIC"}


async def test_a_merchant_query_stays_exact_while_a_concept_widens(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, [tx("-23.40", "2026-09-01", "HOFER DANKT 0815"), tx("-12.00", "2026-09-02", "BILLA DANKT 0421"), tx("-60.00", "2026-09-04", "OMV 4711")])

    hofer = (await aexecute('{ transactions(filters: {search: "hofer"}) { counterparty } }')).data["transactions"]
    concept = (await aexecute('{ transactions(filters: {search: "supermarket"}) { counterparty } }')).data["transactions"]
    counted = (await aexecute('{ transactionsCount(filters: {search: "hofer"}) }')).data["transactionsCount"]

    assert [t["counterparty"] for t in hofer] == ["HOFER DANKT 0815"] and counted == 1
    assert {t["counterparty"] for t in concept} == {"HOFER DANKT 0815", "BILLA DANKT 0421"}


async def test_the_inbox_list_shape_resolves_suggestions(link, aexecute, fakebank):
    await _bank(link, aexecute, fakebank, [tx("-9.80", "2026-09-02", "Apotheke zum Hirschen"), tx("-5.00", "2026-09-03", "Apotheke zum Hirschen"), tx("-7.00", "2026-09-04", "Mystery Merchant XY")])

    inbox = (await aexecute(
        "{ transactions(filters: {uncategorized: true}, ordering: [{bookingDate: ASC}]) { counterparty suggestedCategories(limit: 2) { category { key } reason } similarTransactions(limit: 3, maxDistance: 0.2) { counterparty } } }"
    )).data["transactions"]

    by_line = {t["counterparty"]: t for t in inbox}
    first = inbox[0]
    assert first["suggestedCategories"][0]["category"]["key"] == "health.pharmacy"
    assert [s["counterparty"] for s in first["similarTransactions"]] == ["Apotheke zum Hirschen"]
    assert by_line["Mystery Merchant XY"]["suggestedCategories"] == []
