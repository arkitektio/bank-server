"""``descriptors`` on bank's types: what an object says about itself, stored on its row.

One declaration (``bank_server.service``) feeds the manifest, the signals and this field, so the
tests hold the three to each other: the field answers what a signal about the object carries, in
the keys the manifest declares. The descriptors are stored, so a port's ``requires`` is a filter
on the list query, and every bulk write (a sync, an import, rules, transfer detection, a user's
batch edit) keeps the stored value what the describer says. And the agent's actions run for one
organization: a sweep or a sync claims only that organization's rows.
"""

import uuid
from decimal import Decimal

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import Organization
from django.core.management import call_command

from bank_server import descriptors
from bank_server.hook_agent import agent
from bank_server.service import service
from embeddings.healer import stale_queryset
from finance import models
from finance.scheduled import sync_all_accounts
from finance.taxonomy import seed_base_categories
from tests.conftest import LINK, SESSION, account, cash, trade, tx
from tests.test_import_finanzguru import APPLY, fg
from tests.test_import_finanzguru import import_file  # noqa: F401  the fixture
from tests.test_signals import intake  # noqa: F401  the fixture

pytestmark = pytest.mark.django_db(transaction=True)

DESCRIBED = """
    query Described($category: ID!, $merchant: ID!) {
        bankConnections { descriptors }
        bankAccounts { descriptors }
        transactions { descriptors }
        category(id: $category) { descriptors }
        merchant(id: $merchant) { descriptors }
    }
"""


def _declared() -> dict[str, list[str]]:
    return {s["identifier"]: [d["key"] for d in s["descriptors"]] for s in service.manifest()["structures"]}


def _iban() -> str:
    return f"AT{uuid.uuid4().int % 10**18:018d}"


@sync_to_async
def _stored(model: type, **filters: object) -> list:
    """The rows as stored, each checked against its describer and its structure's declared keys."""
    declared = service.structure_for(model)
    assert declared is not None
    rows = list(model.objects.filter(**filters).order_by("id"))
    for row in rows:
        assert row.stored_descriptors == declared.compute(row), row
        assert set(row.stored_descriptors) == set(_declared()[declared.identifier])
    return rows


async def _required(aexecute, field: str, constraints: list[dict]) -> set[str]:
    result = await aexecute(f"query ($c: [DescriptorConstraintInput!]!) {{ {field}(filters: {{ requires: $c }}) {{ id }} }}", {"c": constraints})
    return {row["id"] for row in result.data[field]}


def _eq(key: str, value: object) -> list[dict]:
    return [{"key": key, "operator": "EQUALS", "value": value}]


async def test_an_object_answers_the_descriptors_its_structure_declares(link, aexecute, fakebank, authenticated_context):
    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop")])])
    await link()
    await sync_all_accounts(organization="static_org")
    organization = authenticated_context.request.organization
    category = await models.Category.objects.acreate(name="described", organization=organization, kind=models.CategoryKind.INCOME)
    merchant = await models.Merchant.objects.acreate(name="Shop", key="shop", organization=organization, online=True)

    data = (await aexecute(DESCRIBED, {"category": str(category.pk), "merchant": str(merchant.pk)})).data

    assert data["bankConnections"] == [
        {"descriptors": {"@bank/status": "ACTIVE", "@bank/provider": "ENABLEBANKING", "@bank/country": "AT", "@bank/link_step": "", "@bank/error_code": ""}}
    ]
    # What a sync brought is a fact about the sync: the account itself does not carry it.
    assert data["bankAccounts"] == [{"descriptors": {"@bank/kind": "CASH", "@bank/currency": "EUR", "@bank/has_iban": True}}]
    assert data["transactions"] == [
        {
            "descriptors": {
                "@bank/status": "BOOK",
                "@bank/kind": "",
                "@bank/currency": "EUR",
                "@bank/direction": "OUT",
                "@bank/is_transfer": False,
                "@bank/categorized": False,
                "@bank/has_merchant": False,
                "@bank/has_security": False,
            }
        }
    ]
    assert data["category"]["descriptors"] == {"@bank/kind": "INCOME", "@bank/custom": True}
    assert data["merchant"]["descriptors"] == {"@bank/online": True}

    # Stored in jsonb, which orders keys its own way: the keys are the declared ones, as a set.
    declared = _declared()
    assert set(data["bankConnections"][0]["descriptors"]) == set(declared["@bank/bankconnection"])
    assert set(data["bankAccounts"][0]["descriptors"]) == set(declared["@bank/bankaccount"])
    assert set(data["transactions"][0]["descriptors"]) == set(declared["@bank/transaction"])
    assert set(data["category"]["descriptors"]) == set(declared["@bank/category"])
    assert set(data["merchant"]["descriptors"]) == set(declared["@bank/merchant"])


async def test_the_field_answers_what_the_signal_carried(intake, aexecute, authenticated_context):  # noqa: F811
    category = await models.Category.objects.acreate(name="signalled and described", organization=authenticated_context.request.organization)

    (received,) = await sync_to_async(intake.of)("@bank/category")
    assert received["json"]["object"] == str(category.pk)
    data = (await aexecute("query($id: ID!) { category(id: $id) { descriptors } }", {"id": str(category.pk)})).data
    assert data["category"]["descriptors"] == received["json"]["descriptors"] != {}


MARK = "mutation($ids: [ID!]!, $t: Boolean) { markTransfers(ids: $ids, isTransfer: $t) { id } }"
CATEGORIZE = "mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }"


async def test_a_sync_stores_what_its_bulk_writes_describe_and_requires_filters_on_it(link, aexecute, fakebank, authenticated_context):
    """Rows are bulk-created, merchants matched, rules applied and transfers detected in bulk: each step keeps the column right."""
    organization = authenticated_context.request.organization
    groceries = await models.Category.objects.acreate(name="Groceries", organization=organization)
    await models.CategoryRule.objects.acreate(organization=organization, category=groceries, field=models.RuleField.COUNTERPARTY, pattern="Shop")
    shop = await models.Merchant.objects.acreate(name="Shop", key="shop", organization=organization)
    await models.MerchantAlias.objects.acreate(organization=organization, merchant=shop, pattern="shop")
    main, savings = _iban(), _iban()
    fakebank.scenario(
        [
            account(
                iban=main,
                transactions=[
                    tx("-1.00", "2026-09-01", "Shop"),
                    tx("-50.00", "2026-09-02", "Me", iban=savings),
                    tx("20.00", "2026-09-03", "Employer"),
                    tx("0.00", "2026-09-04", "Bank notice"),
                    tx("-3.00", "2026-09-05", "Kiosk", status="PDNG"),
                    *(tx("-9.99", day, "Streaming") for day in ("2026-07-01", "2026-07-31", "2026-08-30")),
                ],
            ),
            account(iban=savings, name="Savings"),
        ]
    )
    await link()
    await sync_all_accounts(organization="static_org")

    rows = {row.counterparty: row for row in await _stored(models.Transaction, account__organization=organization)}
    assert len(rows) == 6  # the streaming payments share a counterparty
    facts = {name: {key.removeprefix("@bank/"): value for key, value in row.stored_descriptors.items() if key in ("@bank/direction", "@bank/is_transfer", "@bank/has_merchant", "@bank/status")} for name, row in rows.items()}
    assert facts["Shop"] == {"direction": "OUT", "is_transfer": False, "has_merchant": True, "status": "BOOK"}
    assert facts["Me"] == {"direction": "OUT", "is_transfer": True, "has_merchant": False, "status": "BOOK"}
    assert facts["Employer"]["direction"] == "IN"
    # Zero moved no money out.
    assert facts["Bank notice"]["direction"] == "IN"
    assert facts["Kiosk"]["status"] == "PDNG"
    assert rows["Shop"].stored_descriptors["@bank/categorized"] is True  # by the rule, in bulk

    (recurring,) = await _stored(models.RecurringPayment, organization=organization)
    assert recurring.stored_descriptors == {"@bank/status": "DETECTED", "@bank/interval_days": 30, "@bank/currency": "EUR", "@bank/direction": "OUT"}
    assert {a.iban for a in await _stored(models.BankAccount, organization=organization)} == {main, savings}
    (connection,) = await _stored(models.BankConnection, organization=organization)
    (provider,) = await _stored(models.BankProvider, organization=organization)
    assert provider.stored_descriptors == {"@bank/provider": "ENABLEBANKING", "@bank/enabled": True, "@bank/capabilities": ["BALANCES", "SCHEDULED_SYNC", "TRANSACTIONS"]}

    ids = {name: str(row.pk) for name, row in rows.items()}
    streaming = {str(t.pk) async for t in models.Transaction.objects.filter(counterparty="Streaming")}
    assert await _required(aexecute, "transactions", _eq("@bank/direction", "OUT")) == {ids["Shop"], ids["Me"], ids["Kiosk"], *streaming}
    assert await _required(aexecute, "transactions", [*_eq("@bank/direction", "IN"), {"key": "@bank/status", "operator": "NOT_EQUALS", "value": "PDNG"}]) == {ids["Employer"], ids["Bank notice"]}
    assert await _required(aexecute, "transactions", _eq("@bank/is_transfer", True)) == {ids["Me"]}
    assert await _required(aexecute, "transactions", _eq("@bank/has_merchant", True)) == {ids["Shop"]}
    assert await _required(aexecute, "bankConnections", _eq("@bank/country", "AT")) == {str(connection.pk)}
    assert await _required(aexecute, "bankAccounts", _eq("@bank/has_iban", True)) == {str(a.pk) async for a in models.BankAccount.objects.filter(organization=organization)}
    assert await _required(aexecute, "recurringPayments", _eq("@bank/direction", "OUT")) == {str(recurring.pk)}

    # A user's batch edits are updates too: the stored descriptors follow them.
    await aexecute(MARK, {"ids": [ids["Shop"]], "t": True})
    await aexecute(CATEGORIZE, {"ids": [ids["Employer"]], "c": str(groceries.pk)})
    assert await _required(aexecute, "transactions", _eq("@bank/is_transfer", True)) == {ids["Me"], ids["Shop"]}
    assert ids["Employer"] in await _required(aexecute, "transactions", _eq("@bank/categorized", True))
    await aexecute(MARK, {"ids": [ids["Shop"]], "t": None})  # back to detection: Shop is no transfer
    assert await _required(aexecute, "transactions", _eq("@bank/is_transfer", True)) == {ids["Me"]}
    await _stored(models.Transaction, account__organization=organization)

    # A key bank does not declare is refused, not ignored.
    result = await aexecute(
        "query ($c: [DescriptorConstraintInput!]!) { transactions(filters: { requires: $c }) { id } }", {"c": _eq("@bank/colour", "red")}, allow_errors=True
    )
    assert result.errors and "not a descriptor here" in result.errors[0].message


SYNC_CONNECTION = "mutation Sync($id: ID!) { syncConnection(id: $id) { created } }"


async def test_a_scalable_sync_describes_its_trades_and_providers_are_filtered_by_capability(scalable_link, provider_for, aexecute, fakescalable, authenticated_context):
    fakescalable.seed(
        {
            "p1": {
                "cash": 100,
                "holdings": [],
                "transactions": [
                    cash(12.34, "2026-09-20T00:00:00.000Z", "DISTRIBUTION", isin="IE00TEST0001", description="Fund IE00TEST0001"),
                    trade(-500, "2026-09-10T21:57:43.636Z", "IE00TEST0001", quantity=5),
                    cash(3000, "2026-09-02T00:00:00.000Z", "DEPOSIT", description="Sent from N26"),
                ],
            }
        }
    )
    linked = await scalable_link()
    await aexecute(SYNC_CONNECTION, {"id": linked["id"]})
    organization = authenticated_context.request.organization

    by_kind = {row.kind: row.stored_descriptors for row in await _stored(models.Transaction, account__organization=organization)}
    assert {kind: (d["@bank/direction"], d["@bank/is_transfer"], d["@bank/has_security"]) for kind, d in by_kind.items()} == {
        "DISTRIBUTION": ("IN", False, True),
        "BUY": ("OUT", True, True),
        "DEPOSIT": ("IN", True, False),
    }
    (connection,) = await _stored(models.BankConnection, organization=organization)
    assert (connection.stored_descriptors["@bank/provider"], connection.stored_descriptors["@bank/link_step"]) == ("SCALABLE", "DONE")
    assert {a.stored_descriptors["@bank/has_iban"] for a in await _stored(models.BankAccount, organization=organization)} == {False}

    enable_banking = await provider_for("ENABLEBANKING")
    scalable = await provider_for("SCALABLE")
    await _stored(models.BankProvider, organization=organization)
    holdings = [{"key": "@bank/capabilities", "operator": "CONTAINS", "value": "HOLDINGS"}]
    assert await _required(aexecute, "bankProviders", holdings) == {scalable}
    assert await _required(aexecute, "bankProviders", [{"key": "@bank/capabilities", "operator": "CONTAINS", "value": "TRANSACTIONS"}]) == {scalable, enable_banking}
    assert await _required(aexecute, "transactions", _eq("@bank/has_security", True)) == {str(t.pk) async for t in models.Transaction.objects.filter(isin="IE00TEST0001")}


async def test_an_import_stores_what_it_wrote_in_bulk(import_file, aexecute, authenticated_context):  # noqa: F811
    reference = f"DE{uuid.uuid4().int % 10**20:020d}"
    preview = await import_file(
        [
            fg("1", reference, "01.09.2026", "-12.30", "Hofer", main="Lebenshaltung", sub="Lebensmittel"),
            fg("2", reference, "02.09.2026", "-100", "Me", transfer=True),
            fg("3", reference, "03.09.2026", "2500", "ACME GmbH"),
        ]
    )
    applied = (await aexecute(APPLY, {"id": preview["id"]})).data["applyStatementImport"]
    organization = authenticated_context.request.organization

    rows = {row.counterparty: row.stored_descriptors for row in await _stored(models.Transaction, account__organization=organization)}
    assert (rows["Hofer"]["@bank/categorized"], rows["Hofer"]["@bank/direction"]) == (True, "OUT")
    assert rows["Me"]["@bank/is_transfer"] is True
    assert rows["ACME GmbH"]["@bank/direction"] == "IN"
    (statement,) = await _stored(models.StatementImport, organization=organization)
    assert statement.stored_descriptors == {"@bank/status": "APPLIED", "@bank/source": "FINANZGURU"}
    assert await _required(aexecute, "statementImports", _eq("@bank/status", "APPLIED")) == {applied["id"]}


async def test_categories_merchants_and_budgets_are_described_and_filtered(aexecute, authenticated_context):
    organization = authenticated_context.request.organization
    await sync_to_async(seed_base_categories)(organization.id, {"food"})
    own = await models.Category.objects.acreate(name="Hobbies", organization=organization)
    online = await models.Merchant.objects.acreate(name="Webshop", key="webshop", organization=organization, online=True)
    await models.Merchant.objects.acreate(name="Corner", key="corner", organization=organization)
    budget = await models.Budget.objects.acreate(organization=organization, category=own, amount=Decimal("50"), currency="EUR", start_month="2026-09-01")

    await _stored(models.Category, organization=organization)
    await _stored(models.Merchant, organization=organization)
    (stored_budget,) = await _stored(models.Budget, organization=organization)
    assert stored_budget.stored_descriptors == {"@bank/currency": "EUR"}

    assert await _required(aexecute, "categories", _eq("@bank/custom", True)) == {str(own.pk)}
    assert await _required(aexecute, "merchants", _eq("@bank/online", True)) == {str(online.pk)}
    assert await _required(aexecute, "budgets", _eq("@bank/currency", "EUR")) == {str(budget.pk)}

    # A root's kind is followed by its subtree in one update: the children are stored with it.
    child = await models.Category.objects.acreate(name="Model trains", organization=organization, parent=own)
    await aexecute("mutation($id: ID!) { updateCategory(input: {id: $id, kind: INCOME}) { id } }", {"id": str(own.pk)})
    assert (await models.Category.objects.aget(pk=child.pk)).stored_descriptors["@bank/kind"] == "INCOME"


CANCEL = "mutation($s: String!) { cancelAuth(state: $s) { %s } }" % SESSION


async def test_a_cancelled_login_is_stored_as_cancelled(aexecute, fakebank, provider_for, authenticated_context):
    fakebank.scenario([account(iban=_iban())])
    started = (await aexecute(LINK, {"provider": await provider_for("ENABLEBANKING"), "aspsp": fakebank.aspsp})).data["startLink"]
    pending = await _required(aexecute, "bankConnections", _eq("@bank/status", "PENDING"))

    await aexecute(CANCEL, {"s": started["state"]})

    (connection,) = await _stored(models.BankConnection, organization=authenticated_context.request.organization)
    assert connection.stored_descriptors["@bank/status"] == "CANCELLED"
    assert pending == {str(connection.pk)}


async def test_the_setup_job_repairs_a_row_that_misses_its_descriptors(link, fakebank, authenticated_context):
    fakebank.scenario([account(iban=_iban(), transactions=[tx("-1.00", "2026-09-01", "Shop")])])
    await link()
    await sync_all_accounts(organization="static_org")
    row = await models.Transaction.objects.aget(account__organization=authenticated_context.request.organization)
    category = await models.Category.objects.acreate(name="blanked", organization=authenticated_context.request.organization)

    # A row from before its descriptors were stored owes them; the setup job states them.
    await models.Transaction.objects.filter(pk=row.pk).aupdate(stored_descriptors={})
    await models.Category.objects.filter(pk=category.pk).aupdate(stored_descriptors={})
    await sync_to_async(call_command)("restore_descriptors")

    assert (await models.Transaction.objects.aget(pk=row.pk)).stored_descriptors == descriptors.transaction_descriptors(row)
    await _stored(models.Category, pk=category.pk)


async def _stale_rows(organization: Organization) -> dict[type, int]:
    """One category (with its term) and one transaction of ``organization``, all embedded by another model."""
    category = await models.Category.objects.acreate(name=f"stale {organization.slug}", organization=organization)
    term = await models.CategoryTerm.objects.filter(category=category).afirst()
    bank_account = await models.BankAccount.objects.acreate(organization=organization, currency="EUR", name="Giro")
    transaction = await models.Transaction.objects.acreate(account=bank_account, fingerprint=f"stale-{organization.slug}", amount=Decimal("-1.00"), currency="EUR", counterparty="Shop")
    rows = {models.Category: category.pk, models.CategoryTerm: term.pk, models.Transaction: transaction.pk}
    for model, pk in rows.items():
        await model.objects.filter(pk=pk).aupdate(embedding_model="another-model")
    return rows


async def test_a_sweep_for_one_organization_claims_only_its_rows(authenticated_context, other_org_context):
    """Every organization has the agent and its own schedule: a run must not do another's work."""
    mine = await _stale_rows(authenticated_context.request.organization)
    theirs = await _stale_rows(other_org_context.request.organization)

    def stale(model: type, organization: str | None) -> set[int]:
        return set(stale_queryset(model, organization).filter(pk__in=[mine[model], theirs[model]]).values_list("pk", flat=True))

    # A category carries its organization; a term reaches it through its category, a transaction through its account.
    for model in (models.Category, models.CategoryTerm, models.Transaction):
        assert await sync_to_async(stale)(model, "other_org") == {theirs[model]}
        assert await sync_to_async(stale)(model, "static_org") == {mine[model]}
        assert await sync_to_async(stale)(model, None) == {mine[model], theirs[model]}

    result = await agent.actions["reembed_stale"].function(organization="other_org")

    assert result["reembedded"] >= 3
    for model in (models.Category, models.CategoryTerm, models.Transaction):
        assert await sync_to_async(stale)(model, None) == {mine[model]}
