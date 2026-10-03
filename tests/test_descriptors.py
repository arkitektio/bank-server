"""``descriptors`` on bank's types: what an object says about itself, from its structure's declaration.

One declaration (``bank_server.service``) feeds the manifest, the signals and this field, so the
tests hold the three to each other: the field answers what a signal about the object carries, in
the keys the manifest declares. And the agent's actions run for one organization: a sweep or a
sync claims only that organization's rows.
"""

from decimal import Decimal

import pytest
from asgiref.sync import sync_to_async
from authentikate.models import Organization

from bank_server.service import agent, service
from embeddings.healer import stale_queryset
from finance import models
from finance.scheduled import sync_all_accounts
from tests.conftest import account, tx
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


async def test_an_object_answers_the_descriptors_its_structure_declares(link, aexecute, fakebank, authenticated_context):
    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop")])])
    await link()
    await sync_all_accounts(organization="static_org")
    organization = authenticated_context.request.organization
    category = await models.Category.objects.acreate(name="described", organization=organization, kind=models.CategoryKind.INCOME)
    merchant = await models.Merchant.objects.acreate(name="Shop", key="shop", organization=organization)

    data = (await aexecute(DESCRIBED, {"category": str(category.pk), "merchant": str(merchant.pk)})).data

    assert data["bankConnections"] == [{"descriptors": {"@bank/status": "ACTIVE", "@bank/provider": "ENABLEBANKING"}}]
    # What a sync brought is a fact about the sync: the account itself does not carry it.
    assert data["bankAccounts"] == [{"descriptors": {"@bank/kind": "CASH", "@bank/currency": "EUR"}}]
    assert data["transactions"] == [{"descriptors": {"@bank/status": "BOOK", "@bank/kind": "", "@bank/currency": "EUR"}}]
    assert data["category"]["descriptors"] == {"@bank/kind": "INCOME"}
    # A structure that declares no descriptors has none.
    assert data["merchant"]["descriptors"] == {}

    declared = _declared()
    assert list(data["bankConnections"][0]["descriptors"]) == declared["@bank/bankconnection"]
    assert list(data["bankAccounts"][0]["descriptors"]) == declared["@bank/bankaccount"]
    assert list(data["transactions"][0]["descriptors"]) == declared["@bank/transaction"]
    assert list(data["category"]["descriptors"]) == declared["@bank/category"]


async def test_the_field_answers_what_the_signal_carried(intake, aexecute, authenticated_context):  # noqa: F811
    category = await models.Category.objects.acreate(name="signalled and described", organization=authenticated_context.request.organization)

    (received,) = await sync_to_async(intake.of)("@bank/category")
    assert received["json"]["object"] == str(category.pk)
    data = (await aexecute("query($id: ID!) { category(id: $id) { descriptors } }", {"id": str(category.pk)})).data
    assert data["category"]["descriptors"] == received["json"]["descriptors"] != {}


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
