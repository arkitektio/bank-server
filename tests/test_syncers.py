"""Accounts and their syncers: the 0006 migration, account resolution on link, and syncing through syncers."""

import uuid

from kante.context import HttpContext

from finance import models

from .conftest import AExecute, FakeBank, Link, account, extensions_of, tx

SYNC = "mutation Sync($id: ID!) { syncAccount(id: $id) { created account { id lastSyncedAt isImportOnly syncers { backend lastSyncedAt } } } }"
ACCOUNT = "query($id: ID!) { bankAccount(id: $id) { id isImportOnly connection { id } syncers { backend connection { id } } } }"


async def test_a_linked_account_has_one_syncer_and_syncs_through_it(
    link: Link, aexecute: AExecute, fakebank: FakeBank
) -> None:
    fakebank.scenario([account(transactions=[tx("-3.00", "2026-09-02", "Shop")])])
    connection = await link()
    acc_id = connection["accounts"][0]["id"]

    synced = (await aexecute(SYNC, {"id": acc_id})).data["syncAccount"]

    assert synced["created"] == 1 and synced["account"]["isImportOnly"] is False
    assert [s["backend"] for s in synced["account"]["syncers"]] == ["ENABLEBANKING"]
    assert (
        synced["account"]["lastSyncedAt"]
        == synced["account"]["syncers"][0]["lastSyncedAt"]
    )
    row = await models.Transaction.objects.select_related("syncer").aget(
        account_id=acc_id
    )
    assert row.origin == "SYNC" and row.syncer.account_id == int(acc_id)


async def test_relink_adopts_an_account_known_only_by_iban(
    link: Link,
    aexecute: AExecute,
    fakebank: FakeBank,
    authenticated_context: HttpContext,
) -> None:
    """An account without syncers (e.g. from an import) is adopted by IBAN + currency instead of duplicated."""
    org = authenticated_context.request.organization
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    dead = await models.BankAccount.objects.acreate(
        organization=org, iban=f"{iban[:4]} {iban[4:]}", currency="EUR", name="Old Giro"
    )
    fakebank.scenario([account(iban=iban, name="Giro")])

    connection = await link()

    assert [a["id"] for a in connection["accounts"]] == [str(dead.id)]
    adopted = (await aexecute(ACCOUNT, {"id": dead.id})).data["bankAccount"]
    assert (
        adopted["isImportOnly"] is False
        and adopted["connection"]["id"] == connection["id"]
    )
    assert await models.BankAccount.objects.filter(organization=org).acount() == 1


async def test_relink_does_not_adopt_when_the_iban_is_ambiguous(
    link: Link, fakebank: FakeBank, authenticated_context: HttpContext
) -> None:
    org = authenticated_context.request.organization
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    for name in ("Pocket A", "Pocket B"):
        await models.BankAccount.objects.acreate(
            organization=org, iban=iban, currency="EUR", name=name
        )
    fakebank.scenario([account(iban=iban)])

    connection = await link()

    assert await models.BankAccount.objects.filter(organization=org).acount() == 3
    assert int(connection["accounts"][0]["id"]) not in {
        a.id async for a in models.BankAccount.objects.filter(name__startswith="Pocket")
    }


async def test_two_consented_accounts_with_one_iban_stay_two(
    link: Link, fakebank: FakeBank
) -> None:
    """Currency pockets or sub-accounts a bank reports under one IBAN: the second must not adopt the first."""
    iban = f"AT{uuid.uuid4().int % 10**18:018d}"
    fakebank.scenario(
        [account(iban=iban, name="Pocket A"), account(iban=iban, name="Pocket B")]
    )

    connection = await link()

    ids = {int(a["id"]) for a in connection["accounts"]}
    assert len(ids) == 2
    assert [
        await models.AccountSyncer.objects.filter(account_id=i).acount()
        for i in sorted(ids)
    ] == [1, 1]


async def test_an_account_without_syncers_is_import_only_and_refuses_sync(
    aexecute: AExecute, authenticated_context: HttpContext
) -> None:
    acc = await models.BankAccount.objects.acreate(
        organization=authenticated_context.request.organization,
        currency="EUR",
        name="Imported",
    )

    result = await aexecute(SYNC, {"id": acc.id}, allow_errors=True)

    assert extensions_of(result)["code"] == "CONNECTION_INACTIVE"
    assert (await aexecute(ACCOUNT, {"id": acc.id})).data["bankAccount"] == {
        "id": str(acc.id),
        "isImportOnly": True,
        "connection": None,
        "syncers": [],
    }
