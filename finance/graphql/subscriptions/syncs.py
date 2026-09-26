"""Realtime sync events, per organization."""

from typing import AsyncGenerator

import strawberry
from kante.types import Info

from finance import types
from finance.channels import account_sync_channel, org_group

__all__ = ["account_syncs"]


async def account_syncs(self, info: Info) -> AsyncGenerator[types.AccountSyncEvent, None]:
    """Stream an event whenever one of this organization's accounts finished syncing."""
    group = org_group(info.context.request.organization.id)
    async for signal in account_sync_channel.listen(info.context, [group]):
        yield types.AccountSyncEvent(account_id=strawberry.ID(str(signal.account_id)), created=signal.created, updated=signal.updated, pending_replaced=signal.pending_replaced)
