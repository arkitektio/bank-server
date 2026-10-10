"""Enable Banking as a provider kind: PSD2 bank consents through one Enable Banking application.

An instance is one application: its id, its RS256 private key (encrypted in the row's
``secret``) and the redirect URLs registered for it. Where the API lives is the deployment's
(``enablebanking.api_url``), not the instance's.
"""

import hashlib
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Literal

from channels.db import database_sync_to_async
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel, Field

from finance import crypto, models
from finance.enablebanking import linking
from finance.enablebanking.client import EnableBankingClient, EnableBankingConfig, endpoint
from finance.providers.base import AuthSession, BackendBase, Finish, Institution, LinkCompletion, LinkRequest, ProviderCheck, PsuHeaders
from finance.providers.errors import LinkError, NotConfigured

if TYPE_CHECKING:
    from authentikate.models import Organization, User

    from finance.sync import SyncResult


class EnableBankingSettings(BaseModel):
    """The non-secret settings of an Enable Banking provider (the row's ``settings``)."""

    app_id: str = Field(description="The application id; also the `kid` of every JWT signed for it.")
    redirect_urls: list[str] = Field(default_factory=list, description="Redirect URLs registered for the application; the first is the default.")
    consent_days: int = Field(default=90, ge=1, description="How long a new consent is requested for, in days (banks may cap it).")
    psu_type: Literal["personal", "business"] = Field(default="personal", description="PSU type sent on authorization.")
    key_fingerprint: str = Field(default="", description="SHA-256 of the stored key's public half, to recognise it by.")


class InvalidKey(ValueError):
    """The text is not an RSA private key in PEM."""


def read_key(pem: str) -> tuple[bytes, str]:
    """The PEM as bytes and the fingerprint of its public half; raises :class:`InvalidKey` for anything but an RSA private key."""
    data = pem.strip().encode()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as error:
        raise InvalidKey("The private key is not a PEM file (an unencrypted `-----BEGIN PRIVATE KEY-----` block is expected).") from error
    if not isinstance(key, rsa.RSAPrivateKey):
        raise InvalidKey("The private key is not an RSA key; Enable Banking applications sign with RS256.")
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashlib.sha256(public).hexdigest()
    return data, ":".join(digest[i : i + 2] for i in range(0, 32, 2))


class EnableBankingBackend(BackendBase):
    """PSD2 bank consents through an Enable Banking application."""

    kind: ClassVar[models.Provider] = models.Provider.ENABLEBANKING
    label: ClassVar[str] = "Enable Banking"
    description: ClassVar[str] = "Bank accounts across Europe through PSD2 consents, with your own Enable Banking application (its id and private key)."
    finish: ClassVar[Finish] = "REDIRECT"
    capabilities: ClassVar[frozenset[models.ProviderCapability]] = frozenset(
        {models.ProviderCapability.TRANSACTIONS, models.ProviderCapability.BALANCES, models.ProviderCapability.SCHEDULED_SYNC}
    )
    has_institutions: ClassVar[bool] = True
    default_daily_sync_limit: ClassVar[int | None] = 4

    def settings(self) -> EnableBankingSettings:
        """The instance's settings, validated."""
        return EnableBankingSettings.model_validate(self.provider.settings)

    def config(self) -> EnableBankingConfig:
        """The client configuration of this application; decrypts its key."""
        if not self.provider.secret:
            raise NotConfigured(f"The provider {self.provider.name!r} holds no private key; set one.")
        values = self.settings()
        api_url, timeout = endpoint()
        return EnableBankingConfig(
            app_id=values.app_id,
            private_key=crypto.decrypt(self.provider.secret).encode(),
            api_url=api_url,
            redirect_urls=values.redirect_urls,
            consent_days=values.consent_days,
            psu_type=values.psu_type,
            timeout_seconds=timeout,
        )

    def client(self) -> EnableBankingClient:
        """A client acting as this application; open it with ``async with``."""
        return EnableBankingClient(self.config())

    async def verify(self) -> ProviderCheck:
        """Ask Enable Banking which application the key belongs to; fails when it knows neither."""
        async with self.client() as client:
            application = await client.application()
        return ProviderCheck(name=application.name, environment=application.environment, active=application.active, redirect_urls=application.redirect_urls)

    async def institutions(self, country: str) -> list[Institution]:
        """The banks (ASPSPs) the application can reach in a country."""
        async with self.client() as client:
            aspsps = await client.aspsps(country)
        return [
            Institution(
                name=aspsp["name"],
                country=aspsp.get("country", country),
                logo=aspsp.get("logo"),
                bic=aspsp.get("bic"),
                maximum_consent_days=(aspsp.get("maximum_consent_validity") or 0) // 86400 or None,
            )
            for aspsp in aspsps
        ]

    async def start_link(self, organization: "Organization", creator: "User", request: LinkRequest) -> models.BankConnection:
        """Start a consent at the chosen bank."""
        if not request.institution or not request.country:
            raise LinkError("Linking through Enable Banking needs the bank (`institution`) and its `country`.", code=None)
        async with self.client() as client:
            return await linking.start_link(self.provider, client, organization, creator, request.institution, request.country.upper(), request.redirect_url)

    async def complete_link(self, connection: models.BankConnection, completion: LinkCompletion) -> models.BankConnection:
        """Exchange the redirect's code for the consented accounts."""
        if not completion.code:
            raise LinkError("Finishing a bank link needs the `code` the bank redirected back with.", code=None)
        async with self.client() as client:
            return await linking.complete_link(client, connection, completion.code)

    async def revoke(self, connection: models.BankConnection) -> None:
        """Withdraw the consent at the bank (best effort)."""
        async with self.client() as client:
            await linking.revoke(client, connection)

    @classmethod
    def describe(cls, connection: models.BankConnection) -> AuthSession:
        """The bank redirects the browser to ``redirect_url`` with ``?code&state``."""
        raw = connection.raw or {}
        return AuthSession(
            connection=connection,
            state=connection.state,
            open_url=raw.get("auth_url") or "",
            expires_at=connection.pending_expires_at,
            finish=cls.finish,
            redirect_url=connection.redirect_url or None,
        )

    async def sync(self, syncer: models.AccountSyncer, since: date | None, psu_headers: PsuHeaders | None) -> "SyncResult":
        """Fetch the account's transactions and balances (those switched on) and upsert them."""
        from finance.sync import persist

        transactions: list[dict] | None = None
        balances: list[dict] = []
        async with self.client() as client:
            if self.has(models.ProviderCapability.TRANSACTIONS):
                transactions = await client.transactions(syncer.remote_id, since, psu_headers=psu_headers)
            if self.has(models.ProviderCapability.BALANCES):
                balances = await client.balances(syncer.remote_id, psu_headers=psu_headers)
        result: SyncResult = await database_sync_to_async(persist)(syncer.id, transactions, balances, since)
        return result
