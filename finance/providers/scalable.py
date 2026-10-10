"""Scalable Capital as a provider kind: a broker login through Scalable's official CLI API.

An instance holds no credentials of its own (the CLI's client id is public; each connection
keeps its own encrypted tokens), so creating one only says "this organization uses Scalable" and
which capabilities are on. The endpoints are the deployment's (``scalable`` config block).
"""

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING, ClassVar

from channels.db import database_sync_to_async
from django.db.models import Min
from django.utils import timezone
from pydantic import BaseModel

from finance import models
from finance.providers.base import AuthSession, BackendBase, Finish, Institution, LinkCompletion, LinkRequest, ProviderCheck, PsuHeaders
from finance.scalable import linking
from finance.scalable import sync as scalable_sync
from finance.scalable.client import ScalableClient, Unauthorized
from finance.scalable.tokens import refreshed_session, session_for

if TYPE_CHECKING:
    from authentikate.models import Organization, User

    from finance.sync import SyncResult

logger = logging.getLogger(__name__)


class ScalableSettings(BaseModel):
    """A Scalable provider has no settings of its own."""


class ScalableBackend(BackendBase):
    """A Scalable Capital broker login (device code, then the second factor)."""

    kind: ClassVar[models.Provider] = models.Provider.SCALABLE
    label: ClassVar[str] = "Scalable Capital"
    description: ClassVar[str] = "Broker, depot and overnight savings through Scalable's official CLI login. Enable Profile > Security > Agentic Investing at Scalable first."
    finish: ClassVar[Finish] = "POLL"
    capabilities: ClassVar[frozenset[models.ProviderCapability]] = frozenset(models.ProviderCapability)
    has_institutions: ClassVar[bool] = False
    default_daily_sync_limit: ClassVar[int | None] = None

    async def verify(self) -> ProviderCheck:
        """Nothing to prove: the instance holds no credentials."""
        return ProviderCheck(name=self.label)

    async def institutions(self, country: str) -> list[Institution]:
        """Scalable is one institution; there is nothing to pick."""
        return []

    async def start_link(self, organization: "Organization", creator: "User", request: LinkRequest) -> models.BankConnection:
        """Start a device login."""
        return await linking.start_scalable_link(self.provider, organization, creator)

    async def complete_link(self, connection: models.BankConnection, completion: LinkCompletion) -> models.BankConnection:
        """Advance the login by one step; PENDING until the user approved everything."""
        return await linking.complete_scalable_link(connection)

    async def revoke(self, connection: models.BankConnection) -> None:
        """Log the connection out at Scalable and forget its credentials."""
        if connection.secret:
            await linking.revoke_scalable(connection)

    @classmethod
    def describe(cls, connection: models.BankConnection) -> AuthSession:
        """The user opens the verification page and checks ``user_code``; the client polls."""
        raw = connection.raw or {}
        return AuthSession(
            connection=connection,
            state=connection.state,
            open_url=raw.get("verification_uri_complete") or raw.get("verification_uri") or "",
            expires_at=connection.pending_expires_at,
            finish=cls.finish,
            interval=connection.link_poll_interval,
            user_code=raw.get("user_code"),
        )

    async def sync(self, syncer: models.AccountSyncer, since: date | None, psu_headers: PsuHeaders | None) -> "SyncResult":
        """Fetch the pot (the parts switched on) and upsert it; then the depot's prices, if those are on."""
        from finance.sync import SyncResult

        if syncer.connection_id is None:
            raise AssertionError("a syncer is only synced through its connection")
        # An order can stay pending for weeks: page back far enough to see every pending row again.
        oldest_pending = await syncer.transactions.filter(status=models.TransactionStatus.PENDING).aaggregate(oldest=Min("booking_date"))
        if since and oldest_pending["oldest"]:
            since = min(since, oldest_pending["oldest"])
        async with ScalableClient() as client:
            session = await session_for(syncer.connection_id, client)
            try:
                fetched = await scalable_sync.fetch(syncer, session, client, since, self._enabled)
            except Unauthorized:
                # As the CLI does: refresh once and retry. Only a failed refresh (invalid_grant) means relogin.
                session = await refreshed_session(syncer.connection_id, client, session)
                fetched = await scalable_sync.fetch(syncer, session, client, since, self._enabled)
        result = SyncResult(account_id=syncer.account_id)
        await database_sync_to_async(scalable_sync.persist)(syncer.id, fetched, result)
        if fetched.holdings and self.has(models.ProviderCapability.PRICES):
            # The depot's last month of prices from Scalable, in this same request; never fails the sync.
            from finance.prices import service as prices

            try:
                isins = sorted({item["isin"] for item in fetched.holdings if item.get("isin")})
                today = timezone.now().date()
                await prices.refresh(syncer.organization_id, isins, today - timedelta(days=31), today, only=[models.PriceSource.SCALABLE])
            except Exception:
                logger.warning("Refreshing prices after the depot sync of account %s failed.", syncer.account_id, exc_info=True)
        logger.info("Synced Scalable account %s: %s new, %s updated, %s holdings", syncer.account_id, result.created, result.updated, result.holdings)
        return result
