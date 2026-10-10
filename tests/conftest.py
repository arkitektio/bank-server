"""Shared pytest fixtures for the bank service.

The suite runs against a real stack brought up by dokker (``tests/integration``): postgres,
and ``fakebank`` — a stand-in for the Enable Banking API that speaks real HTTP and verifies
the RS256 application JWT. Nothing in the service is mocked.

* ``backend_stack`` — brings the stack up once per session; yields its ephemeral ports.
* ``providers_endpoints`` / ``application`` — points the provider kinds at the fakes, generates the
  run's Fernet key and an Enable Banking application key pair registered with fakebank.
* ``provider_for`` / ``eb_provider`` / ``sc_provider`` / ``make_provider`` — an organization's
  provider rows, which every link goes through.
* ``authenticated_context`` / ``other_org_context`` — two tenants (static tokens ``test`` and
  ``othertest``), for scoping tests.
* ``fakebank`` — a small client for fakebank's ``/_admin`` endpoints.
* ``scalable`` / ``fakescalable`` / ``scalable_link`` — the same for ``fakescalable``, a stand-in
  for Scalable Capital's OAuth issuer and CLI GraphQL API (DPoP, device login, rotating tokens).
* ``datalayer`` / ``upload`` — points the vendored datalayer at the stack's RustFS and uploads a
  file the way a client does (scoped STS grant, S3 PUT, finish).
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from authentikate.models import Client, Membership, Organization, User
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dokker import testing
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from bank_server.schema import schema


@dataclass
class Stack:
    """Where the test stack's services landed on this host."""

    db_port: int
    fakebank_url: str
    fakescalable_url: str
    rustfs_port: int
    fakegeo_url: str = ""
    fakemarket_url: str = ""


def _wait(check, what: str, timeout: float = 60) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while True:
        try:
            check()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{what} did not come up in {timeout}s")
            time.sleep(0.2)


@pytest.fixture(scope="session")
def backend_stack():
    """Bring up postgres + fakebank and yield the host ports docker gave them.

    No host port is pinned in the compose file: dokker mints a unique project per run and
    docker picks free ports, so two suites (or a stack stranded by a crashed run) never
    collide. Ports are resolved inside the wait loop because ``up()`` may return before the
    containers publish them.
    """
    compose = Path(__file__).parent / "integration" / "docker-compose.yaml"
    with testing(str(compose)) as e:
        e.up()
        ports: dict[str, int] = {}

        def db_ready() -> None:
            if "db" not in ports:
                ports["db"] = e.get_port("db", 5432)
            with psycopg.connect(dbname="testdb", user="test", password="test", host="localhost", port=ports["db"], connect_timeout=1) as connection:
                connection.execute("SELECT 1")

        def fakebank_ready() -> None:
            if "fakebank" not in ports:
                ports["fakebank"] = e.get_port("fakebank", 8000)
            urllib.request.urlopen(f"http://localhost:{ports['fakebank']}/_admin/health", timeout=1).read()

        def fakescalable_ready() -> None:
            if "fakescalable" not in ports:
                ports["fakescalable"] = e.get_port("fakescalable", 8000)
            urllib.request.urlopen(f"http://localhost:{ports['fakescalable']}/_admin/health", timeout=1).read()

        def rustfs_ready() -> None:
            if "rustfs" not in ports:
                ports["rustfs"] = e.get_port("rustfs", 9000)
            try:
                urllib.request.urlopen(f"http://localhost:{ports['rustfs']}/", timeout=1)
            except urllib.error.HTTPError:
                pass  # any S3 answer (403 to an anonymous request) means it is up

        _wait(db_ready, "postgres")
        _wait(rustfs_ready, "rustfs")
        _wait(fakebank_ready, "fakebank", timeout=180)  # the first run builds its image
        _wait(fakescalable_ready, "fakescalable", timeout=180)

        def fakegeo_ready() -> None:
            if "fakegeo" not in ports:
                ports["fakegeo"] = e.get_port("fakegeo", 8000)
            urllib.request.urlopen(f"http://localhost:{ports['fakegeo']}/_admin/health", timeout=1).read()

        _wait(fakegeo_ready, "fakegeo", timeout=180)

        def fakemarket_ready() -> None:
            if "fakemarket" not in ports:
                ports["fakemarket"] = e.get_port("fakemarket", 8000)
            urllib.request.urlopen(f"http://localhost:{ports['fakemarket']}/_admin/health", timeout=1).read()

        _wait(fakemarket_ready, "fakemarket", timeout=180)
        yield Stack(
            db_port=ports["db"],
            fakebank_url=f"http://localhost:{ports['fakebank']}",
            fakescalable_url=f"http://localhost:{ports['fakescalable']}",
            rustfs_port=ports["rustfs"],
            fakegeo_url=f"http://localhost:{ports['fakegeo']}",
            fakemarket_url=f"http://localhost:{ports['fakemarket']}",
        )


@pytest.fixture(scope="session", autouse=True)
def embedding_model_warm():
    """Load the embedding model once per session, outside any test's DB transaction (as rekuest does)."""
    from embeddings import engine

    engine.warm_up()
    yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(backend_stack):
    """Point Django at the stack's postgres before pytest-django creates the test database."""
    from django.conf import settings

    settings.DATABASES["default"]["PORT"] = str(backend_stack.db_port)
    yield


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Kill the connections of asgiref's executor threads before the test database is dropped.

    ``database_sync_to_async`` runs ORM code in threads whose connections outlive the tests and
    would block dropping the database ("is being accessed by other users").
    """
    yield
    from django.db import connections

    with django_db_blocker.unblock():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        connections.close_all()


REDIRECTS = ["https://bank.test/callback", "https://other.test/callback"]


@dataclass
class Application:
    """An Enable Banking application registered with fakebank for the run: its id and private key (PEM text)."""

    app_id: str
    pem: str


def register_application(fakebank_url: str, app_id: str, redirect_urls: list[str] | None = None) -> Application:
    """Generate a key pair, tell fakebank its public half, and return the application."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    _post(fakebank_url + "/_admin/keys", {"kid": app_id, "public_key": public, "redirect_urls": REDIRECTS if redirect_urls is None else redirect_urls})
    return Application(app_id, pem)


@pytest.fixture(scope="session", autouse=True)
def providers_endpoints(backend_stack, tmp_path_factory):
    """Point the provider kinds at the fakes of the stack, with a Fernet key generated for the run. Nothing is committed."""
    from cryptography.fernet import Fernet
    from django.conf import settings

    key = tmp_path_factory.mktemp("keys") / "bank.fernet"
    key.write_bytes(Fernet.generate_key())
    settings.ENCRYPTION = {"key_path": str(key)}
    settings.ENABLEBANKING = {**settings.ENABLEBANKING, "api_url": backend_stack.fakebank_url}
    url = backend_stack.fakescalable_url
    settings.SCALABLE = {**settings.SCALABLE, "issuer": url, "graphql_url": f"{url}/api/cli/graphql"}
    yield


@pytest.fixture(scope="session")
def application(backend_stack, providers_endpoints) -> Application:
    """The run's Enable Banking application (kid ``test-app``), known to fakebank."""
    return register_application(backend_stack.fakebank_url, "test-app")


def _post(url: str, body: dict, method: str = "POST") -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


class FakeBank:
    """Drives fakebank's /_admin endpoints. Each test gets its own bank and account idents."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.aspsp = f"Fake Bank {uuid.uuid4().hex[:8]}"

    def scenario(self, accounts: list[dict]) -> None:
        """The accounts this test's bank offers (see :func:`account`)."""
        _post(self.url + "/_admin/scenario", {"aspsp_name": self.aspsp, "accounts": accounts})

    def set_account(self, ident: str, transactions: list[dict] | None = None, balances: list[dict] | None = None, rate_limited: bool | None = None) -> None:
        body: dict = {}
        if rate_limited is not None:
            body["rate_limited"] = rate_limited
        if transactions is not None:
            body["transactions"] = transactions
        if balances is not None:
            body["balances"] = balances
        _post(self.url + f"/_admin/accounts/{ident}", body, method="PUT")

    def approve(self, state: str) -> str:
        """The user approves at the bank; returns the code the redirect would carry."""
        return _post(self.url + "/_admin/approve", {"state": state})["code"]

    def expire(self, session_id: str) -> None:
        _post(self.url + f"/_admin/sessions/{session_id}/expire", {})

    def hold(self) -> None:
        _post(self.url + "/_admin/hold", {})

    def release(self) -> None:
        _post(self.url + "/_admin/release", {})

    def held(self) -> int:
        return _get(self.url + "/_admin/held")["held"]

    def log(self) -> list[dict]:
        return _get(self.url + "/_admin/log")["log"]


@pytest.fixture
def fakebank(backend_stack) -> FakeBank:
    bank = FakeBank(backend_stack.fakebank_url)
    yield bank
    bank.release()  # never leave requests parked for the next test


class FakeScalable:
    """Drives fakescalable's /_admin endpoints. Each test gets its own person."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.person = uuid.uuid4().hex

    def seed(self, portfolios: dict | None = None, savings: dict | None = None, mfa: bool = False, person: str | None = None) -> None:
        """This test's person: ``{portfolio_id: {cash, holdings, transactions}}`` and ``{savings_id: {total, transactions}}``."""
        _post(self.url + f"/_admin/people/{person or self.person}", {"portfolios": portfolios or {}, "savings": savings or {}, "mfa": mfa})

    def approve(self, user_code: str, person: str | None = None) -> None:
        _post(self.url + "/_admin/approve", {"user_code": user_code, "person": person or self.person})

    def deny(self, user_code: str) -> None:
        _post(self.url + "/_admin/deny", {"user_code": user_code})

    def mfa(self, status: str) -> None:
        _post(self.url + "/_admin/mfa", {"person": self.person, "status": status})

    def revoke(self) -> None:
        _post(self.url + "/_admin/revoke", {"person": self.person})

    def families(self) -> list[dict]:
        return _get(self.url + f"/_admin/families?person={self.person}")["families"]

    def config(self, **values) -> None:  # noqa: ANN003
        _post(self.url + "/_admin/config", values)

    def hold(self) -> None:
        _post(self.url + "/_admin/hold", {})

    def release(self) -> None:
        _post(self.url + "/_admin/release", {})

    def held(self) -> int:
        return _get(self.url + "/_admin/held")["held"]

    def log(self) -> list[dict]:
        return _get(self.url + "/_admin/log")["log"]

    def prices(self, series: dict[str, list[tuple[str, float]]]) -> None:
        """Market data for BrokerChart / BrokerQuote: ``{isin: [(day, mid price), …]}``."""
        _post(self.url + "/_admin/prices", {isin: [list(p) for p in points] for isin, points in series.items()})


@pytest.fixture
def fakescalable(backend_stack) -> FakeScalable:
    fake = FakeScalable(backend_stack.fakescalable_url)
    defaults = {"token_ttl": 1200, "interval": 5, "graphql_nonce_challenges": 0, "graphql_rejections": 0, "graphql_rate_limited": 0}
    fake.config(**defaults)
    yield fake
    fake.release()
    fake.config(**defaults)


def holding(isin: str, quantity: float, price: float, fifo: float, *, name: str | None = None, kind: str = "ETF") -> dict:
    """A depot position as Scalable's BrokerHoldings returns it."""
    return {
        "isin": isin,
        "name": name or f"Fund {isin}",
        "type": kind,
        "inventory": {"position": {"filled": quantity, "pending": 0, "blocked": 0, "fifoPrice": fifo}},
        "portfolioIsinPerformance": {"valuation": round(quantity * price, 2), "currency": "EUR"},
        "quoteTick": {"midPrice": price, "currency": "EUR", "timestampUtc": {"time": "2026-09-25T21:00:00.000Z"}, "isOutdated": False},
    }


def trade(amount: float, when: str, isin: str, *, side: str = "BUY", quantity: float = 1, status: str = "SETTLED", id: str | None = None, name: str = "Some ETF") -> dict:
    """A BrokerSecurityTransactionSummary; ``amount`` is signed (a buy is negative)."""
    return {
        "__typename": "BrokerSecurityTransactionSummary",
        "id": id or uuid.uuid4().hex[:22],
        "currency": "EUR",
        "type": "SECURITY_TRANSACTION",
        "status": status,
        "isCancellation": False,
        "lastEventDateTime": when,
        "description": name,
        "isin": isin,
        "securityTransactionType": "SAVINGS_PLAN",
        "quantity": quantity,
        "amount": amount,
        "side": side,
    }


def cash(amount: float, when: str, kind: str = "DEPOSIT", *, status: str = "SETTLED", id: str | None = None, description: str = "", isin: str | None = None) -> dict:
    """A BrokerCashTransactionSummary (DEPOSIT, WITHDRAWAL, DISTRIBUTION, INTEREST, FEE, TAX, …)."""
    return {
        "__typename": "BrokerCashTransactionSummary",
        "id": id or uuid.uuid4().hex[:22],
        "currency": "EUR",
        "type": "CASH_TRANSACTION",
        "status": status,
        "isCancellation": False,
        "lastEventDateTime": when,
        "description": description,
        "relatedIsin": isin,
        "cashTransactionType": kind,
        "amount": amount,
    }


#: An auth session, as the external auth flow contract shapes it.
SESSION = "state status finish openUrl expiresAt redirectUrl interval userCode step errorCode errorMessage result { identifier id label }"
SCALABLE_START = "mutation Start($provider: ID!) { startLink(input: {provider: $provider}) { %s } }" % SESSION
SCALABLE_COMPLETE = "mutation Complete($state: String!) { completeAuth(input: {state: $state}) { %s } }" % SESSION
CONNECTION = """
query($id: ID!) { bankConnection(id: $id) { id status linkStep lastError validUntil creator { id sub preferredUsername } accounts { id iban currency kind name } } }
"""


async def connection_of(aexecute, session: dict, context: HttpContext | None = None) -> dict:
    """The connection a DONE auth session linked, as a client reads it."""
    assert session["status"] == "DONE", session
    assert session["result"]["identifier"] == "@bank/connection"
    return (await aexecute(CONNECTION, {"id": session["result"]["id"]}, context=context)).data["bankConnection"]


@pytest.fixture
def scalable_link(aexecute, fakescalable, provider_for):
    """Link this test's Scalable person as tenant A (or ``context``); returns the ACTIVE connection."""

    async def _link(context: HttpContext | None = None) -> dict:
        provider = await provider_for("SCALABLE", context)
        started = (await aexecute(SCALABLE_START, {"provider": provider}, context=context)).data["startLink"]
        fakescalable.approve(started["userCode"])
        completed = await connection_of(aexecute, (await aexecute(SCALABLE_COMPLETE, {"state": started["state"]}, context=context)).data["completeAuth"], context)
        assert completed["status"] == "ACTIVE", completed
        return completed

    return _link


def account(ident: str | None = None, *, iban: str | None = None, currency: str = "EUR", name: str = "Giro", transactions: list[dict] | None = None, balance: str | None = None) -> dict:
    """An account for :meth:`FakeBank.scenario`."""
    ident = ident or uuid.uuid4().hex
    return {
        "identification_hash": ident,
        "account_id": {"iban": iban or f"AT{uuid.uuid4().int % 10**18:018d}"},
        "name": name,
        "currency": currency,
        "product": "Checking",
        "transactions": transactions or [],
        "balances": [{"balance_type": "CLBD", "balance_amount": {"amount": balance, "currency": currency}, "reference_date": None}] if balance is not None else [],
    }


def tx(amount: str, day: str, party: str | None = None, *, iban: str | None = None, remittance: str | None = None, status: str = "BOOK", ref: str | None = None, currency: str = "EUR") -> dict:
    """An Enable Banking transaction; a negative ``amount`` is money out."""
    debit = amount.startswith("-")
    side = "creditor" if debit else "debtor"
    out = {
        "transaction_amount": {"amount": amount.lstrip("-"), "currency": currency},
        "credit_debit_indicator": "DBIT" if debit else "CRDT",
        "status": status,
        "booking_date": day if status == "BOOK" else None,
        "value_date": day,
        "transaction_date": day,
        side: {"name": party} if party else None,
        f"{side}_account": {"iban": iban} if iban else None,
        "remittance_information": [remittance] if remittance else [],
    }
    if ref:
        out["entry_reference"] = ref
    return out


def _context(token: str, sub: str, org_slug: str, roles: list[str] | None = None) -> HttpContext:
    user, _ = User.objects.get_or_create(sub=sub, iss="static_issuer", defaults={"username": f"static_issuer_{sub}"})
    client, _ = Client.objects.get_or_create(client_id="oinsoins")
    org, _ = Organization.objects.get_or_create(slug=org_slug)
    membership, _ = Membership.objects.update_or_create(user=user, organization=org, defaults={"roles": roles or ["editor"]})
    request = UniversalRequest(_extensions={"token": token}, _client=client, _user=user, _organization=org)  # type: ignore[arg-type]
    request.set_membership(membership)  # type: ignore[arg-type]
    return HttpContext(request=request, response=TemporalResponse(), headers={"Authorization": f"Bearer {token}", "User-Agent": "bank-tests", "X-Forwarded-For": "203.0.113.7"}, type="http")


@pytest.fixture
def authenticated_context(transactional_db) -> HttpContext:
    """Tenant A: the static ``test`` token's identity in ``static_org``."""
    return _context("test", "1", "static_org")


@pytest.fixture
def admin_context(transactional_db) -> HttpContext:
    """An admin of tenant A (``static_org``): the only role that may set providers up."""
    return _context("admin", "3", "static_org", roles=["admin"])


@pytest.fixture
def colleague_context(transactional_db) -> HttpContext:
    """Another member of tenant A (``static_org``): same organization, different user."""
    return _context("colleague", "2", "static_org")


@pytest.fixture
def other_org_context(transactional_db) -> HttpContext:
    """Tenant B: the static ``othertest`` token's identity in ``other_org``."""
    return _context("othertest", "9", "other_org")


@pytest.fixture
def aexecute(authenticated_context):
    """Run a GraphQL document as tenant A (or ``context``); fails the test on GraphQL errors unless ``allow_errors``."""

    async def _run(query: str, variables: dict | None = None, context: HttpContext | None = None, allow_errors: bool = False):
        result = await schema.execute(query, variable_values=variables or {}, context_value=context or authenticated_context)
        if not allow_errors:
            assert not result.errors, result.errors
        return result

    return _run


def make_provider(organization: Organization, kind: str, application: Application | None = None, **fields) -> "models.BankProvider":  # noqa: ANN003, F821
    """A provider row as an admin's mutation would leave it (``tests/test_providers.py`` covers the mutations themselves)."""
    from finance import crypto, models
    from finance.providers.enablebanking import read_key
    from finance.providers.registry import kind_of

    backend = kind_of(kind)
    values: dict = {
        "name": fields.pop("name", backend.label),
        "capabilities": sorted(c.value for c in backend.capabilities),
        "daily_sync_limit": backend.default_daily_sync_limit,
    }
    if kind == "ENABLEBANKING":
        assert application is not None
        values["settings"] = {"app_id": application.app_id, "redirect_urls": REDIRECTS, "consent_days": 90, "psu_type": "personal", "key_fingerprint": read_key(application.pem)[1]}
        values["secret"] = crypto.encrypt(application.pem)
    return models.BankProvider.objects.create(organization=organization, kind=kind, **{**values, **fields})


@pytest.fixture
def provider_for(authenticated_context, application):
    """The id of the provider of a kind in tenant A's (or ``context``'s) organization, set up on first use."""
    from channels.db import database_sync_to_async

    from finance import models

    def _get(kind: str, context: HttpContext | None) -> str:
        organization = (context or authenticated_context).request.organization
        row = models.BankProvider.objects.filter(organization=organization, kind=kind).order_by("id").first()
        return str((row or make_provider(organization, kind, application)).id)

    async def _provider(kind: str = "ENABLEBANKING", context: HttpContext | None = None) -> str:
        return await database_sync_to_async(_get)(kind, context)

    return _provider


@pytest.fixture
def eb_provider(authenticated_context, application) -> str:
    """Tenant A's Enable Banking provider (its id), for tests that start links themselves."""
    return str(make_provider(authenticated_context.request.organization, "ENABLEBANKING", application).id)


@pytest.fixture
def sc_provider(authenticated_context) -> str:
    """Tenant A's Scalable provider (its id)."""
    return str(make_provider(authenticated_context.request.organization, "SCALABLE").id)


LINK = 'mutation Start($provider: ID!, $aspsp: String!) { startLink(input: {provider: $provider, institution: $aspsp, country: "AT"}) { %s } }' % SESSION
COMPLETE = "mutation Complete($code: String, $state: String!, $error: String, $description: String) { completeAuth(input: {code: $code, state: $state, error: $error, errorDescription: $description}) { %s } }" % SESSION


@pytest.fixture
def link(aexecute, fakebank, provider_for):
    """Link this test's fake bank as tenant A (or ``context``); returns the completed connection."""

    async def _link(context: HttpContext | None = None) -> dict:
        provider = await provider_for("ENABLEBANKING", context)
        started = await aexecute(LINK, {"provider": provider, "aspsp": fakebank.aspsp}, context=context)
        state = started.data["startLink"]["state"]
        code = fakebank.approve(state)
        completed = await aexecute(COMPLETE, {"code": code, "state": state}, context=context)
        return await connection_of(aexecute, completed.data["completeAuth"], context)

    return _link


@pytest.fixture(scope="session")
def datalayer(backend_stack):
    """The datalayer pointed at the stack's RustFS (root keys, as the deployment's service user), with its bucket."""
    import boto3
    from botocore.config import Config
    from django.conf import settings

    import datalayer.datalayer as dl_module

    settings.DATALAYER = {**settings.DATALAYER, "access_key": "banktestroot", "secret_key": "banktestrootsecret", "port": backend_stack.rustfs_port}
    dl_module.GLOBAL_DL = None
    s3 = boto3.client(
        "s3", endpoint_url=f"http://localhost:{backend_stack.rustfs_port}", aws_access_key_id="banktestroot", aws_secret_access_key="banktestrootsecret", region_name="us-east-1", config=Config(signature_version="s3v4")
    )
    bucket = settings.DATALAYER["bigfile"]["bucket"]
    if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket=bucket)
    yield dl_module.get_current_datalayer()
    dl_module.GLOBAL_DL = None


REQUEST_UPLOAD = """
mutation($name: String!, $size: ByteCount) { requestBigfileUpload(input: {originalFileName: $name, fileSize: $size}) { accessKey secretKey sessionToken region bucket key store } }
"""
FINISH_UPLOAD = "mutation($id: String!) { finishBigfileUpload(input: {storeId: $id}) { id sizeBytes originalFileName } }"


@pytest.fixture
def upload(aexecute, datalayer, backend_stack):
    """Upload bytes as a client does: a scoped grant, a PUT with its temporary credentials, finish. Returns the store id."""
    import boto3
    from botocore.config import Config

    async def _upload(content: bytes, name: str = "export.xlsx", context: HttpContext | None = None) -> str:
        grant = (await aexecute(REQUEST_UPLOAD, {"name": name, "size": len(content)}, context=context)).data["requestBigfileUpload"]
        s3 = boto3.client(
            "s3",
            endpoint_url=f"http://localhost:{backend_stack.rustfs_port}",
            aws_access_key_id=grant["accessKey"],
            aws_secret_access_key=grant["secretKey"],
            aws_session_token=grant["sessionToken"],
            region_name=grant["region"],
            config=Config(signature_version="s3v4"),
        )
        s3.put_object(Bucket=grant["bucket"], Key=grant["key"], Body=content)
        finished = (await aexecute(FINISH_UPLOAD, {"id": grant["store"]}, context=context)).data["finishBigfileUpload"]
        assert finished["sizeBytes"] == len(content)
        return grant["store"]

    return _upload


# --- merchants: the fake geocoder --------------------------------------------------------------


class FakeGeo:
    """Drives fakegeo's /_admin endpoints."""

    def __init__(self, url: str) -> None:
        self.url = url

    def places(self, places: list[dict]) -> None:
        _post(self.url + "/_admin/places", places)

    def reset(self) -> None:
        _post(self.url + "/_admin/reset", {})

    def log(self) -> list[dict]:
        return _get(self.url + "/_admin/log")["log"]


@pytest.fixture
def fakegeo(backend_stack, settings) -> FakeGeo:
    """Point geocoding at fakegeo, with no places until the test adds some."""
    settings.GEOCODING = {**settings.GEOCODING, "enabled": True, "url": backend_stack.fakegeo_url, "country_codes": None}
    geo = FakeGeo(backend_stack.fakegeo_url)
    geo.reset()
    yield geo
    geo.reset()


# --- security prices: the fake market-data APIs -------------------------------------------------

TWELVEDATA_KEY = "test-twelvedata-key"


@pytest.fixture(scope="session", autouse=True)
def prices_offline(backend_stack):
    """Every public price API points at fakemarket, so no test reaches the internet."""
    from django.conf import settings

    url = backend_stack.fakemarket_url
    settings.PRICES = {**settings.PRICES, "openfigi_url": url, "yahoo_url": url, "twelvedata_url": url, "twelvedata_api_key": None, "yahoo_enabled": True}
    yield settings.PRICES


class FakeMarket:
    """Drives fakemarket's /_admin endpoints."""

    def __init__(self, url: str) -> None:
        self.url = url

    def listings(self, listings: dict[str, list[dict]]) -> None:
        _post(self.url + "/_admin/listings", listings)

    def series(self, series: dict[str, dict]) -> None:
        _post(self.url + "/_admin/series", series)

    def reset(self) -> None:
        _post(self.url + "/_admin/reset", {})

    def log(self) -> list[dict]:
        return _get(self.url + "/_admin/log")["log"]


@pytest.fixture
def fakemarket(backend_stack, settings) -> FakeMarket:
    market = FakeMarket(backend_stack.fakemarket_url)
    market.reset()
    yield market
    market.reset()

