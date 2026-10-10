"""Tenant isolation: another organization can neither see nor touch this one's data.

Every list, by-id lookup, mutation argument and stat is checked from tenant B against data
of tenant A. A foreign id must look exactly like a missing one (NOT_FOUND).
"""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def tenant_a(link, aexecute, fakebank):
    fakebank.scenario([account(transactions=[tx("-10.00", "2026-09-01", "Shop")], balance="90.00")])
    connection = await link()
    acc_id = connection["accounts"][0]["id"]
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": acc_id})
    tx_id = str((await models.Transaction.objects.filter(account_id=acc_id).afirst()).id)
    category_id = str((await models.Category.objects.afirst()).id)
    return {"connection": connection["id"], "account": acc_id, "transaction": tx_id, "category": category_id}


LISTS = ["bankProviders { id }", "bankConnections { id }", "bankAccounts { id }", "transactions { id }", "categories { id }", "categoryRules { id }", "budgets { id }", "recurringPayments { id }"]


@pytest.mark.parametrize("selection", LISTS)
async def test_lists_are_empty_for_other_org(tenant_a, aexecute, other_org_context, selection):
    result = await aexecute(f"query {{ {selection} }}", context=other_org_context)
    assert list(result.data.values())[0] == []


@pytest.mark.parametrize(
    "document,key",
    [
        ('query($id: ID!) { bankConnection(id: $id) { id } }', "connection"),
        ('query($id: ID!) { bankAccount(id: $id) { id } }', "account"),
        ('query($id: ID!) { transaction(id: $id) { id } }', "transaction"),
        ('query($id: ID!) { category(id: $id) { id } }', "category"),
        ('mutation($id: ID!) { syncAccount(id: $id) { created } }', "account"),
        ('mutation($id: ID!) { syncConnection(id: $id) { created } }', "connection"),
        ('mutation($id: ID!) { revokeBankConnection(id: $id) { id } }', "connection"),
        ('mutation($id: ID!) { setTransactionNote(input: {id: $id, note: "x"}) { id } }', "transaction"),
        ('mutation($id: ID!) { deleteCategory(id: $id) { categories } }', "category"),
        ('mutation($id: ID!) { createBudget(input: {category: $id, amount: "10"}) { id } }', "category"),
        ('mutation($id: ID!) { createCategoryRule(input: {category: $id, field: COUNTERPARTY, pattern: "x"}) { id } }', "category"),
        ('query($id: ID!) { balanceHistory(account: $id, dateFrom: "2026-01-01") { amount } }', "account"),
        ('query($id: ID!) { forecast(account: $id) { amount } }', "account"),
        ('query($id: ID!) { spendingByCategory(accounts: [$id]) { net } }', "account"),
    ],
)
async def test_foreign_ids_are_not_found(tenant_a, aexecute, other_org_context, document, key):
    result = await aexecute(document, {"id": tenant_a[key]}, context=other_org_context, allow_errors=True)
    assert result.errors, result.data
    assert result.errors[0].extensions["code"] == "NOT_FOUND"


async def test_cannot_categorize_with_a_foreign_category(tenant_a, aexecute, other_org_context, link, fakebank):
    """Tenant B's own transaction, tenant A's category: refused."""
    fakebank.scenario([account(transactions=[tx("-1.00", "2026-09-01", "B shop")])])
    b_account = (await link(context=other_org_context))["accounts"][0]["id"]
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": b_account}, context=other_org_context)
    b_tx = str((await models.Transaction.objects.filter(account_id=b_account).afirst()).id)

    result = await aexecute(
        'mutation($id: ID!, $c: ID!) { categorizeTransaction(input: {id: $id, category: $c}) { id } }',
        {"id": b_tx, "c": tenant_a["category"]},
        context=other_org_context,
        allow_errors=True,
    )
    assert result.errors[0].extensions["code"] == "NOT_FOUND"


async def test_link_state_from_another_org_is_refused(aexecute, other_org_context, fakebank, eb_provider):
    """Tenant B cannot complete a link tenant A started, even holding its code and state."""
    fakebank.scenario([account()])
    started = await aexecute('mutation($a: String!, $p: ID!) { startLink(input: {provider: $p, institution: $a, country: "AT"}) { state } }', {"a": fakebank.aspsp, "p": eb_provider})
    state = started.data["startLink"]["state"]
    code = fakebank.approve(state)

    result = await aexecute(
        'mutation($c: String!, $s: String!) { completeAuth(input: {code: $c, state: $s}) { status } }',
        {"c": code, "s": state},
        context=other_org_context,
        allow_errors=True,
    )
    assert result.errors[0].extensions["code"] == "INVALID_STATE"
    assert not await models.BankAccount.objects.aexists()


async def test_stats_do_not_mix_tenants(tenant_a, aexecute, other_org_context):
    result = await aexecute("query { spendingByCategory { net } cashflow { net } topCounterparties { total } budgetStatus { spent } }", context=other_org_context)
    assert result.data == {"spendingByCategory": [], "cashflow": [], "topCounterparties": [], "budgetStatus": []}
