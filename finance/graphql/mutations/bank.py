"""Linking banks and syncing accounts.

These talk to Enable Banking, so they are async: the HTTP calls never block the event loop.
"""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.types import Info

from finance import auth_sessions, enums, linking, models, types
from finance.scalable import linking as scalable_linking
from finance.graphql.errors import translate as _translate
from finance.graphql.utils import aget_or_404, psu_headers
from finance.sync import sync_account as run_sync

__all__ = [
    "StartBankLinkInput",
    "CompleteBankLinkInput",
    "start_bank_link",
    "complete_bank_link",
    "revoke_bank_connection",
    "sync_account",
    "sync_connection",
    "start_scalable_link",
    "complete_scalable_link",
    "resume_link",
    "cancel_link",
]


@strawberry.input(description="Start linking a bank.")
class StartBankLinkInput:
    aspsp_name: str = strawberry.field(description="The bank's name exactly as `bankInstitutions` lists it.")
    country: str = strawberry.field(description="The bank's ISO country code, e.g. AT.")
    redirect_url: Optional[str] = strawberry.field(default=None, description="One of the server's registered redirect URLs; the first by default.")


@strawberry.input(description="Finish a bank link with what the bank redirected back with.")
class CompleteBankLinkInput:
    code: str = strawberry.field(description="The `code` query parameter of the redirect.")
    state: str = strawberry.field(description="The `state` query parameter of the redirect.")


def _session(session: auth_sessions.AuthSession) -> types.AuthSession:
    return types.AuthSession(
        state=session.state,
        open_url=session.open_url,
        expires_at=session.expires_at,
        finish=enums.AuthFinish(session.finish),
        interval=session.interval,
        user_code=session.user_code,
        redirect_url=session.redirect_url,
        connection=session.connection,  # type: ignore[arg-type]
    )


async def start_bank_link(info: Info, input: StartBankLinkInput) -> types.AuthSession:
    """Start a consent at a bank (finish: REDIRECT). Open `openUrl`; the bank redirects to `redirectUrl` with `code` and `state` for `completeBankLink`."""
    request = info.context.request
    try:
        connection, _ = await linking.start_link(request.organization, request.user, input.aspsp_name, input.country.upper(), input.redirect_url)
    except Exception as error:
        raise _translate(error) from error
    return _session(auth_sessions.describe(connection))


async def complete_bank_link(info: Info, input: CompleteBankLinkInput) -> types.BankConnection:
    """Exchange the redirect's code for access to the consented accounts. Only works for a link started in this organization."""
    try:
        connection = await linking.complete_link(info.context.request.organization.id, input.code, input.state)
    except Exception as error:
        raise _translate(error) from error
    return connection  # type: ignore[return-value]


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


async def start_scalable_link(info: Info) -> types.AuthSession:
    """Start a Scalable Capital login (finish: POLL; Scalable's official CLI login — enable it under Profile > Security > Agentic Investing first).

    Open `openUrl`, then call `completeScalableLink(state)` every `interval` seconds until the connection is ACTIVE.
    """
    request = info.context.request
    try:
        connection, _ = await scalable_linking.start_scalable_link(request.organization, request.user)
    except Exception as error:
        raise _translate(error) from error
    return _session(auth_sessions.describe(connection))


async def complete_scalable_link(info: Info, state: str) -> types.BankConnection:
    """Advance a Scalable login. Returns the connection: PENDING (see `linkStep`) until the user approved the login code and any second factor, then ACTIVE with its accounts."""
    try:
        connection = await scalable_linking.complete_scalable_link(info.context.request.organization.id, state)
    except Exception as error:
        raise _translate(error) from error
    return connection  # type: ignore[return-value]


async def resume_link(info: Info, connection: strawberry.ID) -> types.AuthSession:
    """The stored auth session of a PENDING link you started, to continue a login after the dialog closed."""
    pending = await aget_or_404(models.BankConnection, info, connection)
    try:
        return _session(await database_sync_to_async(auth_sessions.resume)(pending, info.context.request.user))
    except Exception as error:
        raise _translate(error) from error


async def cancel_link(info: Info, connection: strawberry.ID) -> strawberry.ID:
    """Delete a PENDING link you started; returns its id."""
    pending = await aget_or_404(models.BankConnection, info, connection)
    try:
        return strawberry.ID(str(await auth_sessions.cancel(pending, info.context.request.user)))
    except Exception as error:
        raise _translate(error) from error
