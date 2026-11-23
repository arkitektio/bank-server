"""A guest of the organization, granted a collaboration and nothing else: what they read of the bank
account put in it, and that they change nothing.

Their token lists their collaborations by key (`collabs`: lok's key, one across the hub), with the
roles they hold in each (conftest `guest`). Tenant A ("test") links the fake bank, syncs two
accounts and puts one in a collaboration under a fresh key (`addToCollaboration`, which makes
bank's row of it). Its transactions, balances and recurring payments are in it with it; the
categories its transactions are in come along. How it is pulled from the bank (its syncers, the
connection, the provider) never is, nor are statement imports, merchants and budgets.
"""

import datetime
import uuid
from contextlib import aclosing

import pytest
from arkitekt_service.collaborations.models import Collaboration, CollaborationItem
from authentikate.models import Organization
from kante.context import HttpContext
from strawberry.types import ExecutionResult

from bank_server.schema import schema
from finance import models
from tests.conftest import (
    AExecute,
    FakeBank,
    GuestFactory,
    Link,
    account,
    request_context,
    tx,
)

pytestmark = pytest.mark.django_db(transaction=True)

ADD = "mutation ($input: CollaborationObjectInput!) { addToCollaboration(input: $input) { key } }"
ACCOUNT = """
query ($id: ID!) {
  bankAccount(id: $id) {
    id iban name
    transactions { counterparty amount category { name parent { name } } merchant { id } syncer { id } }
    balances { amount }
    syncers { id }
    connection { id }
    lastSyncedAt lastError isImportOnly isSyncing syncsRemainingToday
  }
}
"""


async def _run(ctx: HttpContext, query: str, **variables: object) -> ExecutionResult:
    return await schema.execute(
        query, context_value=ctx, variable_values=variables or None
    )


def _refused(result: ExecutionResult, reason: str) -> None:
    assert result.errors, "it was let through"
    extensions = result.errors[0].extensions or {}
    assert extensions.get("reason") == reason, result.errors


async def _share(identifier: str, pk: object, key: str | None = None) -> str:
    """Put ``identifier`` ``pk`` in the collaboration ``key`` (a fresh one), as tenant A; its key."""
    key = key or str(uuid.uuid4())
    added = await _run(
        request_context("test"),
        ADD,
        input={
            "collaboration": key,
            "name": "With the accountant",
            "identifier": identifier,
            "object": str(pk),
        },
    )
    assert not added.errors, added.errors
    return key


@pytest.fixture
async def accounts(
    link: Link, aexecute: AExecute, fakebank: FakeBank
) -> dict[str, models.BankAccount]:
    """Two synced accounts of tenant A: a giro, whose shop purchase is in a nested category, and
    a private savings account."""
    fakebank.scenario(
        [
            account(
                name="Giro",
                transactions=[
                    tx("-10.00", "2026-09-01", "Shop"),
                    tx("2000.00", "2026-09-02", "Employer"),
                ],
                balance="1990.00",
            ),
            account(
                name="Savings",
                transactions=[tx("-500.00", "2026-09-03", "Secret Hobby")],
                balance="9000.00",
            ),
        ]
    )
    connection = await link()
    for linked in connection["accounts"]:
        await aexecute(
            "mutation($id: ID!) { syncAccount(id: $id) { created } }",
            {"id": linked["id"]},
        )
    giro = await models.BankAccount.objects.aget(name="Giro")
    savings = await models.BankAccount.objects.aget(name="Savings")
    organization = await Organization.objects.aget(
        identifier="static_issuer#static_org"
    )
    food = await models.Category.objects.acreate(
        organization=organization, name="Food (test)", kind="EXPENSE"
    )
    snacks = await models.Category.objects.acreate(
        organization=organization, name="Snacks (test)", kind="EXPENSE", parent=food
    )
    await models.Transaction.objects.filter(account=giro, counterparty="Shop").aupdate(
        category=snacks
    )
    await models.Budget.objects.acreate(
        organization=organization,
        category=snacks,
        amount="50",
        currency="EUR",
        start_month=datetime.date(2026, 9, 1),
    )
    return {"giro": giro, "savings": savings}


async def test_a_guest_reads_the_shared_account_and_nothing_else(
    accounts: dict[str, models.BankAccount], guest: GuestFactory
) -> None:
    giro, savings = accounts["giro"], accounts["savings"]
    ctx = guest({await _share("@bank/bankaccount", giro.pk): []})

    listed = await _run(
        ctx,
        "{ bankAccounts { name } transactions { counterparty } categories { name } }",
    )
    assert not listed.errors, listed.errors
    assert listed.data is not None
    assert listed.data["bankAccounts"] == [{"name": "Giro"}]
    assert sorted(t["counterparty"] for t in listed.data["transactions"]) == [
        "Employer",
        "Shop",
    ]
    # Its purchase's category, and the one that is nested in, came along.
    assert sorted(c["name"] for c in listed.data["categories"]) == [
        "Food (test)",
        "Snacks (test)",
    ]

    read = await _run(ctx, ACCOUNT, id=str(giro.pk))
    assert not read.errors, read.errors
    assert read.data is not None
    seen = read.data["bankAccount"]
    shop = next(t for t in seen["transactions"] if t["counterparty"] == "Shop")
    assert shop["category"] == {
        "name": "Snacks (test)",
        "parent": {"name": "Food (test)"},
    }
    assert shop["syncer"] is None and shop["merchant"] is None
    assert len(seen["balances"]) == 1
    # How the account is pulled from the bank is the organization's.
    assert seen["syncers"] == [] and seen["connection"] is None
    assert seen["lastSyncedAt"] is None and seen["lastError"] is None
    member = await _run(request_context("test"), ACCOUNT, id=str(giro.pk))
    assert member.data is not None
    assert member.data["bankAccount"]["lastSyncedAt"] is not None
    assert len(member.data["bankAccount"]["syncers"]) == 1
    assert seen["isSyncing"] is False and seen["syncsRemainingToday"] is None

    assert (await _run(ctx, ACCOUNT, id=str(savings.pk))).errors
    nothing = await _run(
        ctx,
        "{ bankProviders { id } bankConnections { id } statementImports { id } merchants { id } budgets { id } categoryRules { id } }",
    )
    assert not nothing.errors, nothing.errors
    assert nothing.data is not None
    assert all(rows == [] for rows in nothing.data.values()), nothing.data


async def test_a_transaction_goes_in_with_its_account_never_alone(
    accounts: dict[str, models.BankAccount],
) -> None:
    lone = await models.Transaction.objects.filter(account=accounts["savings"]).afirst()
    assert lone is not None
    key = str(uuid.uuid4())
    put = await _run(
        request_context("test"),
        ADD,
        input={
            "collaboration": key,
            "identifier": "@bank/transaction",
            "object": str(lone.pk),
        },
    )
    assert put.errors and "is part of" in put.errors[0].message, put.errors
    assert not await Collaboration.objects.filter(key=key).aexists()


async def test_what_holds_or_reaches_credentials_never_goes_in(
    accounts: dict[str, models.BankAccount], guest: GuestFactory
) -> None:
    key = await _share("@bank/bankaccount", accounts["giro"].pk)
    connection = await models.BankConnection.objects.afirst()
    provider = await models.BankProvider.objects.afirst()
    budget = await models.Budget.objects.afirst()
    assert connection is not None and provider is not None and budget is not None
    for identifier, pk in [
        ("@bank/bankconnection", connection.pk),
        ("@bank/bankprovider", provider.pk),
        ("@bank/budget", budget.pk),
    ]:
        put = await _run(
            request_context("test"),
            ADD,
            input={"collaboration": key, "identifier": identifier, "object": str(pk)},
        )
        assert put.errors and "never put in a collaboration" in put.errors[0].message, (
            identifier,
            put.errors,
        )
    held = {
        item.content_type.model
        async for item in CollaborationItem.objects.filter(
            collaboration__key=key
        ).select_related("content_type")
    }
    assert held == {"bankaccount", "category"}, held


async def test_no_guest_changes_anything_not_even_an_editor(
    accounts: dict[str, models.BankAccount], guest: GuestFactory
) -> None:
    giro = accounts["giro"]
    key = await _share("@bank/bankaccount", giro.pk)
    shop = await models.Transaction.objects.aget(account=giro, counterparty="Shop")

    for ctx in (guest({key: []}), guest({key: ["bank_editor"]})):
        for document, variables in [
            (
                "mutation ($id: ID!) { syncAccount(id: $id) { created } }",
                {"id": str(giro.pk)},
            ),
            (
                'mutation ($id: ID!) { setTransactionNote(input: {id: $id, note: "mine"}) { id } }',
                {"id": str(shop.pk)},
            ),
            (
                'mutation { createCategory(input: {name: "Guest"}) { id } }',
                {},
            ),
            (
                ADD,
                {
                    "input": {
                        "collaboration": key,
                        "identifier": "@bank/bankaccount",
                        "object": str(accounts["savings"].pk),
                    }
                },
            ),
            (
                "mutation ($input: ShareWorkflowInput!) { shareWorkflow(input: $input) { dryRun } }",
                {"input": {"collaboration": key, "rootTask": "any", "dryRun": True}},
            ),
            (
                "mutation ($key: ID!) { deleteCollaboration(key: $key) }",
                {"key": key},
            ),
        ]:
            _refused(await _run(ctx, document, **variables), "SCOPED_READ_ONLY")

    await shop.arefresh_from_db()
    assert not shop.note
    assert not await models.Category.objects.filter(name="Guest").aexists()
    assert await CollaborationItem.objects.filter(collaboration__key=key).acount() == 3


async def test_a_guest_subscribes_to_nothing_and_computes_nothing_over_the_organization(
    accounts: dict[str, models.BankAccount], guest: GuestFactory
) -> None:
    ctx = guest({await _share("@bank/bankaccount", accounts["giro"].pk): []})

    subscribed = await schema.subscribe(
        "subscription { accountSyncs { accountId } }", context_value=ctx
    )
    if not isinstance(subscribed, ExecutionResult):
        async with aclosing(subscribed) as stream:
            subscribed = await anext(stream)
    _refused(subscribed, "SCOPED_READ_ONLY")

    # Aggregates are computed over every account of the organization: closed to guests.
    for document in (
        "{ spendingByCategory { net } }",
        "{ cashflow { income } }",
        "{ balanceTotals { currency } }",
        "{ providerKinds { kind } }",
    ):
        _refused(await _run(ctx, document), "COLLABORATION_DOES_NOT_REACH")


async def test_a_member_lists_what_is_in_a_collaboration(
    accounts: dict[str, models.BankAccount], guest: GuestFactory
) -> None:
    key = await _share("@bank/bankaccount", accounts["giro"].pk)
    await _share("@bank/bankaccount", accounts["savings"].pk)  # another, not granted

    member = request_context("test")
    listed = await _run(
        member,
        "query ($key: ID!, $a: BankAccountFilter, $t: TransactionFilter) { collaboration(key: $key) { key itemCount(brought: false) } bankAccounts(filters: $a) { name } transactions(filters: $t) { counterparty } }",
        key=key,
        a={"inCollaboration": key},
        t={"inCollaboration": key},
    )
    assert not listed.errors, listed.errors
    assert listed.data is not None
    assert listed.data["collaboration"] == {"key": key, "itemCount": 1}
    assert listed.data["bankAccounts"] == [{"name": "Giro"}]
    assert sorted(t["counterparty"] for t in listed.data["transactions"]) == [
        "Employer",
        "Shop",
    ]

    granted = await _run(guest({key: []}), "{ collaborations { key name } }")
    assert not granted.errors, granted.errors
    assert granted.data == {
        "collaborations": [{"key": key, "name": "With the accountant"}]
    }


def _type_line(sdl: str, name: str) -> str:
    return next(line for line in sdl.splitlines() if line.startswith(f"type {name} "))


def test_the_schema_says_what_each_type_is_and_opens_no_write_to_guests() -> None:
    sdl = str(schema)
    assert '@identified(by: "@bank/bankaccount")' in _type_line(sdl, "BankAccount")
    assert '@identified(by: "@bank/transaction")' in _type_line(sdl, "Transaction")
    assert "allow_scoped: true" not in sdl
