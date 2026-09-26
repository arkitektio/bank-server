"""Accounts and their syncers: the 0006 migration, account resolution on link, and syncing through syncers."""

import datetime
import uuid

import pytest

from finance import models
from finance.sync import fetch_since

from .conftest import account, tx


@pytest.mark.django_db(transaction=True)
def test_migration_gives_every_account_a_syncer():
    """0006: one syncer per account — with or without a connection — carrying its sync state, and every row gets it."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("finance", "0005_category_terms")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        apps = executor.loader.project_state(before).apps
        Organization = apps.get_model("authentikate", "Organization")
        Connection = apps.get_model("finance", "BankConnection")
        Account = apps.get_model("finance", "BankAccount")
        Transaction = apps.get_model("finance", "Transaction")
        org = Organization.objects.create(slug=f"mig-{uuid.uuid4().hex[:6]}")
        now = datetime.datetime.now(datetime.timezone.utc)
        linked = Connection.objects.create(
            organization=org, aspsp_name="Bank", aspsp_country="AT", state=uuid.uuid4().hex, redirect_url="x", status="ACTIVE", pending_expires_at=now, provider="SCALABLE"
        )
        with_connection = Account.objects.create(
            organization=org, connection=linked, uid="u1", identification_key="scalable:p:broker:1:cash", iban="AT12 3456", currency="EUR",
            last_synced_at=now, syncs_today=2, sync_day=now.date(), last_error="boom", last_error_code="BANK_ERROR",
        )
        orphan = Account.objects.create(organization=org, connection=None, uid="u2", identification_key="hash-2", currency="EUR")
        for day in (1, 5):
            Transaction.objects.create(account=with_connection, fingerprint=f"f{day}", booking_date=datetime.date(2026, 9, day), amount="-1.00", currency="EUR")

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes("finance"))
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes("finance"))

    first = models.AccountSyncer.objects.get(account_id=with_connection.id)
    assert (first.backend, first.remote_id, first.identification_key, first.connection_id) == ("SCALABLE", "u1", "scalable:p:broker:1:cash", linked.id)
    assert (first.syncs_today, first.last_error_code, first.last_synced_at) == (2, "BANK_ERROR", now)
    second = models.AccountSyncer.objects.get(account_id=orphan.id)
    assert (second.backend, second.identification_key, second.connection_id) == ("ENABLEBANKING", "hash-2", None)
    assert models.BankAccount.objects.get(id=with_connection.id).iban_normalized == "AT123456"
    assert set(models.Transaction.objects.filter(account_id=with_connection.id).values_list("syncer_id", "origin")) == {(first.id, "SYNC")}
    # The incremental start is what it was per account before.
    assert fetch_since(first, 3) == datetime.date(2026, 9, 2)


SYNC = "mutation Sync($id: ID!) { syncAccount(id: $id) { created account { id lastSyncedAt isImportOnly syncers { backend lastSyncedAt } } } }"
ACCOUNT = "query($id: ID!) { bankAccount(id: $id) { id isImportOnly connection { id } syncers { backend connection { id } } } }"


async def test_a_linked_account_has_one_syncer_and_syncs_through_it(link, aexecute, fakebank):
    fakebank.scenario([account(transactions=[tx("-3.00", "2026-09-02", "Shop")])])
    connection = await link()
    acc_id = connection["accounts"][0]["id"]

    synced = (await aexecute(SYNC, {"id": acc_id})).data["syncAccount"]

    assert synced["created"] == 1 and synced["account"]["isImportOnly"] is False
    assert [s["backend"] for s in synced["account"]["syncers"]] == ["ENABLEBANKING"]
    assert synced["account"]["lastSyncedAt"] == synced["account"]["syncers"][0]["lastSyncedAt"]
    row = await models.Transaction.objects.select_related("syncer").aget(account_id=acc_id)
    assert row.origin == "SYNC" and row.syncer.account_id == int(acc_id)


async def test_relink_adopts_an_account_known_only_by_iban(link, aexecute, fakebank, authenticated_context):
    """An account without syncers (e.g. from an import) is adopted by IBAN + currency instead of duplicated."""
    org = authenticated_context.request.organization
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    dead = await models.BankAccount.objects.acreate(organization=org, iban=f"{iban[:4]} {iban[4:]}", currency="EUR", name="Old Giro")
    fakebank.scenario([account(iban=iban, name="Giro")])

    connection = await link()

    assert [a["id"] for a in connection["accounts"]] == [str(dead.id)]
    adopted = (await aexecute(ACCOUNT, {"id": dead.id})).data["bankAccount"]
    assert adopted["isImportOnly"] is False and adopted["connection"]["id"] == connection["id"]
    assert await models.BankAccount.objects.filter(organization=org).acount() == 1


async def test_relink_does_not_adopt_when_the_iban_is_ambiguous(link, fakebank, authenticated_context):
    org = authenticated_context.request.organization
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    for name in ("Pocket A", "Pocket B"):
        await models.BankAccount.objects.acreate(organization=org, iban=iban, currency="EUR", name=name)
    fakebank.scenario([account(iban=iban)])

    connection = await link()

    assert await models.BankAccount.objects.filter(organization=org).acount() == 3
    assert int(connection["accounts"][0]["id"]) not in {a.id async for a in models.BankAccount.objects.filter(name__startswith="Pocket")}


async def test_two_consented_accounts_with_one_iban_stay_two(link, fakebank):
    """Currency pockets or sub-accounts a bank reports under one IBAN: the second must not adopt the first."""
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    fakebank.scenario([account(iban=iban, name="Pocket A"), account(iban=iban, name="Pocket B")])

    connection = await link()

    ids = {int(a["id"]) for a in connection["accounts"]}
    assert len(ids) == 2
    assert [await models.AccountSyncer.objects.filter(account_id=i).acount() for i in sorted(ids)] == [1, 1]


async def test_an_account_without_syncers_is_import_only_and_refuses_sync(aexecute, authenticated_context):
    acc = await models.BankAccount.objects.acreate(organization=authenticated_context.request.organization, currency="EUR", name="Imported")

    result = await aexecute(SYNC, {"id": acc.id}, allow_errors=True)

    assert result.errors[0].extensions["code"] == "CONNECTION_INACTIVE"
    assert (await aexecute(ACCOUNT, {"id": acc.id})).data["bankAccount"] == {"id": str(acc.id), "isImportOnly": True, "connection": None, "syncers": []}
