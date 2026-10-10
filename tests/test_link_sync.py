"""Linking a bank and syncing it, end to end against fakebank.

The link is started and completed through GraphQL exactly as a client would, the user's
approval at the bank is fakebank's /_admin/approve, and every sync goes over real HTTP.
"""

import pytest

from finance import models
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

SYNC = """
mutation Sync($id: ID!) { syncAccount(id: $id) { created updated pendingReplaced balances account { id lastSyncedAt latestBalance { amount currency } } } }
"""
TXS = """
query Txs($acc: ID!) { transactions(filters: {accounts: [$acc]}, ordering: [{bookingDate: ASC}]) { id amount status counterparty note category { name } categorySource } }
"""


async def test_link_creates_accounts_and_default_categories(link, fakebank):
    fakebank.scenario([account(iban="AT111", balance="100.00"), account(iban="AT222", currency="USD")])

    connection = await link()

    assert connection["status"] == "ACTIVE"
    assert connection["validUntil"] is not None
    assert sorted(a["iban"] for a in connection["accounts"]) == ["AT111", "AT222"]
    assert await models.Category.objects.filter(name="Groceries").aexists()


async def test_complete_rejects_unknown_state(aexecute, fakebank):
    fakebank.scenario([account()])
    result = await aexecute(
        'mutation { completeAuth(input: {code: "x", state: "not-a-state"}) { status } }', allow_errors=True
    )
    assert result.errors[0].extensions["code"] == "INVALID_STATE"


async def test_unregistered_redirect_is_refused(aexecute, fakebank, eb_provider):
    fakebank.scenario([account()])
    result = await aexecute(
        'mutation($a: String!, $p: ID!) { startLink(input: {provider: $p, institution: $a, country: "AT", redirectUrl: "https://evil.test/cb"}) { state } }',
        {"a": fakebank.aspsp, "p": eb_provider},
        allow_errors=True,
    )
    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_sync_pages_and_is_idempotent(link, aexecute, fakebank):
    # 7 rows > fakebank's page size of 3, so the continuation key is followed twice.
    rows = [tx(f"-{i}.00", f"2026-09-0{i}", f"Shop {i}") for i in range(1, 8)]
    fakebank.scenario([account(transactions=rows, balance="500.00")])
    acc_id = (await link())["accounts"][0]["id"]

    first = (await aexecute(SYNC, {"id": acc_id})).data["syncAccount"]
    assert first["created"] == 7
    assert first["balances"] == 1
    assert first["account"]["latestBalance"] == {"amount": "500.00", "currency": "EUR"}

    second = (await aexecute(SYNC, {"id": acc_id})).data["syncAccount"]
    assert (second["created"], second["updated"]) == (0, 0)
    assert await models.Transaction.objects.filter(account_id=acc_id).acount() == 7


async def test_genuine_duplicates_are_kept(link, aexecute, fakebank):
    coffee = tx("-3.20", "2026-09-10", "Cafe")
    fakebank.scenario([account(transactions=[coffee, dict(coffee)])])
    acc_id = (await link())["accounts"][0]["id"]

    await aexecute(SYNC, {"id": acc_id})
    await aexecute(SYNC, {"id": acc_id})

    assert await models.Transaction.objects.filter(account_id=acc_id).acount() == 2


async def test_annotations_survive_resync_and_pending_is_replaced(link, aexecute, fakebank):
    ident = "acc-annot"
    booked = tx("-50.00", "2026-09-01", "Grocer", ref="REF1")
    pending = tx("-9.99", "2026-09-05", "Streaming", status="PDNG")
    fakebank.scenario([account(ident, transactions=[booked, pending])])
    acc_id = (await link())["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc_id})

    rows = (await aexecute(TXS, {"acc": acc_id})).data["transactions"]
    booked_id = next(r["id"] for r in rows if r["status"] == "BOOKED")
    groceries = await models.Category.objects.aget(name="Groceries")
    await aexecute('mutation($id: ID!, $c: ID!) { categorizeTransaction(input: {id: $id, category: $c}) { id } }', {"id": booked_id, "c": str(groceries.id)})
    await aexecute('mutation($id: ID!) { setTransactionNote(input: {id: $id, note: "weekly shop"}) { id } }', {"id": booked_id})

    # The pending payment books, and the bank corrects the booked row's remittance.
    corrected = {**booked, "remittance_information": ["corrected"]}
    fakebank.set_account(ident, transactions=[corrected, tx("-9.99", "2026-09-06", "Streaming")])
    result = (await aexecute(SYNC, {"id": acc_id})).data["syncAccount"]
    assert result["pendingReplaced"] == 1
    assert result["updated"] == 1

    rows = (await aexecute(TXS, {"acc": acc_id})).data["transactions"]
    assert [r["status"] for r in rows] == ["BOOKED", "BOOKED"]
    annotated = next(r for r in rows if r["id"] == booked_id)
    assert annotated["note"] == "weekly shop"
    assert annotated["category"] == {"name": "Groceries"}
    assert annotated["categorySource"] == "MANUAL"


async def test_attended_sync_sends_psu_headers(link, aexecute, fakebank):
    fakebank.scenario([account(transactions=[tx("-1.00", "2026-09-01", "X")])])
    acc_id = (await link())["accounts"][0]["id"]
    uid = (await models.AccountSyncer.objects.aget(account_id=acc_id)).remote_id

    await aexecute(SYNC, {"id": acc_id})

    calls = [c for c in fakebank.log() if c["path"] == f"/accounts/{uid}/transactions"]
    assert calls and all(c["psu_ip"] == "203.0.113.7" and c["psu_user_agent"] == "bank-tests" for c in calls)


async def test_expired_consent_needs_reauth_and_relink_keeps_history(link, aexecute, fakebank):
    ident = "acc-relink"
    fakebank.scenario([account(ident, transactions=[tx("-5.00", "2026-09-01", "A", ref="R1")])])
    first = await link()
    acc_id = first["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc_id})
    connection = await models.BankConnection.objects.aget(id=first["id"])
    fakebank.expire(connection.session_id)

    failed = await aexecute(SYNC, {"id": acc_id}, allow_errors=True)
    assert failed.errors[0].extensions["code"] == "CONSENT_EXPIRED"
    status = await aexecute('query($id: ID!) { bankConnection(id: $id) { status needsReauth } }', {"id": first["id"]})
    assert status.data["bankConnection"] == {"status": "EXPIRED", "needsReauth": True}

    second = await link()
    assert second["accounts"][0]["id"] == acc_id  # same account, re-attached
    assert await models.Transaction.objects.filter(account_id=acc_id).acount() == 1


async def test_transfers_between_own_accounts_are_flagged(link, aexecute, fakebank):
    fakebank.scenario(
        [
            account(iban="AT00OWN1", transactions=[tx("-200.00", "2026-09-02", "Me", iban="AT00 OWN2")]),
            account(iban="AT00OWN2", transactions=[tx("200.00", "2026-09-02", "Me", iban="AT00OWN1")]),
        ]
    )
    for acc in (await link())["accounts"]:
        await aexecute(SYNC, {"id": acc["id"]})

    assert await models.Transaction.objects.filter(is_transfer=True).acount() == 2


async def test_bank_institutions_lists_linkable_banks(aexecute, fakebank, eb_provider):
    fakebank.scenario([account()])
    result = await aexecute('query($p: ID!) { bankInstitutions(provider: $p, country: "at") { name country maximumConsentDays } }', {"p": eb_provider})
    assert {"name": fakebank.aspsp, "country": "AT", "maximumConsentDays": 180} in result.data["bankInstitutions"]


async def test_syncing_a_revoked_connection_keeps_it_revoked(link, aexecute, fakebank):
    fakebank.scenario([account()])
    connection = await link()
    await aexecute('mutation($id: ID!) { revokeBankConnection(id: $id) { status } }', {"id": connection["id"]})

    result = await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": connection["accounts"][0]["id"]}, allow_errors=True)

    assert result.errors[0].extensions["code"] == "CONNECTION_INACTIVE"
    status = await aexecute('query($id: ID!) { bankConnection(id: $id) { status needsReauth } }', {"id": connection["id"]})
    assert status.data["bankConnection"] == {"status": "REVOKED", "needsReauth": False}


async def test_old_pending_rows_outside_the_fetch_window_are_kept(link, aexecute, fakebank):
    """A pending row older than the incremental window is not re-sent, so it must not be dropped."""
    ident = "acc-old-pending"
    old_pending = tx("-7.00", "2026-08-01", "Hotel deposit", status="PDNG")
    booked = tx("-1.00", "2026-09-20", "Kiosk", ref="K1")
    fakebank.scenario([account(ident, transactions=[old_pending, booked])])
    acc_id = (await link())["accounts"][0]["id"]
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": acc_id})

    # Next fetch starts 2026-09-13; the bank (like fakebank) only returns rows from then on.
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": acc_id})

    assert await models.Transaction.objects.filter(account_id=acc_id, counterparty="Hotel deposit").aexists()


async def test_sync_runs_recurring_detection(link, aexecute, fakebank):
    rows = [tx("-900.00", f"2026-{m:02d}-01", "Landlord") for m in (5, 6, 7, 8, 9)]
    fakebank.scenario([account(transactions=rows)])
    acc_id = (await link())["accounts"][0]["id"]
    await aexecute('mutation($id: ID!) { syncAccount(id: $id) { created } }', {"id": acc_id})

    pattern = await models.RecurringPayment.objects.aget(account_id=acc_id)
    assert (pattern.label, pattern.interval_days) == ("Landlord", 30)
