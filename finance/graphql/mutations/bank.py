"""Linking banks and syncing accounts, through the organization's providers.

These talk to a provider, so they are async: the HTTP calls never block the event loop.
"""

import logging
from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.types import Info

from finance import auth_sessions, linking, models, types
from finance.graphql.errors import translate as _translate
from finance.graphql.utils import aget_or_404, psu_headers
from finance.providers.base import LinkRequest
from finance.providers.errors import LinkError
from finance.sync import sync_account as run_sync

__all__ = [
    "StartLinkInput",
    "CompleteAuthInput",
    "start_link",
    "complete_auth",
    "resume_auth",
    "cancel_auth",
    "auth_session",
    "revoke_bank_connection",
    "sync_account",
    "sync_connection",
]

logger = logging.getLogger(__name__)


@strawberry.input(description="Start a login through one of the organization's providers.")
class StartLinkInput:
    provider: strawberry.ID = strawberry.field(description="The provider to link through (see `bankProviders`).")
    institution: Optional[str] = strawberry.field(default=None, description="For a kind with institutions: the bank's name exactly as `bankInstitutions` lists it.")
    country: Optional[str] = strawberry.field(default=None, description="For a kind with institutions: the bank's ISO country code, e.g. AT.")
    redirect_url: Optional[str] = strawberry.field(default=None, description="REDIRECT kinds: one of the provider's redirect URLs; the first by default.")


@strawberry.input(description="Finish (REDIRECT) or advance (POLL) a started login.")
class CompleteAuthInput:
    state: str
    code: Optional[str] = strawberry.field(default=None, description="REDIRECT: the `code` query parameter of the redirect. POLL: omitted.")
    error: Optional[str] = strawberry.field(default=None, description="REDIRECT: the provider's `error` / `error_description`, when it refused.")
    error_description: Optional[str] = None


async def start_link(info: Info, input: StartLinkInput) -> types.AuthSession:
    """Start a login through a provider. Open `openUrl`, then finish as the session's `finish` says (see `completeAuth`)."""
    request = info.context.request
    provider = await aget_or_404(models.BankProvider, info, input.provider)
    try:
        connection = await linking.start_link(request.organization, request.user, provider, LinkRequest(institution=input.institution, country=input.country, redirect_url=input.redirect_url))
    except Exception as error:
        raise _translate(error) from error
    logger.info("Login %s started through %s by user %s.", connection.id, provider.kind, request.user.id)
    return types.AuthSession.of(auth_sessions.describe(connection))


def _settled(call: str, connection: models.BankConnection, session: auth_sessions.AuthSession) -> None:
    """One line when a call leaves a login anywhere but PENDING (a poll step that changes nothing says nothing)."""
    if session.status != "PENDING":
        logger.info("%s: login %s is %s%s.", call, connection.id, session.status, f" ({session.error_code}: {session.error_message})" if session.error_message else "")


async def _own_login(info: Info, state: str) -> models.BankConnection:
    request = info.context.request
    try:
        return await auth_sessions.find(request.organization, request.user, state)
    except LinkError:
        logger.info("%s: user %s has no login with the state it sent.", info.field_name, request.user.id)
        raise


async def complete_auth(info: Info, input: CompleteAuthInput) -> types.AuthSession:
    """REDIRECT: finish with the code. POLL: advance one step; call until not PENDING.

    A login that is settled (DONE, FAILED, EXPIRED, CANCELLED) is answered again as it is: nothing is exchanged twice.
    """
    try:
        connection = await _own_login(info, input.state)
        session = await auth_sessions.complete(connection, input.code, input.error, input.error_description)
        _settled("completeAuth", connection, session)
        return types.AuthSession.of(session)
    except Exception as error:
        raise _translate(error) from error


async def resume_auth(info: Info, state: str) -> types.AuthSession:
    """The same login again (a fresh openUrl if the old one cannot be reused)."""
    try:
        return types.AuthSession.of(auth_sessions.resume(await _own_login(info, state)))
    except Exception as error:
        raise _translate(error) from error


async def cancel_auth(info: Info, state: str) -> types.AuthSession:
    """Drop a login that will not be finished. Idempotent."""
    try:
        connection = await _own_login(info, state)
        session = await auth_sessions.cancel(connection)
        _settled("cancelAuth", connection, session)
        return types.AuthSession.of(session)
    except Exception as error:
        raise _translate(error) from error


async def auth_session(info: Info, state: str) -> types.AuthSession:
    """Where a login is. No side effect."""
    try:
        return types.AuthSession.of(auth_sessions.describe(await _own_login(info, state)))
    except Exception as error:
        raise _translate(error) from error


async def revoke_bank_connection(info: Info, id: strawberry.ID) -> types.BankConnection:
    """Withdraw the consent at the bank and stop syncing. Accounts and transactions are kept."""
    connection = await aget_or_404(models.BankConnection, info, id)
    try:
        return await linking.revoke_link(connection)  # type: ignore[return-value]
    except Exception as error:
        raise _translate(error) from error


async def _sync(info: Info, account_id: int) -> types.SyncResult:
    try:
        result = await run_sync(account_id, psu_headers=psu_headers(info))
    except Exception as error:
        raise _translate(error) from error
    account = await models.BankAccount.objects.aget(id=account_id)
    return types.SyncResult(account=account, created=result.created, updated=result.updated, pending_replaced=result.pending_replaced, balances=result.balances, categorized=result.categorized, holdings=result.holdings)  # type: ignore[arg-type]


async def sync_account(info: Info, id: strawberry.ID) -> types.SyncResult:
    """Pull an account from its provider now, inside this request. Fails with RATE_LIMITED (without contacting the provider) when `nextSyncAllowedAt` has not passed."""
    account = await aget_or_404(models.BankAccount, info, id)
    return await _sync(info, account.id)


async def sync_connection(info: Info, id: strawberry.ID) -> list[types.SyncResult]:
    """Pull every account of a connection now."""
    connection = await aget_or_404(models.BankConnection, info, id)
    ids = await database_sync_to_async(lambda: list(models.BankAccount.objects.filter(syncers__connection=connection).distinct().order_by("id").values_list("id", flat=True)))()
    return [await _sync(info, account_id) for account_id in ids]
