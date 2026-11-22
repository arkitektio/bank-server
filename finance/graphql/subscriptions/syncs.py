"""Realtime sync events (per organization) and login progress."""

from collections.abc import AsyncGenerator
from contextlib import aclosing

import strawberry
from django.utils import timezone
from kante.types import Info

from finance import auth_sessions, types
from finance.channels import (
    account_sync_channel,
    auth_group,
    auth_session_channel,
    org_group,
)
from finance.graphql.errors import translate

__all__ = ["account_syncs", "auth_session"]

#: A login being watched is read again at least this often, so an expiry (which nothing announces) shows.
AUTH_RECHECK_SECONDS = 10.0


async def account_syncs(
    self, info: Info, account: strawberry.ID | None = None
) -> AsyncGenerator[types.AccountSyncEvent, None]:
    """Stream an event whenever one of this organization's accounts finished syncing (only `account`, when given)."""
    group = org_group(info.context.request.organization.id)
    async for signal in account_sync_channel.listen(info.context, [group]):
        if account is not None and str(signal.account_id) != str(account):
            continue
        yield types.AccountSyncEvent(
            account_id=strawberry.ID(str(signal.account_id)),
            created=signal.created,
            updated=signal.updated,
            pending_replaced=signal.pending_replaced,
        )


def _key(session: auth_sessions.AuthSession) -> tuple[object, ...]:
    return (
        session.status,
        session.step,
        session.error_code,
        session.result.id if session.result else None,
    )


async def auth_session(
    self, info: Info, state: str
) -> AsyncGenerator[types.AuthSession, None]:
    """The login as it is now, then again whenever it changes, until it is no longer PENDING. Only its starter may watch it."""
    request = info.context.request
    try:
        connection = await auth_sessions.find(request.organization, request.user, state)
    except Exception as error:
        raise translate(error) from error
    session = auth_sessions.describe(connection)
    yield types.AuthSession.of(session)
    seen = _key(session)
    while session.status == "PENDING":
        # Until something is announced, or at the latest until the login's time runs out (nothing announces that).
        left = (connection.pending_expires_at - timezone.now()).total_seconds()
        async with aclosing(
            auth_session_channel.listen(
                info.context,
                [auth_group(state)],
                timeout=min(max(left, 0.0) + 0.5, AUTH_RECHECK_SECONDS),
            )
        ) as signals:
            async for _ in signals:
                break
        await connection.arefresh_from_db()
        session = auth_sessions.describe(connection)
        if _key(session) != seen:
            seen = _key(session)
            yield types.AuthSession.of(session)
