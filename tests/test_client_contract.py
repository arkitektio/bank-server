"""The Orkestrator client contract (logins are in ``test_auth_contract``): sync budget, error codes,
bulk mutations, filters, ordering and counts — all request/response, end to end against the fakes.
"""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from finance import models
from tests.conftest import account, cash, trade, tx

pytestmark = pytest.mark.django_db(transaction=True)

BUDGET = """
query($a: ID!, $c: ID!) {
  bankAccount(id: $a) { syncsRemainingToday nextSyncAllowedAt lastError lastErrorCode }
  bankConnection(id: $c) { syncsRemainingToday nextSyncAllowedAt }
}
"""
SYNC = "mutation($id: ID!) { syncAccount(id: $id) { created } }"


def _tomorrow_utc() -> str:
    return (timezone.now().astimezone(timezone.UTC).date() + timedelta(days=1)).isoformat()


# --- §3 sync budget and §4 error codes -------------------------------------------------------


async def test_the_daily_budget_is_shown_and_enforced_without_asking_the_bank(link, aexecute, fakebank):
    fakebank.scenario([account()])
    connection = await link()
    acc = connection["accounts"][0]["id"]
    before = (await aexecute(BUDGET, {"a": acc, "c": connection["id"]})).data

    for _ in range(4):
        await aexecute(SYNC, {"id": acc})
    after = (await aexecute(BUDGET, {"a": acc, "c": connection["id"]})).data
    calls = len(fakebank.log())
    refused = await aexecute(SYNC, {"id": acc}, allow_errors=True)

    assert before["bankAccount"]["syncsRemainingToday"] == 4 and before["bankAccount"]["nextSyncAllowedAt"] is None
    assert after["bankAccount"]["syncsRemainingToday"] == 0
    assert after["bankAccount"]["nextSyncAllowedAt"].startswith(_tomorrow_utc())
    assert after["bankConnection"] == {"syncsRemainingToday": 0, "nextSyncAllowedAt": after["bankAccount"]["nextSyncAllowedAt"]}
    assert refused.errors[0].extensions["code"] == "RATE_LIMITED"
    assert refused.errors[0].extensions["nextSyncAllowedAt"].startswith(_tomorrow_utc())
    assert len(fakebank.log()) == calls  # refused before contacting the bank


async def test_a_bank_rate_limit_is_recorded_and_blocks_until_tomorrow(link, aexecute, fakebank):
    ident = "rate-" + fakebank.aspsp.replace(" ", "-")
    fakebank.scenario([account(ident)])
    connection = await link()
    acc = connection["accounts"][0]["id"]
    fakebank.set_account(ident, rate_limited=True)

    failed = await aexecute(SYNC, {"id": acc}, allow_errors=True)
    state = (await aexecute(BUDGET, {"a": acc, "c": connection["id"]})).data["bankAccount"]

    assert failed.errors[0].extensions["code"] == "RATE_LIMITED"
    assert state["lastErrorCode"] == "RATE_LIMITED"
    assert state["nextSyncAllowedAt"].startswith(_tomorrow_utc())
    assert state["syncsRemainingToday"] == 3


async def test_scalable_retry_after_sets_the_next_allowed_sync(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": {"cash": 1}})
    connection = await scalable_link()
    cash_account = next(a for a in connection["accounts"] if a["kind"] == "CASH")["id"]
    fakescalable.config(graphql_rate_limited=1)

    failed = await aexecute(SYNC, {"id": cash_account}, allow_errors=True)
    state = (await aexecute(BUDGET, {"a": cash_account, "c": connection["id"]})).data["bankAccount"]

    assert failed.errors[0].extensions["code"] == "RATE_LIMITED"
    allowed = timezone.datetime.fromisoformat(state["nextSyncAllowedAt"])
    assert timedelta(seconds=100) < allowed - timezone.now() <= timedelta(seconds=120)
    assert state["syncsRemainingToday"] is None  # Scalable has no daily limit


async def test_expired_consent_is_coded_on_account_and_connection(link, aexecute, fakebank):
    fakebank.scenario([account()])
    connection = await link()
    acc = connection["accounts"][0]["id"]
    stored = await models.BankConnection.objects.aget(id=connection["id"])
    fakebank.expire(stored.session_id)

    failed = await aexecute(SYNC, {"id": acc}, allow_errors=True)
    state = (await aexecute("query($c: ID!) { bankConnection(id: $c) { status lastErrorCode accounts { lastErrorCode } } }", {"c": connection["id"]})).data["bankConnection"]

    assert failed.errors[0].extensions["code"] == "CONSENT_EXPIRED"
    assert state == {"status": "EXPIRED", "lastErrorCode": "CONSENT_EXPIRED", "accounts": [{"lastErrorCode": "CONSENT_EXPIRED"}]}


# --- §5 bulk mutations -----------------------------------------------------------------------


async def _synced_bank(link, aexecute, fakebank, rows) -> tuple[str, list[str]]:  # noqa: ANN001
    fakebank.scenario([account(transactions=rows)])
    acc = (await link())["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc})
    ids = [t["id"] for t in (await aexecute("query($a: ID!) { transactions(filters: {accounts: [$a]}, ordering: [{bookingDate: ASC}]) { id } }", {"a": acc})).data["transactions"]]
    return acc, ids


async def test_bulk_categorize_and_clear(link, aexecute, fakebank, other_org_context):
    acc, ids = await _synced_bank(link, aexecute, fakebank, [tx("-10.00", "2026-09-01", "Shop A"), tx("-20.00", "2026-09-02", "Shop B"), tx("-30.00", "2026-09-03", "Shop C")])
    groceries = (await aexecute('{ categories(filters: {search: "Groceries"}) { id } }')).data["categories"][0]["id"]

    done = (await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id category { id } categorySource } }", {"ids": ids, "c": groceries})).data["categorizeTransactions"]
    cleared = (await aexecute("mutation($ids: [ID!]!) { categorizeTransactions(ids: $ids) { categorySource } }", {"ids": ids[:2]})).data["categorizeTransactions"]
    foreign = await aexecute("mutation($ids: [ID!]!) { categorizeTransactions(ids: $ids) { id } }", {"ids": ids}, context=other_org_context, allow_errors=True)

    assert [t["id"] for t in done] == ids
    assert all(t["category"]["id"] == groceries and t["categorySource"] == "MANUAL" for t in done)
    assert [t["categorySource"] for t in cleared] == ["NONE", "NONE"]
    assert foreign.errors[0].extensions["code"] == "NOT_FOUND"


async def test_bulk_mark_transfers_pins_and_unpins(link, aexecute, fakebank):
    acc, ids = await _synced_bank(link, aexecute, fakebank, [tx("-10.00", "2026-09-01", "A"), tx("-20.00", "2026-09-02", "B")])

    pinned = (await aexecute("mutation($ids: [ID!]!) { markTransfers(ids: $ids, isTransfer: true) { isTransfer isTransferManual } }", {"ids": ids})).data["markTransfers"]
    auto = (await aexecute("mutation($ids: [ID!]!) { markTransfers(ids: $ids) { isTransfer isTransferManual } }", {"ids": ids})).data["markTransfers"]

    assert pinned == [{"isTransfer": True, "isTransferManual": True}] * 2
    assert auto == [{"isTransfer": False, "isTransferManual": False}] * 2


async def test_unpinning_a_scalable_trade_restores_the_provider_flag(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": {"cash": 1, "transactions": [trade(-100, "2026-09-10T10:00:00.000Z", "IE00TEST0001")]}})
    connection = await scalable_link()
    cash_account = next(a for a in connection["accounts"] if a["kind"] == "CASH")["id"]
    await aexecute(SYNC, {"id": cash_account})
    [buy] = (await aexecute("query($a: ID!) { transactions(filters: {accounts: [$a]}) { id } }", {"a": cash_account})).data["transactions"]

    await aexecute("mutation($ids: [ID!]!) { markTransfers(ids: $ids, isTransfer: false) { id } }", {"ids": [buy["id"]]})
    restored = (await aexecute("mutation($ids: [ID!]!) { markTransfers(ids: $ids) { isTransfer isTransferManual } }", {"ids": [buy["id"]]})).data["markTransfers"]

    assert restored == [{"isTransfer": True, "isTransferManual": False}]


async def test_bulk_recurring_statuses(link, aexecute, fakebank):
    fakebank.scenario([account()])
    acc_id = int((await link())["accounts"][0]["id"])
    acc = await models.BankAccount.objects.aget(id=acc_id)
    patterns = [
        await models.RecurringPayment.objects.acreate(organization_id=acc.organization_id, account=acc, key=f"k{i}", label=f"P{i}", amount="-9.99", currency="EUR", interval_days=30, last_seen=date(2026, 9, 1), next_expected=date(2026, 10, 1))
        for i in range(3)
    ]

    result = (await aexecute("mutation($ids: [ID!]!) { setRecurringStatuses(ids: $ids, status: CONFIRMED) { id status } }", {"ids": [str(p.id) for p in patterns]})).data["setRecurringStatuses"]

    assert [r["status"] for r in result] == ["CONFIRMED"] * 3


# --- §6 filters and ordering, §7 counts ------------------------------------------------------


async def test_accounts_by_kind_and_ordered(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": {"cash": 1}}, savings={"s1": {"total": 1}})
    await scalable_link()

    depots = (await aexecute("{ bankAccounts(filters: {kind: DEPOT}) { kind } }")).data["bankAccounts"]
    ordered = (await aexecute("{ bankAccounts(ordering: [{name: ASC}]) { name } }")).data["bankAccounts"]

    assert depots == [{"kind": "DEPOT"}]
    names = [a["name"] for a in ordered]
    assert names == sorted(names)


async def test_categories_and_recurring_ordering(link, aexecute, fakebank):
    fakebank.scenario([account()])
    await link()

    names = [c["name"] for c in (await aexecute("{ categories(ordering: [{name: ASC}]) { name } }")).data["categories"]]
    # Postgres collation, not Python's sort order ("Fitness & Sports" vs "Food").
    by_db = [n async for n in models.Category.objects.filter(organization__slug="static_org").order_by("name").values_list("name", flat=True)]

    assert names == by_db and len(names) > 3
    assert (await aexecute("{ recurringPayments(ordering: [{nextExpected: ASC}]) { id } } ")).data["recurringPayments"] == []
    assert (await aexecute("{ budgets(ordering: [{amount: DESC}]) { id } }")).data["budgets"] == []


async def test_child_categories_kinds_and_counts(link, aexecute, fakebank):
    acc, ids = await _synced_bank(link, aexecute, fakebank, [tx("-10.00", "2026-09-01", "A"), tx("-20.00", "2026-09-02", "B"), tx("-5.00", "2026-09-03", "C")])
    parent = (await aexecute('mutation { createCategory(input: {name: "Household"}) { id } }')).data["createCategory"]["id"]
    child = (await aexecute("mutation($p: ID!) { createCategory(input: {name: \"Cleaning\", parent: $p}) { id } }", {"p": parent})).data["createCategory"]["id"]
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": ids[:1], "c": parent})
    await aexecute("mutation($ids: [ID!]!, $c: ID) { categorizeTransactions(ids: $ids, category: $c) { id } }", {"ids": ids[1:2], "c": child})

    flat = (await aexecute("query($c: ID!) { transactions(filters: {categories: [$c]}) { id } }", {"c": parent})).data["transactions"]
    tree = (await aexecute("query($c: ID!) { transactions(filters: {categories: [$c], includeChildCategories: true}) { id } }", {"c": parent})).data["transactions"]
    counts = (await aexecute("query($a: ID!) { all: transactionsCount(filters: {accounts: [$a]}) open: transactionsCount(filters: {accounts: [$a], uncategorized: true}) }", {"a": acc})).data

    assert len(flat) == 1 and len(tree) == 2
    assert counts == {"all": 3, "open": 1}


async def test_transaction_kind_is_typed_and_filterable(scalable_link, aexecute, fakescalable):
    rows = [trade(-100, "2026-09-10T10:00:00.000Z", "IE00TEST0001"), cash(5, "2026-09-11T00:00:00.000Z", "DISTRIBUTION"), cash(1000, "2026-09-01T00:00:00.000Z", "DEPOSIT"), cash(1, "2026-09-02T00:00:00.000Z", "SOMETHING_NEW")]
    fakescalable.seed({"p1": {"cash": 1, "transactions": rows}})
    connection = await scalable_link()
    cash_account = next(a for a in connection["accounts"] if a["kind"] == "CASH")["id"]
    await aexecute(SYNC, {"id": cash_account})

    trades = (await aexecute("{ transactions(filters: {kinds: [BUY, SELL]}) { kind } }")).data["transactions"]
    payouts = (await aexecute("{ transactions(filters: {kind: DISTRIBUTION}) { kind } }")).data["transactions"]
    unknown = (await aexecute("{ transactions(filters: {kind: OTHER}) { kind } }")).data["transactions"]

    assert trades == [{"kind": "BUY"}] and payouts == [{"kind": "DISTRIBUTION"}] and unknown == [{"kind": "OTHER"}]


async def test_budget_fields_resolve_in_nested_lists(link, aexecute, fakebank):
    fakebank.scenario([account(), account()])
    connection = await link()
    await aexecute(SYNC, {"id": connection["accounts"][0]["id"]})

    listed = (await aexecute("{ bankConnections { syncsRemainingToday nextSyncAllowedAt accounts { syncsRemainingToday nextSyncAllowedAt lastErrorCode } } }")).data["bankConnections"]
    flat = (await aexecute("{ bankAccounts(filters: {kind: CASH}) { syncsRemainingToday } }")).data["bankAccounts"]

    [conn] = [c for c in listed if c["accounts"]]
    assert sorted(a["syncsRemainingToday"] for a in conn["accounts"]) == [3, 4]
    assert conn["syncsRemainingToday"] == 3
    assert sorted(a["syncsRemainingToday"] for a in flat) == [3, 4]
