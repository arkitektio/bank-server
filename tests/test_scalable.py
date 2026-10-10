"""Linking Scalable Capital and syncing it, end to end against fakescalable.

The device login is started and advanced through GraphQL exactly as a client would; the user's
approval of the login code (and of the second factor) is fakescalable's /_admin; every token
and GraphQL call goes over real HTTP with real DPoP proofs.
"""

import asyncio
import threading
import time

import pytest

from finance import models
from finance.scalable.client import ScalableClient
from finance.scalable.dpop import DpopKey
from finance.scalable.queries import OPERATIONS
from finance.scalable.tokens import session_for
from tests.conftest import SCALABLE_COMPLETE, SCALABLE_START, cash, connection_of, holding, trade

pytestmark = pytest.mark.django_db(transaction=True)

SYNC_CONNECTION = """
mutation Sync($id: ID!) { syncConnection(id: $id) { created updated balances holdings account { id kind latestBalance { amount balanceType } } } }
"""
SYNC_ACCOUNT = """
mutation Sync($id: ID!) { syncAccount(id: $id) { created updated balances holdings } }
"""
TXS = """
query Txs($acc: ID!) { transactions(filters: {accounts: [$acc]}, ordering: [{bookingDate: ASC}]) { id amount status kind isin quantity counterparty isTransfer note } }
"""
HOLDINGS = """
query H($acc: ID!) { bankAccount(id: $acc) { currentHoldings { isin quantity price fifoPrice valuation unrealizedGain } } }
"""


def portfolio(**overrides) -> dict:
    data = {
        "cash": 1234.56,
        "holdings": [holding("IE00TEST0001", 10, 100.0, 80.0), holding("IE00TEST0002", 2.5, 40.0, 50.0)],
        "transactions": [
            cash(12.34, "2026-09-20T00:00:00.000Z", "DISTRIBUTION", isin="IE00TEST0001", description="Fund IE00TEST0001"),
            trade(-500, "2026-09-10T21:57:43.636Z", "IE00TEST0001", quantity=5),
            cash(3000, "2026-09-02T00:00:00.000Z", "DEPOSIT", description="Sent from N26"),
        ],
    }
    return {**data, **overrides}


def by_kind(accounts: list[dict]) -> dict[str, dict]:
    return {a["kind"]: a for a in accounts}


async def test_link_creates_cash_depot_and_savings_accounts(scalable_link, fakescalable):
    fakescalable.seed({"p1": portfolio()}, savings={"s1": {"total": 500, "name": "Tagesgeld"}})

    connection = await scalable_link()

    accounts = by_kind(connection["accounts"])
    assert set(accounts) == {"CASH", "DEPOT", "SAVINGS"}
    assert {s.remote_id async for s in models.AccountSyncer.objects.filter(connection_id=connection["id"], account__kind__in=["CASH", "DEPOT"])} == {"p1"}
    assert accounts["SAVINGS"]["name"] == "Tagesgeld"
    stored = await models.BankConnection.objects.aget(id=connection["id"])
    assert stored.provider == models.Provider.SCALABLE and stored.provider_user_id == fakescalable.person
    assert stored.secret and "BEGIN" not in stored.secret  # encrypted at rest
    assert await models.Category.objects.filter(name="Groceries").aexists()


async def test_sync_stores_transactions_balances_and_holdings(scalable_link, aexecute, fakescalable):
    savings_tx = [cash(1.5, "2026-09-01T00:00:00.000Z", "INTEREST")]
    fakescalable.seed({"p1": portfolio()}, savings={"s1": {"total": 500, "transactions": savings_tx}})
    connection = await scalable_link()

    results = (await aexecute(SYNC_CONNECTION, {"id": connection["id"]})).data["syncConnection"]

    by = {r["account"]["kind"]: r for r in results}
    assert by["CASH"]["created"] == 3 and by["CASH"]["account"]["latestBalance"] == {"amount": "1234.56", "balanceType": "CLBD"}
    assert by["DEPOT"]["holdings"] == 2 and by["DEPOT"]["account"]["latestBalance"] == {"amount": "1100.00", "balanceType": "VALU"}
    assert by["SAVINGS"]["created"] == 1 and by["SAVINGS"]["account"]["latestBalance"]["amount"] == "500.00"

    txs = (await aexecute(TXS, {"acc": by["CASH"]["account"]["id"]})).data["transactions"]
    assert [(t["kind"], t["amount"], t["isTransfer"]) for t in txs] == [("DEPOSIT", "3000.00", True), ("BUY", "-500.00", True), ("DISTRIBUTION", "12.34", False)]
    assert txs[1]["isin"] == "IE00TEST0001" and txs[1]["quantity"] == "5.00000000"

    positions = (await aexecute(HOLDINGS, {"acc": by["DEPOT"]["account"]["id"]})).data["bankAccount"]["currentHoldings"]
    assert [(p["isin"], p["valuation"], p["unrealizedGain"]) for p in positions] == [("IE00TEST0001", "1000.00", "200.00"), ("IE00TEST0002", "100.00", "-25.00")]


async def test_resync_is_idempotent_and_pending_order_updates_in_place(scalable_link, aexecute, fakescalable):
    order = trade(-250, "2026-09-24T09:00:00.000Z", "IE00TEST0002", status="PENDING", id="order-1")
    fakescalable.seed({"p1": portfolio(transactions=[order])})
    accounts = by_kind((await scalable_link())["accounts"])
    cash_id = accounts["CASH"]["id"]
    await aexecute(SYNC_ACCOUNT, {"id": cash_id})
    [pending] = (await aexecute(TXS, {"acc": cash_id})).data["transactions"]
    assert pending["status"] == "PENDING"
    await aexecute('mutation($id: ID!) { setTransactionNote(input: {id: $id, note: "rebalance"}) { id } }', {"id": pending["id"]})

    fakescalable.seed({"p1": portfolio(transactions=[{**order, "status": "SETTLED", "lastEventDateTime": "2026-09-25T09:00:00.000Z"}])})
    moved = (await aexecute(SYNC_ACCOUNT, {"id": cash_id})).data["syncAccount"]
    again = (await aexecute(SYNC_ACCOUNT, {"id": cash_id})).data["syncAccount"]

    [booked] = (await aexecute(TXS, {"acc": cash_id})).data["transactions"]
    assert (moved["created"], moved["updated"]) == (0, 1)
    assert (again["created"], again["updated"]) == (0, 0)
    assert booked["id"] == pending["id"] and booked["status"] == "BOOKED" and booked["note"] == "rebalance"


async def test_holdings_of_the_day_are_replaced_on_resync(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    depot = by_kind((await scalable_link())["accounts"])["DEPOT"]["id"]
    await aexecute(SYNC_ACCOUNT, {"id": depot})

    fakescalable.seed({"p1": portfolio(holdings=[holding("IE00TEST0001", 12, 101.0, 81.0)])})
    await aexecute(SYNC_ACCOUNT, {"id": depot})

    positions = (await aexecute(HOLDINGS, {"acc": depot})).data["bankAccount"]["currentHoldings"]
    assert [(p["isin"], p["quantity"]) for p in positions] == [("IE00TEST0001", "12.00000000")]
    assert await models.HoldingSnapshot.objects.filter(account_id=depot).acount() == 1


async def test_trades_and_deposits_stay_out_of_spending(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio(transactions=[*portfolio()["transactions"], cash(-4.99, "2026-09-15T00:00:00.000Z", "FEE", description="Crypto fee")])})
    cash_id = by_kind((await scalable_link())["accounts"])["CASH"]["id"]
    await aexecute(SYNC_ACCOUNT, {"id": cash_id})

    flow = (await aexecute('query($a: ID!) { cashflow(accounts: [$a]) { income expense count } }', {"a": cash_id})).data["cashflow"]

    assert flow == [{"income": "12.34", "expense": "4.99", "count": 2}]


async def test_link_waits_for_the_device_code_and_respects_the_interval(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": portfolio()})
    fakescalable.config(interval=1)
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    assert started["status"] == "PENDING" and started["step"] is None and started["interval"] == 1
    seen = len(fakescalable.log())

    first = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]
    second = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]  # too early: not polled
    polls = [e for e in fakescalable.log()[seen:] if e.get("grant") == "urn:ietf:params:oauth:grant-type:device_code"]
    fakescalable.approve(started["userCode"])
    await asyncio.sleep(1.1)
    done = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]

    assert first["status"] == second["status"] == "PENDING" and first["step"] is None
    assert len(polls) == 1
    linked = await connection_of(aexecute, done)
    assert linked["status"] == "ACTIVE" and len(linked["accounts"]) == 2
    assert done["result"]["label"] == "Scalable Capital"


async def test_link_with_second_factor(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": portfolio()}, mfa=True)
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(started["userCode"])

    waiting = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]
    still = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]
    fakescalable.mfa("SUCCESS")
    done = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]

    assert (waiting["status"], waiting["step"]) == ("PENDING", "MFA")
    assert (still["status"], still["step"]) == ("PENDING", "MFA")
    assert done["status"] == "DONE" and done["step"] is None


async def test_denied_second_factor_fails_the_link(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": portfolio()}, mfa=True)
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(started["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})
    fakescalable.mfa("DENY")

    result = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]

    assert (result["status"], result["errorCode"]) == ("FAILED", "MFA_REJECTED") and result["errorMessage"]
    connection = await models.BankConnection.objects.aget(state=started["state"])
    assert connection.status == models.ConnectionStatus.FAILED and connection.secret is None


async def test_denied_login_code_fails_the_link(aexecute, fakescalable, sc_provider):
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.deny(started["userCode"])

    result = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]

    assert (result["status"], result["errorCode"]) == ("FAILED", "MFA_REJECTED")


async def test_another_organization_cannot_advance_the_link(aexecute, fakescalable, other_org_context, sc_provider):
    fakescalable.seed({"p1": portfolio()})
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(started["userCode"])

    result = await aexecute(SCALABLE_COMPLETE, {"state": started["state"]}, context=other_org_context, allow_errors=True)

    assert result.errors[0].extensions["code"] == "INVALID_STATE"
    assert (await models.BankConnection.objects.aget(state=started["state"])).status == models.ConnectionStatus.PENDING


async def test_short_lived_tokens_are_refreshed_and_rotated(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    fakescalable.config(token_ttl=30)  # under the refresh margin: every sync must refresh first
    cash_id = by_kind((await scalable_link())["accounts"])["CASH"]["id"]

    await aexecute(SYNC_ACCOUNT, {"id": cash_id})
    await aexecute(SYNC_ACCOUNT, {"id": cash_id})

    assert fakescalable.families() == [{"revoked": False, "refreshes": 2}]


async def test_revoked_login_expires_the_connection_and_relink_keeps_history(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    fakescalable.config(token_ttl=30)
    first = await scalable_link()
    cash_id = by_kind(first["accounts"])["CASH"]["id"]
    await aexecute(SYNC_ACCOUNT, {"id": cash_id})

    fakescalable.revoke()
    failed = await aexecute(SYNC_ACCOUNT, {"id": cash_id}, allow_errors=True)
    connection = await models.BankConnection.objects.aget(id=first["id"])
    second = await scalable_link()

    assert failed.errors[0].extensions["code"] == "CONSENT_EXPIRED"
    assert connection.status == models.ConnectionStatus.EXPIRED
    assert by_kind(second["accounts"])["CASH"]["id"] == cash_id
    assert len((await aexecute(TXS, {"acc": cash_id})).data["transactions"]) == 3


async def test_revoking_logs_out_at_scalable_and_forgets_credentials(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    connection = await scalable_link()

    revoked = (await aexecute('mutation($id: ID!) { revokeBankConnection(id: $id) { status } }', {"id": connection["id"]})).data["revokeBankConnection"]

    assert revoked["status"] == "REVOKED"
    assert fakescalable.families() == [{"revoked": True, "refreshes": 0}]
    assert (await models.BankConnection.objects.aget(id=connection["id"])).secret is None


async def test_only_allowed_operations_ever_reach_scalable(aexecute, fakescalable, sc_provider):
    fakescalable.seed({"p1": portfolio()}, savings={"s1": {"total": 1}}, mfa=True)
    fakescalable.mfa("SUCCESS")
    seen = len(fakescalable.log())
    started = (await aexecute(SCALABLE_START, {"provider": sc_provider})).data["startLink"]
    fakescalable.approve(started["userCode"])
    await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})  # starts the second factor
    done = (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]})).data["completeAuth"]
    await aexecute(SYNC_CONNECTION, {"id": done["result"]["id"]})

    sent = {e["operation"] for e in fakescalable.log()[seen:] if "operation" in e}
    assert {"Start2faOnLogin", "BrokerHoldings", "OvernightTransactions"} <= sent
    assert sent <= set(OPERATIONS)
    with pytest.raises(ValueError):
        async with ScalableClient() as client:
            await client.graphql(DpopKey.generate(), "token", "BrokerBuyOrder", {})


async def test_graphql_nonce_challenge_is_answered(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    cash_id = by_kind((await scalable_link())["accounts"])["CASH"]["id"]
    fakescalable.config(graphql_nonce_challenges=1)
    seen = len(fakescalable.log())

    synced = (await aexecute(SYNC_ACCOUNT, {"id": cash_id})).data["syncAccount"]

    assert synced["created"] == 3
    assert [e["rejected"] for e in fakescalable.log()[seen:] if "rejected" in e] == ["use_dpop_nonce"]


async def test_a_rejected_access_token_is_refreshed_not_relinked(scalable_link, aexecute, fakescalable):
    fakescalable.seed({"p1": portfolio()})
    connection = await scalable_link()
    cash_id = by_kind(connection["accounts"])["CASH"]["id"]
    fakescalable.config(graphql_rejections=1)
    seen = len(fakescalable.log())

    synced = (await aexecute(SYNC_ACCOUNT, {"id": cash_id})).data["syncAccount"]

    assert synced["created"] == 3
    assert (await models.BankConnection.objects.aget(id=connection["id"])).status == models.ConnectionStatus.ACTIVE
    assert fakescalable.families() == [{"revoked": False, "refreshes": 1}]
    assert [e["rejected"] for e in fakescalable.log()[seen:] if "rejected" in e] == ["token"]


def test_concurrent_refreshes_rotate_the_token_once(transactional_db, fakescalable, scalable_link):
    """Two replicas find the token expiring at once: one refreshes, the other waits and reuses it.

    Real threads with their own event loops and DB connections; the first refresh is parked in
    fakescalable (hold) so the second contender provably arrives while the first holds the lease.
    """
    fakescalable.seed({"p1": portfolio()})
    fakescalable.config(token_ttl=30)
    connection_id = int(asyncio.run(scalable_link())["id"])
    fakescalable.config(token_ttl=1200)  # the linked token is stale; the refreshed one is not
    fakescalable.hold()
    outcomes: list = []

    def contender() -> None:
        from django.db import connection

        async def run() -> str:
            async with ScalableClient() as client:
                return (await session_for(connection_id, client)).access_token

        try:
            outcomes.append(asyncio.run(run()))
        except BaseException as error:  # noqa: BLE001 - reported to the test
            outcomes.append(error)
        finally:
            connection.close()

    first = threading.Thread(target=contender)
    first.start()
    deadline = time.monotonic() + 10
    while fakescalable.held() == 0:
        assert time.monotonic() < deadline, "the first refresh never reached fakescalable"
        time.sleep(0.02)
    second = threading.Thread(target=contender)
    second.start()
    time.sleep(0.3)  # the second contender is now spinning on the lease
    fakescalable.release()
    first.join(20)
    second.join(20)

    assert len(outcomes) == 2 and all(isinstance(o, str) for o in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    assert fakescalable.families() == [{"revoked": False, "refreshes": 1}]
