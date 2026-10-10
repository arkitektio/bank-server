"""Providers: an organization's admin sets one up, links go through it, and its capabilities decide what a sync does.

End to end against fakebank (which verifies each application's RS256 JWT by its ``kid``) and
fakescalable. Nothing is mocked.
"""

from importlib import import_module

import pytest
from django.db import connection

from finance import crypto, models
from finance.scheduled import sync_all_accounts
from tests.conftest import COMPLETE, LINK, REDIRECTS, account, connection_of, holding, make_provider, register_application, tx

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER = "id name kind enabled dailySyncLimit capabilities enableBanking { appId redirectUrls consentDays psuType keyFingerprint }"
CREATE_EB = f"mutation($input: CreateEnableBankingProviderInput!) {{ createEnableBankingProvider(input: $input) {{ {PROVIDER} }} }}"
UPDATE_EB = f"mutation($input: UpdateEnableBankingProviderInput!) {{ updateEnableBankingProvider(input: $input) {{ {PROVIDER} }} }}"
CREATE_SC = f"mutation($input: CreateScalableProviderInput!) {{ createScalableProvider(input: $input) {{ {PROVIDER} }} }}"
UPDATE = f"mutation($input: UpdateProviderInput!) {{ updateProvider(input: $input) {{ {PROVIDER} }} }}"
DELETE = "mutation($id: ID!) { deleteProvider(id: $id) }"
SYNC = "mutation($id: ID!) { syncAccount(id: $id) { created balances holdings } }"


def _eb(application, **fields) -> dict:
    return {"input": {"name": "Our application", "appId": application.app_id, "privateKey": application.pem, **fields}}


async def _set(provider_id: str, **fields) -> None:
    await models.BankProvider.objects.filter(id=provider_id).aupdate(**fields)


# --- what the server can run -------------------------------------------------------------------


async def test_the_kinds_list_their_capabilities(aexecute):
    kinds = (await aexecute("{ providerKinds { kind label finish hasInstitutions defaultDailySyncLimit capabilities { capability label description } } }")).data["providerKinds"]

    by_kind = {k["kind"]: k for k in kinds}
    assert set(by_kind) == {"ENABLEBANKING", "SCALABLE"}
    assert [c["capability"] for c in by_kind["ENABLEBANKING"]["capabilities"]] == ["TRANSACTIONS", "BALANCES", "SCHEDULED_SYNC"]
    assert [c["capability"] for c in by_kind["SCALABLE"]["capabilities"]] == ["TRANSACTIONS", "BALANCES", "HOLDINGS", "PRICES", "SCHEDULED_SYNC"]
    assert (by_kind["ENABLEBANKING"]["finish"], by_kind["ENABLEBANKING"]["hasInstitutions"], by_kind["ENABLEBANKING"]["defaultDailySyncLimit"]) == ("REDIRECT", True, 4)
    assert (by_kind["SCALABLE"]["finish"], by_kind["SCALABLE"]["hasInstitutions"], by_kind["SCALABLE"]["defaultDailySyncLimit"]) == ("POLL", False, None)
    assert all(c["label"] and c["description"] for k in kinds for c in k["capabilities"])


# --- setting one up ----------------------------------------------------------------------------


async def test_an_admin_sets_up_an_application_with_its_pem_and_links_through_it(aexecute, admin_context, application, fakebank):
    fakebank.scenario([account(transactions=[tx("-5.00", "2026-09-01", "Shop")], balance="10.00")])

    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]
    started = (await aexecute(LINK, {"provider": created["id"], "aspsp": fakebank.aspsp})).data["startLink"]
    connection_ = await connection_of(aexecute, (await aexecute(COMPLETE, {"code": fakebank.approve(started["state"]), "state": started["state"]})).data["completeAuth"])
    synced = (await aexecute(SYNC, {"id": connection_["accounts"][0]["id"]})).data["syncAccount"]

    assert created["kind"] == "ENABLEBANKING" and created["enabled"] is True and created["dailySyncLimit"] == 4
    assert created["capabilities"] == ["BALANCES", "SCHEDULED_SYNC", "TRANSACTIONS"]
    # The redirect URLs were not given: they are the ones registered for the application at Enable Banking.
    assert created["enableBanking"]["appId"] == "test-app" and created["enableBanking"]["redirectUrls"] == REDIRECTS
    assert len(created["enableBanking"]["keyFingerprint"]) == 47
    assert started["redirectUrl"] == REDIRECTS[0]
    assert synced == {"created": 1, "balances": 1, "holdings": 0}
    assert (await models.BankConnection.objects.aget(id=connection_["id"])).bank_provider_id == int(created["id"])


async def test_the_key_is_stored_encrypted_and_never_leaves(aexecute, admin_context, application):
    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]

    stored = await models.BankProvider.objects.aget(id=created["id"])
    history = [h async for h in models.BankProvider.provenance.model.objects.filter(id=created["id"])]
    exposed = set(import_module("bank_server.schema").schema._schema.type_map["BankProvider"].fields)

    assert "PRIVATE KEY" not in stored.secret and crypto.decrypt(stored.secret) == application.pem.strip()
    assert "PRIVATE KEY" not in str(stored.settings)
    assert history and not any(hasattr(h, "secret") for h in history)
    assert not exposed & {"secret", "privateKey", "settings"}


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"privateKey": "not a key"}, "not a PEM file"),
        ({"appId": "an-app-enable-banking-never-heard-of"}, "does not accept this application id and private key"),
        ({"redirectUrls": ["https://evil.test/cb"]}, "Not registered for this application"),
        ({"capabilities": ["HOLDINGS"]}, "cannot do HOLDINGS"),
        ({"name": "  "}, "needs a name"),
        ({"psuType": "alien"}, "Invalid Enable Banking settings"),
    ],
)
async def test_what_enable_banking_would_not_accept_is_refused_and_nothing_is_stored(aexecute, admin_context, application, fields, message):
    result = await aexecute(CREATE_EB, _eb(application, **fields), context=admin_context, allow_errors=True)

    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR" and message in result.errors[0].message
    assert not await models.BankProvider.objects.aexists()


async def test_a_key_that_is_not_the_applications_is_refused(aexecute, admin_context, application, backend_stack):
    other = register_application(backend_stack.fakebank_url, "another-app")

    result = await aexecute(CREATE_EB, _eb(application, privateKey=other.pem), context=admin_context, allow_errors=True)

    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert not await models.BankProvider.objects.aexists()


async def test_only_an_admin_of_the_organization_sets_providers_up(aexecute, admin_context, colleague_context, other_org_context, application):
    by_member = await aexecute(CREATE_EB, _eb(application), allow_errors=True)
    scalable_by_member = await aexecute(CREATE_SC, {"input": {}}, context=colleague_context, allow_errors=True)
    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]
    update_by_member = await aexecute(UPDATE, {"input": {"id": created["id"], "enabled": False}}, allow_errors=True)
    delete_by_member = await aexecute(DELETE, {"id": created["id"]}, allow_errors=True)
    seen_by_member = (await aexecute("{ bankProviders { id name } }")).data["bankProviders"]
    seen_elsewhere = (await aexecute("{ bankProviders { id } }", context=other_org_context)).data["bankProviders"]
    foreign = await aexecute("query($id: ID!) { bankProvider(id: $id) { id } }", {"id": created["id"]}, context=other_org_context, allow_errors=True)

    for refused in (by_member, scalable_by_member, update_by_member, delete_by_member):
        assert refused.errors[0].extensions["code"] == "PERMISSION_DENIED"
    assert seen_by_member == [{"id": created["id"], "name": "Our application"}]  # members see it: they link through it
    assert seen_elsewhere == [] and foreign.errors[0].extensions["code"] == "NOT_FOUND"


async def test_a_provider_of_another_organization_links_nothing(aexecute, other_org_context, eb_provider, fakebank):
    fakebank.scenario([account()])

    result = await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp}, context=other_org_context, allow_errors=True)

    assert result.errors[0].extensions["code"] == "NOT_FOUND"


async def test_names_are_unique_in_the_organization(aexecute, admin_context, application):
    await aexecute(CREATE_EB, _eb(application), context=admin_context)

    again = await aexecute(CREATE_SC, {"input": {"name": "Our application"}}, context=admin_context, allow_errors=True)

    assert again.errors[0].extensions["code"] == "VALIDATION_ERROR" and "already has a provider named" in again.errors[0].message


# --- changing one ------------------------------------------------------------------------------


async def test_an_update_without_a_key_keeps_the_stored_one(aexecute, admin_context, application):
    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]
    secret = (await models.BankProvider.objects.aget(id=created["id"])).secret

    changed = (
        await aexecute(UPDATE_EB, {"input": {"id": created["id"], "name": "Renamed", "privateKey": "", "consentDays": 30, "redirectUrls": [REDIRECTS[1]]}}, context=admin_context)
    ).data["updateEnableBankingProvider"]

    assert changed["name"] == "Renamed" and changed["enableBanking"]["consentDays"] == 30 and changed["enableBanking"]["redirectUrls"] == [REDIRECTS[1]]
    assert changed["enableBanking"]["keyFingerprint"] == created["enableBanking"]["keyFingerprint"]
    assert (await models.BankProvider.objects.aget(id=created["id"])).secret == secret


async def test_a_new_key_is_proven_before_it_replaces_the_old_one(aexecute, admin_context, application, backend_stack):
    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]
    rotated = register_application(backend_stack.fakebank_url, "test-app-rotated")

    wrong = await aexecute(UPDATE_EB, {"input": {"id": created["id"], "privateKey": rotated.pem}}, context=admin_context, allow_errors=True)
    after_wrong = await models.BankProvider.objects.aget(id=created["id"])
    right = (await aexecute(UPDATE_EB, {"input": {"id": created["id"], "privateKey": rotated.pem, "appId": rotated.app_id}}, context=admin_context)).data["updateEnableBankingProvider"]

    assert wrong.errors[0].extensions["code"] == "VALIDATION_ERROR"
    assert crypto.decrypt(after_wrong.secret) == application.pem.strip()
    assert right["enableBanking"]["appId"] == "test-app-rotated" and right["enableBanking"]["keyFingerprint"] != created["enableBanking"]["keyFingerprint"]


async def test_a_provider_whose_key_enable_banking_no_longer_accepts_can_still_be_disabled(aexecute, admin_context, backend_stack, fakebank):
    """The kill switch: the form sends every field back unchanged, and none of that may ask Enable Banking."""
    revoked = register_application(backend_stack.fakebank_url, "revoked-app")
    created = (await aexecute(CREATE_EB, _eb(revoked), context=admin_context)).data["createEnableBankingProvider"]
    register_application(backend_stack.fakebank_url, "revoked-app")  # the application's key was replaced at Enable Banking
    seen = len(fakebank.log())

    unchanged = {"appId": "revoked-app", "privateKey": "", "redirectUrls": created["enableBanking"]["redirectUrls"], "consentDays": 90}
    disabled = (await aexecute(UPDATE_EB, {"input": {"id": created["id"], "name": "Revoked", "enabled": False, **unchanged}}, context=admin_context)).data["updateEnableBankingProvider"]
    new_redirect = await aexecute(UPDATE_EB, {"input": {"id": created["id"], "redirectUrls": [REDIRECTS[1]]}}, context=admin_context, allow_errors=True)

    assert (disabled["name"], disabled["enabled"]) == ("Revoked", False)
    assert fakebank.log()[seen:] == []
    assert new_redirect.errors[0].extensions["code"] == "VALIDATION_ERROR"  # that does need Enable Banking, which refuses the old key


async def test_two_applications_in_one_organization_each_sign_as_themselves(aexecute, admin_context, application, backend_stack, fakebank):
    second = register_application(backend_stack.fakebank_url, "second-app")
    fakebank.scenario([account(transactions=[tx("-1.00", "2026-09-01", "Shop")])])
    first_id = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]["id"]
    second_id = (await aexecute(CREATE_EB, {"input": {"name": "Second", "appId": second.app_id, "privateKey": second.pem}}, context=admin_context)).data["createEnableBankingProvider"]["id"]

    seen = len(fakebank.log())
    for provider in (first_id, second_id):
        started = (await aexecute(LINK, {"provider": provider, "aspsp": fakebank.aspsp})).data["startLink"]
        linked = await connection_of(aexecute, (await aexecute(COMPLETE, {"code": fakebank.approve(started["state"]), "state": started["state"]})).data["completeAuth"])
        await aexecute(SYNC, {"id": linked["accounts"][0]["id"]})
        await models.AccountSyncer.objects.aupdate(sync_day=None, syncs_today=0)  # the same account: give the second link a budget too

    calls = [(e["kid"], e["path"].split("/")[1]) for e in fakebank.log()[seen:]]
    assert [kid for kid, _ in calls] == ["test-app"] * 4 + ["second-app"] * 4
    assert [path for _, path in calls][:4] == ["auth", "sessions", "accounts", "accounts"]


# --- capabilities ------------------------------------------------------------------------------


async def test_a_capability_switched_off_is_not_fetched_or_stored(aexecute, admin_context, link, eb_provider, fakebank):
    fakebank.scenario([account(transactions=[tx("-5.00", "2026-09-01", "Shop")], balance="10.00")])
    account_id = (await link())["accounts"][0]["id"]

    changed = (await aexecute(UPDATE, {"input": {"id": eb_provider, "capabilities": ["TRANSACTIONS"]}}, context=admin_context)).data["updateProvider"]
    seen = len(fakebank.log())
    synced = (await aexecute(SYNC, {"id": account_id})).data["syncAccount"]

    assert changed["capabilities"] == ["TRANSACTIONS"]
    assert synced == {"created": 1, "balances": 0, "holdings": 0}
    assert [e["path"].rsplit("/", 1)[1] for e in fakebank.log()[seen:]] == ["transactions"]
    assert not await models.BalanceSnapshot.objects.aexists()


async def test_transactions_switched_off_leaves_the_stored_ones_alone(aexecute, link, eb_provider, fakebank):
    ident = "acc-keeps-rows"
    fakebank.scenario([account(ident, transactions=[tx("-5.00", "2026-09-01", "Shop"), tx("-2.00", "2026-09-02", "Cafe", status="PDNG")], balance="10.00")])
    account_id = (await link())["accounts"][0]["id"]
    await aexecute(SYNC, {"id": account_id})

    await _set(eb_provider, capabilities=["BALANCES"])
    synced = (await aexecute(SYNC, {"id": account_id})).data["syncAccount"]

    assert synced == {"created": 0, "balances": 1, "holdings": 0}
    assert await models.Transaction.objects.filter(account_id=account_id).acount() == 2  # the pending row too


async def test_a_provider_with_nothing_to_sync_says_so_without_asking_the_bank(aexecute, link, eb_provider, fakebank):
    fakebank.scenario([account()])
    account_id = (await link())["accounts"][0]["id"]
    await _set(eb_provider, capabilities=["SCHEDULED_SYNC"])
    seen = len(fakebank.log())

    result = await aexecute(SYNC, {"id": account_id}, allow_errors=True)

    assert result.errors[0].extensions["code"] == "NOT_CONFIGURED"
    assert fakebank.log()[seen:] == []


async def test_an_unsupported_capability_is_refused(aexecute, admin_context, eb_provider):
    result = await aexecute(UPDATE, {"input": {"id": eb_provider, "capabilities": ["TRANSACTIONS", "PRICES"]}}, context=admin_context, allow_errors=True)

    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR" and "cannot do PRICES" in result.errors[0].message


async def test_the_unattended_sync_only_runs_providers_that_allow_it(aexecute, link, eb_provider, fakebank):
    fakebank.scenario([account(transactions=[tx("-5.00", "2026-09-01", "Shop")])])
    account_id = (await link())["accounts"][0]["id"]
    await _set(eb_provider, capabilities=["TRANSACTIONS", "BALANCES"])

    skipped = await sync_all_accounts("static_org")
    by_hand = (await aexecute(SYNC, {"id": account_id})).data["syncAccount"]
    await _set(eb_provider, capabilities=["TRANSACTIONS", "BALANCES", "SCHEDULED_SYNC"])
    ran = await sync_all_accounts("static_org")

    assert skipped == {"synced": 0, "skipped": 0, "failed": 0}
    assert by_hand["created"] == 1
    assert ran == {"synced": 1, "skipped": 0, "failed": 0}


async def test_scalable_holdings_and_prices_follow_their_capabilities(aexecute, scalable_link, provider_for, fakescalable, fakemarket):
    fakescalable.seed({"p1": {"cash": 100, "holdings": [holding("IE00TEST0001", 2, 109.0, 100.0)], "transactions": []}})
    fakescalable.prices({"IE00TEST0001": [("2026-09-24", 108.0), ("2026-09-25", 109.0)]})
    connection_ = await scalable_link()
    depot = next(a["id"] for a in connection_["accounts"] if a["kind"] == "DEPOT")
    provider = await provider_for("SCALABLE")

    await _set(provider, capabilities=["TRANSACTIONS", "BALANCES"])
    seen = len(fakescalable.log())
    without = (await aexecute(SYNC, {"id": depot})).data["syncAccount"]
    asked = {e["operation"] for e in fakescalable.log()[seen:] if "operation" in e}
    quote = await aexecute('{ securityQuote(isin: "IE00TEST0001", priceSource: SCALABLE) { price } }', allow_errors=True)
    stored_without = await models.HoldingSnapshot.objects.aexists()
    await _set(provider, capabilities=["TRANSACTIONS", "BALANCES", "HOLDINGS", "PRICES"])
    with_all = (await aexecute(SYNC, {"id": depot})).data["syncAccount"]

    assert without == {"created": 0, "balances": 1, "holdings": 0}
    assert "BrokerHoldings" not in asked and "BrokerChart" not in asked
    assert quote.errors and not stored_without
    assert with_all["holdings"] == 1
    assert await models.SecurityPrice.objects.filter(source="SCALABLE").aexists()


# --- disabling and removing --------------------------------------------------------------------


async def test_a_disabled_provider_links_and_syncs_nothing_but_still_revokes(aexecute, admin_context, link, eb_provider, fakebank):
    fakebank.scenario([account()])
    linked = await link()
    await aexecute(UPDATE, {"input": {"id": eb_provider, "enabled": False}}, context=admin_context)
    seen = len(fakebank.log())

    start = await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp}, allow_errors=True)
    sync = await aexecute(SYNC, {"id": linked["accounts"][0]["id"]}, allow_errors=True)
    silent = fakebank.log()[seen:]
    revoked = (await aexecute("mutation($id: ID!) { revokeBankConnection(id: $id) { status lastError } }", {"id": linked["id"]})).data["revokeBankConnection"]

    assert start.errors[0].extensions["code"] == sync.errors[0].extensions["code"] == "NOT_CONFIGURED"
    assert silent == []
    assert revoked == {"status": "REVOKED", "lastError": None}
    assert [e["method"] for e in fakebank.log()[seen:]] == ["DELETE"]


async def test_a_provider_in_use_is_not_deleted_and_a_deleted_one_leaves_the_data(aexecute, admin_context, link, eb_provider, fakebank):
    fakebank.scenario([account(transactions=[tx("-5.00", "2026-09-01", "Shop")])])
    linked = await link()
    await aexecute(SYNC, {"id": linked["accounts"][0]["id"]})

    in_use = await aexecute(DELETE, {"id": eb_provider}, context=admin_context, allow_errors=True)
    await aexecute("mutation($id: ID!) { revokeBankConnection(id: $id) { id } }", {"id": linked["id"]})
    deleted = (await aexecute(DELETE, {"id": eb_provider}, context=admin_context)).data["deleteProvider"]
    left = (await aexecute("query($id: ID!) { bankConnection(id: $id) { status bankProvider { id } accounts { id } } }", {"id": linked["id"]})).data["bankConnection"]

    assert in_use.errors[0].extensions["code"] == "VALIDATION_ERROR" and "revoke them first" in in_use.errors[0].message
    assert deleted == eb_provider
    assert left == {"status": "REVOKED", "bankProvider": None, "accounts": [{"id": linked["accounts"][0]["id"]}]}
    assert await models.Transaction.objects.acount() == 1


# --- connections from before providers existed --------------------------------------------------


async def test_consents_without_a_provider_wait_for_the_organizations_first_one(aexecute, admin_context, link, eb_provider, application, fakebank):
    """What an update leaves behind: an ACTIVE Enable Banking consent attached to nothing."""
    fakebank.scenario([account(transactions=[tx("-5.00", "2026-09-01", "Shop")])])
    linked = await link()
    await models.BankProvider.objects.filter(id=eb_provider).adelete()

    orphaned = await aexecute(SYNC, {"id": linked["accounts"][0]["id"]}, allow_errors=True)
    created = (await aexecute(CREATE_EB, _eb(application), context=admin_context)).data["createEnableBankingProvider"]
    synced = (await aexecute(SYNC, {"id": linked["accounts"][0]["id"]})).data["syncAccount"]

    assert orphaned.errors[0].extensions["code"] == "NOT_CONFIGURED"
    assert (await models.BankConnection.objects.aget(id=linked["id"])).bank_provider_id == int(created["id"])
    assert synced["created"] == 1


def test_the_migration_gives_scalable_logins_their_provider(transactional_db, authenticated_context, other_org_context):
    """0012's data step, run on rows as 0011 left them: Scalable logins of two organizations, none attached."""
    migration = import_module("finance.migrations.0012_bank_providers")
    organization, other = authenticated_context.request.organization, other_org_context.request.organization
    existing = make_provider(other, "SCALABLE", name="Already there")
    rows = [
        models.BankConnection.objects.create(organization=org, provider=kind, aspsp_name="x", aspsp_country="DE", state=f"s{index}", redirect_url="", pending_expires_at="2026-01-01T00:00:00Z")
        for index, (org, kind) in enumerate([(organization, "SCALABLE"), (organization, "SCALABLE"), (organization, "ENABLEBANKING"), (other, "SCALABLE")])
    ]

    from django.apps import apps

    with connection.schema_editor() as editor:
        migration.scalable_providers(apps, editor)
        migration.scalable_providers(apps, editor)  # nothing left to attach the second time

    for row in rows:
        row.refresh_from_db()
    created = models.BankProvider.objects.get(organization=organization)
    assert (created.kind, created.name, created.capabilities) == ("SCALABLE", "Scalable Capital", ["BALANCES", "HOLDINGS", "PRICES", "SCHEDULED_SYNC", "TRANSACTIONS"])
    assert [row.bank_provider_id for row in rows] == [created.id, created.id, None, existing.id]
    assert models.BankProvider.objects.count() == 2
