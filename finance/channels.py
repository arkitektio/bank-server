"""Realtime channels for the ``accountSyncs`` and ``authSession`` subscriptions.

Groups are per organization (:func:`org_group`): a sync is only ever broadcast to — and only
ever listened for in — the organization the account belongs to. Not kante's
``Channel.org_group``: its ``:``-separated names are not valid channel-layer group names.
"""

import hashlib
from dataclasses import asdict

from kante.channel import build_channel
from pydantic import BaseModel, Field


class AccountSyncSignal(BaseModel):
    """An account finished syncing."""

    account_id: int = Field(description="The account that was synced.")
    created: int = Field(default=0, description="New transactions.")
    updated: int = Field(
        default=0, description="Transactions whose bank fields changed."
    )
    pending_replaced: int = Field(
        default=0, description="Pending transactions dropped and re-read."
    )
    balances: int = Field(default=0, description="Balance snapshots written.")
    categorized: int = Field(
        default=0, description="Transactions whose category a rule changed."
    )


account_sync_channel = build_channel(AccountSyncSignal, name="bank_account_syncs")


def org_group(organization_id: int) -> str:
    """The channel group of one organization."""
    return f"bank_account_syncs.org.{organization_id}"


def broadcast_sync(organization_id: int, result: object) -> None:
    """Tell the organization's subscribers an account was synced."""
    account_sync_channel.broadcast(
        AccountSyncSignal(**asdict(result)), [org_group(organization_id)]
    )


class AuthSessionSignal(BaseModel):
    """A login changed: the subscriber reads it again."""

    changed: bool = True


auth_session_channel = build_channel(AuthSessionSignal, name="bank_auth_sessions")


def auth_group(state: str) -> str:
    """The channel group of one login: a hash of its state, so the state never travels as a group name. Only its starter may listen."""
    return f"bank_auth.{hashlib.sha256(state.encode()).hexdigest()[:40]}"


async def announce_auth(state: str) -> None:
    """Tell whoever watches the login that it changed."""
    await auth_session_channel.abroadcast(AuthSessionSignal(), [auth_group(state)])
