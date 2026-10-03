"""The scheduled sync: ``sync_all_accounts``, an action of bank's HookAgent, run by the hub's rekuest for one organization.

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
from joserfc.jwk import OKPKey

from bank_server.hook_agent import agent
from bank_server.service import service
from rekuest_service import trust
from tests.conftest import account, tx

pytestmark = pytest.mark.django_db(transaction=True)

BANK_KEY = OKPKey.generate_key("Ed25519")
REKUEST_KEY = OKPKey.generate_key("Ed25519")


@pytest.fixture
def hooked(settings):
    settings.REKUEST_HOOK = {"REKUEST_URL": "http://127.0.0.1:9"}
    settings.INSTANCE = {
        "PRIVATE_KEY": BANK_KEY.as_pem(private=True).decode(),
        "TRUST_JWKS": {
            "keys": [
                {**trust.public_jwk(BANK_KEY), "service": "live.arkitekt.bank"},
                {**trust.public_jwk(REKUEST_KEY), "service": "live.arkitekt.rekuest"},
            ]
        },
    }
    return settings


def _signed(body: bytes, agent: str = "17", *, method: str = "POST", path: str = "/_rekuest/hook", key=REKUEST_KEY) -> dict:
    """Headers of a request rekuest sends: a service JWT from rekuest's key, for bank."""
    authorization = trust.sign(method, path, body, issuer="live.arkitekt.rekuest", audience="live.arkitekt.bank", key=key)
    return {"Authorization": authorization, "X-Rekuest-Agent": agent}


def test_the_action_is_offered_and_wired_to_nothing():
    assert agent.actions["sync_all_accounts"].manifest() == {
        "interface": "sync_all_accounts",
        "name": "Sync all bank accounts",
        "description": "Sync every active bank account of the organization that has sync budget to spare, unattended.",
    }


def test_the_service_and_the_agent_are_separate_declarations():
    assert "actions" not in service.manifest() and not hasattr(service, "action")
    assert [a["interface"] for a in agent.manifest()["actions"]] == ["sync_all_accounts", "reembed_stale"]


async def test_a_scheduled_pass_syncs_every_active_account(link, fakebank):
    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop")]), account(iban="AT2")])
    await link()

    result = await sync_all_accounts(organization="static_org")

    assert result == {"synced": 2, "skipped": 0, "failed": 0}
    assert await models.Transaction.objects.acount() == 1
    assert await models.AccountSyncer.objects.filter(last_synced_at__isnull=False).acount() == 2


async def test_a_pass_syncs_only_the_organization_it_runs_for(link, fakebank, other_org_context):
    """Every organization has the agent and its own schedule: a run must not do another's work."""
    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop")])])
    await link()
    await link(other_org_context)

    assert await sync_all_accounts(organization="other_org") == {"synced": 1, "skipped": 0, "failed": 0}

    synced = models.AccountSyncer.objects.filter(last_synced_at__isnull=False)
    assert [slug async for slug in synced.values_list("organization__slug", flat=True)] == ["other_org"]
    assert await models.Transaction.objects.filter(account__organization__slug="static_org").acount() == 0
    assert await models.Transaction.objects.filter(account__organization__slug="other_org").acount() == 1
    # An organization without bank links has nothing to do.
    assert await sync_all_accounts(organization="nobody") == {"synced": 0, "skipped": 0, "failed": 0}


async def test_the_reserve_is_left_for_users(link, fakebank):
    fakebank.scenario([account(iban="AT1")])
    acc_id = int((await link())["accounts"][0]["id"])
    # 4 a day (the test limit); 3 spent → 1 left, which is the reserve.
    await models.AccountSyncer.objects.filter(account_id=acc_id).aupdate(syncs_today=3, sync_day=timezone.now().date())

    assert await sync_all_accounts(organization="static_org") == {"synced": 0, "skipped": 0, "failed": 0}
    assert (await models.AccountSyncer.objects.aget(account_id=acc_id)).syncs_today == 3  # nothing spent


async def test_a_signed_assign_runs_the_sync(hooked, link, fakebank):
    fakebank.scenario([account(iban="AT1")])
    acc_id = int((await link())["accounts"][0]["id"])
    body = json.dumps({"type": "ASSIGN", "task": "501", "interface": "sync_all_accounts", "org": "static_org", "args": {}}).encode()

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
    response = client.get("/_rekuest/hook/manifest", headers=_signed(b"", method="GET", path="/_rekuest/hook/manifest"))
    assert response.status_code == 200
    assert "sync_all_accounts" in [a["interface"] for a in response.json()["actions"]]


def test_unsigned_forged_and_unconfigured_requests_are_refused(hooked, settings):
    client = HttpClient()
    body = b'{"type": "ASSIGN", "task": "1", "interface": "sync_all_accounts", "args": {}}'
    assert client.post("/_rekuest/hook", data=body, content_type="application/json").status_code == 401
    forged = _signed(body, key=BANK_KEY)  # bank's own key, posing as rekuest
    assert client.post("/_rekuest/hook", data=body, content_type="application/json", headers=forged).status_code == 401

    settings.REKUEST_HOOK = None
    assert client.post("/_rekuest/hook", data=body, content_type="application/json", headers=_signed(body)).status_code == 503
