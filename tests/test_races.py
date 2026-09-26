"""Concurrent syncs of one account, as N backend replicas would run them.

Real threads, each with its own event loop and DB connection — ``asyncio.gather`` in one loop
would share a connection and prove nothing. The interleaving is forced with fakebank's hold
(the first sync is parked mid-fetch while holding the lease), not with sleeps.
"""

import asyncio
import threading
import time

import pytest
from channels.db import database_sync_to_async

from finance import models
from finance.sync import AlreadySyncing, claim, release, sync_account
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)


def _in_thread(fn):  # noqa: ANN001, ANN202
    """Run ``fn`` in a fresh thread; returns (thread, outcome dict)."""
    outcome: dict = {}

    def run() -> None:
        from django.db import connection

        try:
            outcome["value"] = fn()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            outcome["error"] = error
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


async def _linked_account(link, fakebank, rows) -> int:  # noqa: ANN001
    fakebank.scenario([account(transactions=rows)])
    return int((await link())["accounts"][0]["id"])


def _wait_until(predicate, timeout: float = 10) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


async def test_claim_has_exactly_one_winner(link, fakebank):
    account_id = await _linked_account(link, fakebank, [])
    syncer_id = (await models.AccountSyncer.objects.aget(account_id=account_id)).id
    barrier = threading.Barrier(8)

    def contender() -> bool:
        barrier.wait()
        return claim(syncer_id, 600)

    runs = [_in_thread(contender) for _ in range(8)]
    for thread, _ in runs:
        thread.join()
    assert sorted(outcome["value"] for _, outcome in runs) == [False] * 7 + [True]
    await database_sync_to_async(release)(syncer_id)


async def test_second_sync_is_refused_while_first_holds_the_account(link, fakebank):
    account_id = await _linked_account(link, fakebank, [tx(f"-{i}.00", "2026-09-01", f"S{i}") for i in range(1, 5)])
    fakebank.hold()

    first, first_outcome = _in_thread(lambda: asyncio.run(sync_account(account_id)))
    _wait_until(lambda: fakebank.held() == 1)  # the first sync holds the lease, parked mid-fetch

    second, second_outcome = _in_thread(lambda: asyncio.run(sync_account(account_id)))
    second.join()
    fakebank.release()
    first.join()

    assert isinstance(second_outcome.get("error"), AlreadySyncing)
    assert first_outcome["value"].created == 4
    assert await models.Transaction.objects.filter(account_id=account_id).acount() == 4
    assert (await models.AccountSyncer.objects.aget(account_id=account_id)).sync_lease_until is None


async def test_expired_lease_is_taken_over(link, fakebank):
    """A sync that crashed without releasing blocks the account only until its lease runs out."""
    from datetime import timedelta

    from django.utils import timezone

    account_id = await _linked_account(link, fakebank, [])
    await models.AccountSyncer.objects.filter(account_id=account_id).aupdate(sync_lease_until=timezone.now() - timedelta(seconds=1))
    await sync_account(account_id)
