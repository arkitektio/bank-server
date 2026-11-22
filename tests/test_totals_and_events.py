"""Counts and totals past any page, positions merged across depots, and live events.

End to end against fakebank (REDIRECT) and fakescalable (POLL).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from authentikate.models import Organization
from django.utils import timezone

from finance import models
from finance.channels import auth_group, org_group
from tests.conftest import COMPLETE, LINK, account, cash, holding, trade, tx

pytestmark = pytest.mark.django_db(transaction=True)

SYNC = "mutation($id: ID!) { syncAccount(id: $id) { created } }"
SESSION = "state status errorCode"
AUTH = "subscription($s: String!) { authSession(state: $s) { %s } }" % SESSION
SYNCS = "subscription($a: ID) { accountSyncs(account: $a) { accountId created } }"


async def test_candidate_count_covers_every_candidate_past_the_limit(
    link, aexecute, fakebank
):
    rows = [
        tx("-12.00", "2026-09-01", "Spar Dankt 3418"),
        tx("-30.00", "2026-09-03", "Spar Dankt 3428"),
        tx("-6.50", "2026-09-07", "MCDONALDS 01476"),
        tx("-7.50", "2026-09-08", "MCDONALDS 01476"),
        tx("-9.00", "2026-09-09", "MCDONALDS 01476"),
        tx("-500.00", "2026-09-06", "Sparkasse Rate"),
    ]
    fakebank.scenario([account(transactions=rows)])
    acc = (await link())["accounts"][0]["id"]
    await aexecute(SYNC, {"id": acc})

    page = (await aexecute("{ merchantCandidates(limit: 1) { key } }")).data[
        "merchantCandidates"
    ]
    every = (await aexecute("{ merchantCandidates(limit: 100) { key } }")).data[
        "merchantCandidates"
    ]
    assert len(page) == 1 and len(every) == 2
    assert (await aexecute("{ merchantCandidatesCount }")).data[
        "merchantCandidatesCount"
    ] == 2
    assert (await aexecute("{ merchantCandidatesCount(minCount: 3) }")).data[
        "merchantCandidatesCount"
    ] == 1


async def test_needs_reauth_filters_like_the_field(link, aexecute, fakebank):
    fakebank.scenario([account()])
    connection = (await link())["id"]
    query = "query($v: Boolean!) { bankConnections(filters: {needsReauth: $v}) { id needsReauth } }"

    async def listed(value: bool) -> list[dict]:
        return (await aexecute(query, {"v": value})).data["bankConnections"]

    await models.BankConnection.objects.filter(id=connection).aupdate(
        status=models.ConnectionStatus.ACTIVE,
        valid_until=timezone.now() + timedelta(days=30),
    )
    assert await listed(True) == [] and await listed(False) == [
        {"id": connection, "needsReauth": False}
    ]
    await models.BankConnection.objects.filter(id=connection).aupdate(
        valid_until=timezone.now() - timedelta(minutes=1)
    )
    assert (
        await listed(True) == [{"id": connection, "needsReauth": True}]
        and await listed(False) == []
    )
    await models.BankConnection.objects.filter(id=connection).aupdate(
        status=models.ConnectionStatus.EXPIRED, valid_until=None
    )
    assert await listed(True) == [{"id": connection, "needsReauth": True}]


async def test_balance_totals_sum_each_accounts_latest_balance(
    link, aexecute, fakebank, other_org_context
):
    fakebank.scenario(
        [
            account(balance="100.00"),
            account(balance="50.50"),
            account(currency="USD", balance="7.00"),
        ]
    )
    accounts = (await link())["accounts"]
    for acc in accounts:
        await aexecute(SYNC, {"id": acc["id"]})
    eur = next(a for a in accounts if a["currency"] == "EUR")
    newest = await models.BalanceSnapshot.objects.filter(account_id=eur["id"]).alatest(
        "date"
    )
    # Neither an older balance nor a less authoritative one of the same day counts.
    await models.BalanceSnapshot.objects.acreate(
        account_id=eur["id"],
        date=newest.date - timedelta(days=3),
        balance_type="CLBD",
        amount=Decimal("999.00"),
        currency="EUR",
    )
    await models.BalanceSnapshot.objects.acreate(
        account_id=eur["id"],
        date=newest.date,
        balance_type="OTHR",
        amount=Decimal("888.00"),
        currency="EUR",
    )

    totals = (
        await aexecute("{ balanceTotals { kind currency amount accounts } }")
    ).data["balanceTotals"]
    latest = (
        await aexecute("{ bankAccounts { currency latestBalance { amount } } }")
    ).data["bankAccounts"]
    assert totals == [
        {"kind": "CASH", "currency": "EUR", "amount": "150.50", "accounts": 2},
        {"kind": "CASH", "currency": "USD", "amount": "7.00", "accounts": 1},
    ]
    assert sum(
        Decimal(a["latestBalance"]["amount"]) for a in latest if a["currency"] == "EUR"
    ) == Decimal("150.50")

    only = (
        await aexecute(
            "query($ids: [ID!]!) { balanceTotals(filters: {ids: $ids}) { amount accounts } }",
            {"ids": [eur["id"]]},
        )
    ).data["balanceTotals"]
    assert only == [{"amount": newest.amount.to_eng_string(), "accounts": 1}]
    assert (
        await aexecute("{ balanceTotals { amount } }", context=other_org_context)
    ).data["balanceTotals"] == []


async def test_positions_merge_by_isin_across_depots(
    scalable_link, aexecute, fakescalable
):
    fakescalable.seed(
        {
            "p1": {
                "cash": 100,
                "holdings": [
                    holding("IE00TEST0001", 10, 100.0, 80.0),
                    holding("US00TEST0002", 5, 20.0, 25.0, kind="STOCK"),
                ],
                "transactions": [
                    trade(
                        -800, "2026-09-10T10:00:00.000Z", "IE00TEST0001", quantity=10
                    ),
                    cash(1000, "2026-09-02T00:00:00.000Z", "DEPOSIT"),
                ],
            }
        }
    )
    connection = await scalable_link()
    await aexecute(
        "mutation($id: ID!) { syncConnection(id: $id) { created } }",
        {"id": connection["id"]},
    )
    depot = await models.BankAccount.objects.aget(
        organization__identifier="static_issuer#static_org",
        kind=models.AccountKind.DEPOT,
    )
    day = (
        await models.HoldingSnapshot.objects.filter(account=depot).alatest("date")
    ).date
    # A second depot (another broker) holding the same ETF, and a transferred-in stock without a buy-in.
    second = await models.BankAccount.objects.acreate(
        organization_id=depot.organization_id,
        name="Other broker",
        currency="EUR",
        kind=models.AccountKind.DEPOT,
        import_key="test:second-depot",
    )
    await models.HoldingSnapshot.objects.acreate(
        account=second,
        date=day,
        isin="IE00TEST0001",
        name="Some ETF",
        security_type="ETF",
        quantity=Decimal(5),
        fifo_price=Decimal(90),
        price=Decimal(100),
        valuation=Decimal("500.00"),
        currency="EUR",
    )
    await models.HoldingSnapshot.objects.acreate(
        account=second,
        date=day,
        isin="DE000NOFIFO1",
        name="Gift",
        security_type="STOCK",
        quantity=Decimal(2),
        fifo_price=None,
        price=Decimal(100),
        valuation=Decimal("200.00"),
        currency="EUR",
    )

    got = (
        await aexecute(
            "{ portfolioInsights { positions { isin } mergedPositions { isin quantity valuation costBasis fifoPrice unrealizedGain weight depots } } }"
        )
    ).data["portfolioInsights"]

    assert len(got["positions"]) == 4  # one per depot and ISIN
    assert got["mergedPositions"] == [
        {
            "isin": "IE00TEST0001",
            "quantity": "15.00000000",
            "valuation": "1500.00",
            "costBasis": "1250.00",
            "fifoPrice": "83.333333",
            "unrealizedGain": "250.00",
            "weight": 0.8333,
            "depots": 2,
        },
        {
            "isin": "DE000NOFIFO1",
            "quantity": "2.00000000",
            "valuation": "200.00",
            "costBasis": None,
            "fifoPrice": None,
            "unrealizedGain": None,
            "weight": 0.1111,
            "depots": 1,
        },
        {
            "isin": "US00TEST0002",
            "quantity": "5.00000000",
            "valuation": "100.00",
            "costBasis": "125.00",
            "fifoPrice": "25.000000",
            "unrealizedGain": "-25.00",
            "weight": 0.0556,
            "depots": 1,
        },
    ]


async def test_one_depot_on_a_past_day_is_merged_exactly(
    scalable_link, aexecute, fakescalable
):
    fakescalable.seed(
        {
            "p1": {
                "cash": 100,
                "holdings": [holding("IE00TEST0001", 10, 100.0, 80.0)],
                "transactions": [],
            }
        }
    )
    connection = await scalable_link()
    await aexecute(
        "mutation($id: ID!) { syncConnection(id: $id) { created } }",
        {"id": connection["id"]},
    )
    depot = await models.BankAccount.objects.aget(
        organization__identifier="static_issuer#static_org",
        kind=models.AccountKind.DEPOT,
    )
    today = (
        await models.HoldingSnapshot.objects.filter(account=depot).alatest("date")
    ).date
    past = today - timedelta(days=10)
    # Ten days earlier it held two securities (cents that drift as floats, not as decimals).
    for isin, valuation in (("IE00TEST0001", "0.10"), ("US00TEST0002", "0.20")):
        await models.HoldingSnapshot.objects.acreate(
            account=depot,
            date=past,
            isin=isin,
            name=isin,
            security_type="ETF",
            quantity=Decimal(1),
            fifo_price=Decimal("0.05"),
            price=Decimal(valuation),
            valuation=Decimal(valuation),
            currency="EUR",
        )

    query = "query($a: [ID!], $d: Date) { portfolioInsights(accounts: $a, asOf: $d) { mergedPositions { isin quantity valuation costBasis depots } valuation { currency amount } } }"
    # A day between the two snapshots answers with the older one; today's with today's.
    then = (
        await aexecute(
            query, {"a": [str(depot.id)], "d": (past + timedelta(days=2)).isoformat()}
        )
    ).data["portfolioInsights"]
    assert [
        (p["isin"], p["valuation"], p["costBasis"]) for p in then["mergedPositions"]
    ] == [("US00TEST0002", "0.20", "0.05"), ("IE00TEST0001", "0.10", "0.05")]
    assert then["valuation"] == [{"currency": "EUR", "amount": "0.30"}]
    now = (await aexecute(query, {"a": [str(depot.id)], "d": today.isoformat()})).data[
        "portfolioInsights"
    ]
    assert now["mergedPositions"][0]["valuation"] == "1000.00"
    # Before the depot's first synced day there is nothing to show.
    assert (
        await aexecute(
            query, {"a": [str(depot.id)], "d": (past - timedelta(days=1)).isoformat()}
        )
    ).data["portfolioInsights"]["mergedPositions"] == []


async def test_account_syncs_can_follow_one_account(
    link, aexecute, fakebank, subscribe
):
    fakebank.scenario(
        [
            account(transactions=[tx("-1.00", "2026-09-01", "A")]),
            account(transactions=[tx("-2.00", "2026-09-01", "B")]),
        ]
    )
    first, second = (await link())["accounts"]
    group = org_group(
        (await Organization.objects.aget(identifier="static_issuer#static_org")).id
    )
    one = await subscribe(SYNCS, {"a": first["id"]}, group=group)
    every = await subscribe(SYNCS, group=group)

    await aexecute(SYNC, {"id": second["id"]})
    await aexecute(SYNC, {"id": first["id"]})

    assert (await every.next())["accountSyncs"] == {
        "accountId": second["id"],
        "created": 1,
    }
    assert (await every.next())["accountSyncs"] == {
        "accountId": first["id"],
        "created": 1,
    }
    assert (await one.next())["accountSyncs"] == {
        "accountId": first["id"],
        "created": 1,
    }
    assert await one.silent()


async def test_a_redirect_login_is_watched_until_done(
    aexecute, fakebank, eb_provider, subscribe
):
    fakebank.scenario([account()])
    session = (
        await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp})
    ).data["startLink"]
    watcher = await subscribe(
        AUTH, {"s": session["state"]}, group=auth_group(session["state"])
    )
    assert (await watcher.next())["authSession"] == {
        "state": session["state"],
        "status": "PENDING",
        "errorCode": None,
    }

    await aexecute(
        COMPLETE,
        {"code": fakebank.approve(session["state"]), "state": session["state"]},
    )
    assert (await watcher.next())["authSession"] == {
        "state": session["state"],
        "status": "DONE",
        "errorCode": None,
    }
    await watcher.task  # settled: the stream ends


async def test_a_cancelled_login_is_announced(
    aexecute, fakebank, eb_provider, subscribe
):
    fakebank.scenario([account()])
    session = (
        await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp})
    ).data["startLink"]
    watcher = await subscribe(
        AUTH, {"s": session["state"]}, group=auth_group(session["state"])
    )
    await watcher.next()
    await aexecute(
        "mutation($s: String!) { cancelAuth(state: $s) { status } }",
        {"s": session["state"]},
    )
    assert (await watcher.next())["authSession"]["status"] == "CANCELLED"


async def test_an_expiring_login_reads_expired_without_an_announcement(
    aexecute, fakebank, eb_provider, subscribe
):
    fakebank.scenario([account()])
    session = (
        await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp})
    ).data["startLink"]
    await models.BankConnection.objects.filter(state=session["state"]).aupdate(
        pending_expires_at=timezone.now() + timedelta(seconds=1)
    )
    watcher = await subscribe(AUTH, {"s": session["state"]})
    assert (await watcher.next())["authSession"]["status"] == "PENDING"
    assert (await watcher.next())["authSession"] == {
        "state": session["state"],
        "status": "EXPIRED",
        "errorCode": "CODE_EXPIRED",
    }


async def test_only_the_starter_watches_a_login(
    aexecute, fakebank, eb_provider, subscribe
):
    fakebank.scenario([account()])
    session = (
        await aexecute(LINK, {"provider": eb_provider, "aspsp": fakebank.aspsp})
    ).data["startLink"]
    for token in ("colleague", "othertest"):
        stranger = await subscribe(AUTH, {"s": session["state"]}, token=token)
        assert await stranger.error()
