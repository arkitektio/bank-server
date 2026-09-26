"""Categorization rules: priority, filters, and never overriding a user."""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

RULE = """
mutation($c: ID!, $field: RuleField!, $pattern: String!, $match: RuleMatch, $priority: Int, $direction: RuleDirection, $min: Decimal) {
  createCategoryRule(input: {category: $c, field: $field, pattern: $pattern, match: $match, priority: $priority, direction: $direction, amountMin: $min}) { id }
}
"""


async def _synced(link, aexecute, fakebank, rows) -> int:  # noqa: ANN001
    fakebank.scenario([account(transactions=rows)])
    acc_id = (await link())["accounts"][0]["id"]
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": acc_id})
    return int(acc_id)


async def _category(name: str) -> str:
    return str((await models.Category.objects.aget(name=name)).id)


async def _cat_of(counterparty: str) -> tuple[str | None, str]:
    row = await models.Transaction.objects.select_related("category").aget(counterparty=counterparty)
    return (row.category.name if row.category else None, row.category_source)


async def test_first_rule_by_priority_wins_and_filters_apply(link, aexecute, fakebank):
    await _synced(link, aexecute, fakebank, [tx("-40.00", "2026-09-01", "BILLA 123"), tx("-4.00", "2026-09-02", "BILLA Snack"), tx("15.00", "2026-09-03", "BILLA refund")])
    await aexecute(RULE, {"c": await _category("Eating Out"), "field": "COUNTERPARTY", "pattern": "billa", "priority": 50, "direction": "OUT", "min": "0"})
    await aexecute(RULE, {"c": await _category("Groceries"), "field": "COUNTERPARTY", "pattern": "billa", "priority": 10, "direction": "OUT", "min": "10"})

    assert await _cat_of("BILLA 123") == ("Groceries", "RULE")  # both match; priority 10 wins
    assert await _cat_of("BILLA Snack") == ("Eating Out", "RULE")  # below Groceries' amountMin
    assert await _cat_of("BILLA refund") == (None, "NONE")  # money in; both rules are OUT-only


async def test_regex_rule_and_invalid_regex(link, aexecute, fakebank):
    await _synced(link, aexecute, fakebank, [tx("-9.99", "2026-09-01", "NETFLIX.COM 4432")])
    await aexecute(RULE, {"c": await _category("Subscriptions"), "field": "COUNTERPARTY", "pattern": r"^netflix\.com", "match": "REGEX"})
    assert await _cat_of("NETFLIX.COM 4432") == ("Subscriptions", "RULE")

    bad = await aexecute(RULE, {"c": await _category("Subscriptions"), "field": "COUNTERPARTY", "pattern": "([", "match": "REGEX"}, allow_errors=True)
    assert bad.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_rules_never_override_manual_and_clearing_hands_back(link, aexecute, fakebank):
    await _synced(link, aexecute, fakebank, [tx("-40.00", "2026-09-01", "SPAR")])
    tx_id = str((await models.Transaction.objects.aget(counterparty="SPAR")).id)
    await aexecute('mutation($id: ID!, $c: ID!) { categorizeTransaction(input: {id: $id, category: $c}) { id } }', {"id": tx_id, "c": await _category("Leisure & Travel")})
    await aexecute(RULE, {"c": await _category("Groceries"), "field": "COUNTERPARTY", "pattern": "spar"})
    await aexecute("mutation { reapplyRules }")
    assert await _cat_of("SPAR") == ("Leisure & Travel", "MANUAL")

    cleared = await aexecute('mutation($id: ID!) { categorizeTransaction(input: {id: $id}) { category { name } categorySource } }', {"id": tx_id})
    assert cleared.data["categorizeTransaction"] == {"category": {"name": "Groceries"}, "categorySource": "RULE"}


async def test_deleting_a_rule_hands_back_what_it_set(link, aexecute, fakebank):
    await _synced(link, aexecute, fakebank, [tx("-40.00", "2026-09-01", "HOFER"), tx("-9.00", "2026-09-02", "ZZ UNKNOWN SHOP")])
    eating = await _category("Eating Out")
    hofer = await aexecute(RULE, {"c": eating, "field": "COUNTERPARTY", "pattern": "hofer"})
    other = await aexecute(RULE, {"c": eating, "field": "COUNTERPARTY", "pattern": "zz unknown"})
    assert await _cat_of("HOFER") == ("Eating Out", "RULE")
    await aexecute('mutation($id: ID!) { deleteCategoryRule(id: $id) }', {"id": hofer.data["createCategoryRule"]["id"]})
    await aexecute('mutation($id: ID!) { deleteCategoryRule(id: $id) }', {"id": other.data["createCategoryRule"]["id"]})
    # Let go of by its rule, a row gets the rest of the pipeline: "Hofer" is a Groceries term.
    assert await _cat_of("HOFER") == ("Groceries", "SEMANTIC")
    assert await _cat_of("ZZ UNKNOWN SHOP") == (None, "NONE")


async def test_rules_apply_on_import(link, aexecute, fakebank):
    await aexecute("mutation { seedDefaultCategories { id } }")
    await aexecute(RULE, {"c": await _category("Salary"), "field": "REMITTANCE", "pattern": "gehalt"})
    await _synced(link, aexecute, fakebank, [tx("3000.00", "2026-09-01", "ACME GmbH", remittance="Gehalt September")])
    assert await _cat_of("ACME GmbH") == ("Salary", "RULE")


async def test_category_cannot_become_its_own_ancestor(aexecute):
    parent = await aexecute('mutation { createCategory(input: {name: "Household"}) { id } }')
    child = await aexecute('mutation($p: ID!) { createCategory(input: {name: "Cleaning", parent: $p}) { id } }', {"p": parent.data["createCategory"]["id"]})
    result = await aexecute(
        'mutation($id: ID!, $p: ID!) { updateCategory(input: {id: $id, parent: $p}) { id } }',
        {"id": parent.data["createCategory"]["id"], "p": child.data["createCategory"]["id"]},
        allow_errors=True,
    )
    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"
