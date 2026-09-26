"""The scheduled sync: ``sync_all_accounts``, run by the hub's rekuest through ``rekuest_service``.

Against fakebank over real HTTP, like every sync test. The hook tests post a signed Assign the
way rekuest does; the run's reports go to an unreachable intake (there is no rekuest here),
which the endpoint logs and swallows — the account's own state is what proves the run.
"""

import asyncio
import json
import time

import pytest
from asgiref.sync import sync_to_async
from django.test import Client as HttpClient
from django.utils import timezone

from finance import models
from finance.scheduled import sync_all_accounts
from rekuest_service import registered, signing
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

SECRET = "bank-hook-secret"


@pytest.fixture
def hooked(settings):
    settings.REKUEST_HOOK = {"SECRET": SECRET, "REKUEST_URL": "http://127.0.0.1:9", "MAX_SKEW": 300}
    return settings


def _signed(body: bytes, agent: str = "17") -> dict:
    return {signing.AGENT_HEADER: agent, signing.SIGNATURE_V1_HEADER: signing.sign(SECRET, agent, body)}


def test_the_action_declares_its_default_schedule():
    declared = registered()["sync_all_accounts"]
    assert declared.default_interval == 43200
    assert declared.manifest()["default_interval"] == 43200


async def test_a_scheduled_pass_syncs_every_active_account(link, fakebank):
    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop")]), account(iban="AT2")])
    await link()

    result = await sync_all_accounts()

    assert result == {"synced": 2, "skipped": 0, "failed": 0}
    assert await models.Transaction.objects.acount() == 1
    assert await models.AccountSyncer.objects.filter(last_synced_at__isnull=False).acount() == 2


async def test_the_reserve_is_left_for_users(link, fakebank):
    fakebank.scenario([account(iban="AT1")])
    acc_id = int((await link())["accounts"][0]["id"])
    # 4 a day (the test limit); 3 spent → 1 left, which is the reserve.
    await models.AccountSyncer.objects.filter(account_id=acc_id).aupdate(syncs_today=3, sync_day=timezone.now().date())

    assert await sync_all_accounts() == {"synced": 0, "skipped": 0, "failed": 0}
    assert (await models.AccountSyncer.objects.aget(account_id=acc_id)).syncs_today == 3  # nothing spent


async def test_a_signed_assign_runs_the_sync(hooked, link, fakebank):
    fakebank.scenario([account(iban="AT1")])
    acc_id = int((await link())["accounts"][0]["id"])
    body = json.dumps({"type": "ASSIGN", "task": "501", "interface": "sync_all_accounts", "args": {}}).encode()

    response = await sync_to_async(HttpClient().post)("/_rekuest/hook", data=body, content_type="application/json", headers=_signed(body))
    assert response.status_code == 202

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (await models.AccountSyncer.objects.aget(account_id=acc_id)).last_synced_at is not None:
            break
        await asyncio.sleep(0.1)
    assert (await models.AccountSyncer.objects.aget(account_id=acc_id)).last_synced_at is not None


def test_the_manifest_is_signed_and_lists_the_action(hooked):
    client = HttpClient()
    assert client.get("/_rekuest/hook/manifest").status_code == 401
    response = client.get("/_rekuest/hook/manifest", headers=_signed(b""))
    assert response.status_code == 200
    assert "sync_all_accounts" in [a["interface"] for a in response.json()["actions"]]


def test_unsigned_forged_and_unconfigured_requests_are_refused(hooked, settings):
    client = HttpClient()
    body = b'{"type": "ASSIGN", "task": "1", "interface": "sync_all_accounts", "args": {}}'
    assert client.post("/_rekuest/hook", data=body, content_type="application/json").status_code == 401
    forged = {signing.AGENT_HEADER: "17", signing.SIGNATURE_V1_HEADER: signing.sign("wrong", "17", body)}
    assert client.post("/_rekuest/hook", data=body, content_type="application/json", headers=forged).status_code == 401

    settings.REKUEST_HOOK = None
    assert client.post("/_rekuest/hook", data=body, content_type="application/json", headers=_signed(body)).status_code == 503
