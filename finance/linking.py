"""Linking through a provider, whatever its kind.

A link is started at a :class:`~finance.models.BankProvider` the organization set up, and from
then on the stored connection says which provider finishes, syncs and revokes it. How a login
works (a bank redirect, a device code) is the kind's own (:mod:`finance.providers`); how a
started login is finished, read, resumed and cancelled is :mod:`finance.auth_sessions`.
"""

from typing import TYPE_CHECKING

from finance import models
from finance.providers.base import LinkRequest
from finance.providers.errors import PENDING_TTL, LinkError
from finance.providers.registry import backend_for, provider_of, usable

if TYPE_CHECKING:
    from authentikate.models import Organization, User

__all__ = ["PENDING_TTL", "LinkError", "start_link", "revoke_link"]


async def start_link(organization: "Organization", creator: "User", provider: models.BankProvider, request: LinkRequest) -> models.BankConnection:
    """Start a login at ``provider``; returns the PENDING connection."""
    return await usable(provider, "This link").start_link(organization, creator, request)


async def revoke_link(connection: models.BankConnection) -> models.BankConnection:
    """Withdraw the consent at the provider (best effort) and stop syncing its accounts. Data stays."""
    provider = await provider_of(connection)
    if provider is not None:  # a disabled provider still lets its consents be withdrawn
        try:
            await backend_for(provider).revoke(connection)
        except Exception as error:  # the consent may already be gone, or the key no longer be the provider's
            connection.last_error = str(error)[:2000]
    connection.status = models.ConnectionStatus.REVOKED
    await connection.asave(update_fields=["status", "last_error"])
    return connection
