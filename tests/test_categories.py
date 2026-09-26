"""Deleting a category never strands transactions: reassigned, or handed back to rules and suggestions."""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

DELETE = "mutation($id: ID!, $to: ID, $dry: Boolean! = false) { deleteCategory(id: $id, reassignTo: $to, dryRun: $dry) { categories transactions rules budgets dismissedBaseKeys } }"


async def _setup(link, aexecute, fakebank) -> tuple[list[str], str, str]:  # noqa: ANN001
    fakebank.scenario([account(transactions=[tx("-10.00", "2026-09-01", "Gardener A"), tx("-20.00", "2026-09-02", "Gardener B")])])
    acc = (await link())["accounts"][0]["id"]
    await aexecute("mutation($id: ID!) { syncAccount(id: $id) { created } }", {"id": acc})
    ids = [t["id"] for t in (await aexecute("query($a: ID!) { transactions(filters: {accounts: [$a]}) { id } }", {"a": acc})).data["transactions"]]
    garden = (await aexecute('mutation { createCategory(input: {name: "Garden"}) { id } }')).data["createCategory"]["id"]
    tools = (await aexecute('mutation($p: ID!) { createCategory(input: {name: "Tools", parent: $p}) { id } }', {"p": garden})).data["createCategory"]["id"]
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": ids[:1], "c": garden})
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": ids[1:], "c": tools})
    await aexecute('mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "zzz-never"}) { id } }', {"c": tools})
    await aexecute('mutation($c: ID!) { createBudget(input: {category: $c, amount: "50.00", currency: "EUR", startMonth: "2026-09-01"}) { id } }', {"c": garden})
    return ids, garden, tools


async def test_dry_run_reports_without_deleting(link, aexecute, fakebank):
    _, garden, _ = await _setup(link, aexecute, fakebank)

    report = (await aexecute(DELETE, {"id": garden, "dry": True})).data["deleteCategory"]

    assert report == {"categories": 2, "transactions": 2, "rules": 1, "budgets": 1, "dismissedBaseKeys": []}
    assert await models.Category.objects.filter(id=garden).aexists()


async def test_reassign_moves_the_subtrees_transactions_as_manual(link, aexecute, fakebank):
    ids, garden, _ = await _setup(link, aexecute, fakebank)
    home = str((await models.Category.objects.aget(key="housing.maintenance")).id)

    await aexecute(DELETE, {"id": garden, "to": home})
    moved = [row async for row in models.Transaction.objects.filter(id__in=ids).values_list("category_id", "category_source")]

    assert moved == [(int(home), "MANUAL")] * 2
    assert not await models.Category.objects.filter(name__in=["Garden", "Tools"]).aexists()


async def test_without_reassign_rows_go_back_to_rules_and_suggestions(link, aexecute, fakebank):
    ids, garden, _ = await _setup(link, aexecute, fakebank)
    home = str((await models.Category.objects.aget(key="housing.maintenance")).id)
    await aexecute('mutation($c: ID!) { createCategoryRule(input: {category: $c, field: COUNTERPARTY, pattern: "gardener a", apply: false}) { id } }', {"c": home})

    await aexecute(DELETE, {"id": garden})
    rows = {row[0]: row[1:] async for row in models.Transaction.objects.filter(id__in=ids).values_list("counterparty", "category_id", "category_source")}

    assert rows["Gardener A"] == (int(home), "RULE")  # no longer stranded as MANUAL with no category
    assert rows["Gardener B"] == (None, "NONE")


async def test_cannot_reassign_into_the_deleted_subtree(link, aexecute, fakebank):
    _, garden, tools = await _setup(link, aexecute, fakebank)

    result = await aexecute(DELETE, {"id": garden, "to": tools}, allow_errors=True)

    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"
