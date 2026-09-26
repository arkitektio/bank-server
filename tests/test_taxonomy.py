"""Base categories: a two-level tree seeded by key, fully editable, never forced back."""

import pytest

from finance import models, taxonomy
from tests.conftest import account

pytestmark = pytest.mark.django_db(transaction=True)

CATS = "{ categories(filters: {base: true}) { key name kind parent { key } } }"


async def _org_id(link, fakebank) -> int:  # noqa: ANN001
    fakebank.scenario([account()])
    connection = await link()
    return (await models.BankConnection.objects.aget(id=connection["id"])).organization_id


async def test_link_seeds_the_two_level_tree_with_inherited_kinds(link, aexecute, fakebank):
    await _org_id(link, fakebank)

    cats = {c["key"]: c for c in (await aexecute(CATS)).data["categories"]}

    assert set(cats) == set(taxonomy.NODES)
    assert cats["food.groceries"]["parent"] == {"key": "food"}
    assert cats["income.salary"]["kind"] == "INCOME" and cats["transfers.investing"]["kind"] == "TRANSFER"
    groceries = await models.Category.objects.aget(key="food.groceries")
    assert "Hofer" in groceries.description
    assert "Hofer" in [t async for t in groceries.terms.values_list("text", flat=True)]


async def test_seeding_is_idempotent_and_keeps_user_edits(link, aexecute, fakebank):
    await _org_id(link, fakebank)
    groceries = (await models.Category.objects.aget(key="food.groceries")).id
    await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, name: "Lebensmittel", hidden: true, color: "#000000"}) { id } }', {"id": groceries})

    created = (await aexecute("mutation { syncBaseCategories { key } }")).data["syncBaseCategories"]
    edited = await models.Category.objects.aget(id=groceries)

    assert created == []
    assert (edited.name, edited.hidden, edited.color, edited.key) == ("Lebensmittel", True, "#000000", "food.groceries")


async def test_a_deleted_base_category_stays_deleted_until_restored(link, aexecute, fakebank):
    await _org_id(link, fakebank)
    food = (await models.Category.objects.aget(key="food")).id

    deleted = (await aexecute('mutation($id: ID!) { deleteCategory(id: $id) { categories dismissedBaseKeys } }', {"id": food})).data["deleteCategory"]
    resynced = (await aexecute("mutation { syncBaseCategories { key } }")).data["syncBaseCategories"]
    restored = (await aexecute('mutation { restoreBaseCategory(key: "food") { key } }')).data["restoreBaseCategory"]

    assert deleted["categories"] == 4 and set(deleted["dismissedBaseKeys"]) == {"food", "food.groceries", "food.eating_out", "food.delivery"}
    assert resynced == []
    assert {c["key"] for c in restored} == {"food", "food.groceries", "food.eating_out", "food.delivery"}


async def test_a_subtree_is_one_kind(link, aexecute, fakebank):
    await _org_id(link, fakebank)
    income = (await models.Category.objects.aget(key="income")).id
    food = (await models.Category.objects.aget(key="food")).id

    child = (await aexecute('mutation($p: ID!) { createCategory(input: {name: "Tips", parent: $p, kind: EXPENSE}) { id kind } }', {"p": income})).data["createCategory"]
    refused = await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, kind: EXPENSE}) { id } }', {"id": child["id"]}, allow_errors=True)
    moved = (await aexecute('mutation($id: ID!, $p: ID!) { updateCategory(input: {id: $id, parent: $p}) { kind } }', {"id": child["id"], "p": food})).data["updateCategory"]
    await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, kind: TRANSFER}) { id } }', {"id": food})

    assert child["kind"] == "INCOME"
    assert refused.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert moved["kind"] == "EXPENSE"
    kinds = {c.kind async for c in models.Category.objects.filter(parent_id=food)}
    assert kinds == {"TRANSFER"}


async def test_seeding_adopts_a_same_named_category_instead_of_duplicating(transactional_db):
    from authentikate.models import Organization

    org = await Organization.objects.acreate(slug="adopt-org")
    await models.Category.objects.acreate(organization=org, name="Food", kind="EXPENSE")

    from asgiref.sync import sync_to_async

    await sync_to_async(taxonomy.seed_base_categories)(org.id)

    food = [c async for c in models.Category.objects.filter(organization=org, name="Food", parent=None)]
    assert len(food) == 1 and food[0].key == "food"


async def test_descriptions_become_terms_and_follow_edits(link, aexecute, fakebank):
    await _org_id(link, fakebank)
    groceries = (await models.Category.objects.aget(key="food.groceries")).id

    await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, description: "Bauernmarkt, Hofladen"}) { id } }', {"id": groceries})
    terms = (await aexecute("query($id: ID!) { category(id: $id) { terms } }", {"id": groceries})).data["category"]["terms"]

    assert terms == ["Groceries", "Bauernmarkt", "Hofladen"]


@pytest.mark.django_db(transaction=True)
def test_migration_keys_legacy_defaults_and_fixes_kinds():
    """0004 on an organization seeded with the old flat defaults (and a mismatched child kind)."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = [("finance", "0003_on_demand_sync")], [("finance", "0005_category_terms")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    apps = executor.loader.project_state(before).apps
    Organization = apps.get_model("authentikate", "Organization")
    OldCategory = apps.get_model("finance", "Category")
    org = Organization.objects.create(slug="legacy-org")
    for name, kind in [("Groceries", "EXPENSE"), ("Rent & Housing", "EXPENSE"), ("Utilities", "EXPENSE"), ("Salary", "INCOME"), ("Transfer", "TRANSFER"), ("My Own", "EXPENSE")]:
        OldCategory.objects.create(organization=org, name=name, kind=kind)
    salary = OldCategory.objects.get(organization=org, name="Salary")
    OldCategory.objects.create(organization=org, name="Bonus", kind="EXPENSE", parent=salary)  # wrong kind under income

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    try:
        _assert_legacy_keyed()
    finally:
        # Leave the schema as every other test expects it: fully migrated.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes("finance"))


def _assert_legacy_keyed() -> None:
    by_name = {c.name: c for c in models.Category.objects.filter(organization__slug="legacy-org").select_related("parent")}
    assert by_name["Groceries"].key == "food.groceries" and by_name["Groceries"].parent.key == "food"
    assert by_name["Rent & Housing"].key == "housing" and by_name["Rent & Housing"].parent is None
    assert by_name["Utilities"].parent_id == by_name["Rent & Housing"].id  # under the legacy root that became housing
    assert by_name["Salary"].parent.key == "income" and by_name["Salary"].parent.kind == "INCOME"
    assert by_name["Bonus"].kind == "INCOME"
    assert by_name["My Own"].key is None and by_name["My Own"].parent is None
    assert by_name["Groceries"].embedding is not None and models.CategoryTerm.objects.filter(category=by_name["Groceries"]).exists()
