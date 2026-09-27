"""bank's model signals: what it declares to the hub's rekuest, and that a save reaches it signed.

The save goes to a real local HTTP server standing in for rekuest's signal intake, and is
checked the way rekuest checks it (the instance-key JWT, the body).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from joserfc.jwk import OKPKey

from rekuest_service import trust
from bank_server.service import service

EXPECTED = {
    "@bank/bankconnection": [
        "CREATED",
        "UPDATED"
    ],
    "@bank/bankaccount": [
        "CREATED",
        "UPDATED"
    ],
    "@bank/statementimport": [
        "CREATED",
        "UPDATED"
    ],
    "@bank/recurringpayment": [
        "CREATED",
        "UPDATED"
    ],
    "@bank/category": [
        "CREATED",
        "UPDATED",
        "DELETED"
    ],
    "@bank/merchant": [
        "CREATED",
        "UPDATED",
        "DELETED"
    ],
    "@bank/budget": [
        "CREATED",
        "UPDATED",
        "DELETED"
    ],
    "@bank/transaction": [
        "UPDATED"
    ]
}

KEY = OKPKey.generate_key("Ed25519")


class _Intake:
    def __init__(self) -> None:
        self.received: list[dict] = []
        intake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                intake.received.append({"path": self.path, "headers": dict(self.headers), "body": body, "json": json.loads(body)})
                self.send_response(202)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def of(self, identifier: str, count: int = 1, timeout: float = 10) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [r for r in self.received if r["json"]["identifier"] == identifier]
            if len(found) >= count:
                return found
            time.sleep(0.05)
        return [r for r in self.received if r["json"]["identifier"] == identifier]


@pytest.fixture
def intake(settings):
    server = _Intake()
    settings.REKUEST_HOOK = {"REKUEST_URL": server.url, "SERVICE": "bank"}
    settings.INSTANCE = {
        "PRIVATE_KEY": KEY.as_pem(private=True).decode(),
        "TRUST_JWKS": {"keys": [{**trust.public_jwk(KEY), "service": "live.arkitekt.bank"}]},
    }
    yield server
    server.server.shutdown()


def _organization():
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug="signals-test-org")[0]


def test_the_manifest_declares_every_model_signal():
    assert {s["identifier"]: s["kinds"] for s in service.manifest()["signals"]} == EXPECTED


@pytest.mark.django_db(transaction=True)
def test_a_save_is_signalled_signed_by_this_instance(intake):
    from finance.models import Category

    org = _organization()
    obj = Category.objects.create(name='signalled', organization=org)

    (received,) = intake.of("@bank/category")
    assert (received["json"]["kind"], received["json"]["object"], received["json"]["organization"]) == ("CREATED", str(obj.pk), org.slug)
    assert received["path"] == "/agi/signal/bank"
    verified = trust.verify("POST", received["path"], received["body"], received["headers"]["Authorization"], audience="live.arkitekt.rekuest")
    assert verified.issuer == "live.arkitekt.bank"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_sync_is_one_account_signal_with_its_counts(intake, link, fakebank):
    """Transactions are bulk-created, so no model signal fires per row: the sync says how many."""
    from finance.scheduled import sync_all_accounts
    from tests.conftest import account, tx

    fakebank.scenario([account(iban="AT1", transactions=[tx("-1.00", "2026-09-01", "Shop"), tx("-2.00", "2026-09-02", "Cafe")])])
    await link()
    await sync_all_accounts()

    def synced():
        return [r for r in intake.of("@bank/bankaccount", count=1) if "@bank/new_transactions" in r["json"]["descriptors"]]

    deadline = time.monotonic() + 10
    while not synced() and time.monotonic() < deadline:
        time.sleep(0.05)
    (received,) = synced()
    assert received["json"]["kind"] == "UPDATED"
    assert received["json"]["descriptors"]["@bank/new_transactions"] == 2
