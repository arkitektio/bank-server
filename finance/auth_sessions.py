"""A login at a provider, as the external auth flow contract describes it.

The service never handles the browser leg of a login. It hands the client an *auth session*:
what to open (``open_url``) and how the login finishes (``finish``):

* ``REDIRECT`` — Enable Banking: the bank redirects the browser to ``redirect_url`` with
  ``?code&state`` (through a relay the client catches); the client calls ``completeAuth`` with both.
* ``POLL`` — Scalable: the client calls ``completeAuth`` with the state every ``interval``
  seconds until the session is no longer PENDING; the user checks ``user_code`` on the provider's page.

The login *is* the stored :class:`~finance.models.BankConnection`: its ``state`` is the handle,
and everything a session says is read from the row, so any replica can describe, advance, resume
or cancel it. A state is only ever answered to the member who started the login, in the
organization they started it in (:func:`find`).
"""

from typing import TYPE_CHECKING

from channels.db import database_sync_to_async
from django.utils import timezone

from finance import models
from finance.providers.base import AuthResult, AuthSession, AuthStatus, LinkCompletion
from finance.providers.errors import LinkError
from finance.providers.registry import backend_for, kind_of, provider_of, usable

if TYPE_CHECKING:
    from authentikate.models import Organization, User

__all__ = ["AuthSession", "CONNECTION", "describe", "find", "complete", "resume", "cancel"]

#: What a finished login links, under the identifier the app opens a connection's page by (its
#: manifest's; the hub contract in ``bank_server.contract`` calls the same model ``@bank/bankconnection``).
CONNECTION = "@bank/connection"

_TIMED_OUT = "The login was not completed in time."


def _step(connection: models.BankConnection) -> str | None:
    """What a login is still waiting for after its first approval (None before it)."""
    return models.LinkStep.MFA.value if connection.link_step == models.LinkStep.MFA else None


def _status(connection: models.BankConnection) -> AuthStatus:
    if connection.linked_at is not None:
        return "DONE"  # also when the consent was revoked or ran out later: the login itself finished
    if connection.status == models.ConnectionStatus.CANCELLED:
        return "CANCELLED"
    if connection.status == models.ConnectionStatus.PENDING:
        # Nothing flips a login on a timer: past its time with no approval, it reads as expired.
        return "EXPIRED" if _step(connection) is None and connection.pending_expires_at < timezone.now() else "PENDING"
    return "EXPIRED" if connection.last_error_code == models.BankErrorCode.CODE_EXPIRED else "FAILED"


def describe(connection: models.BankConnection) -> AuthSession:
    """The auth session of a connection: how its kind opens and finishes it, and where it is."""
    session = kind_of(connection.provider).describe(connection)
    session.status = _status(connection)
    session.step = _step(connection) if session.status == "PENDING" else None
    if session.status == "DONE":
        session.result = AuthResult(identifier=CONNECTION, id=str(connection.id), label=connection.aspsp_name)
    elif session.status in ("FAILED", "EXPIRED"):
        session.error_code = connection.last_error_code or (models.BankErrorCode.CODE_EXPIRED.value if session.status == "EXPIRED" else None)
        session.error_message = connection.last_error or (_TIMED_OUT if session.status == "EXPIRED" else "The provider refused the login.")
    return session


async def find(organization: "Organization", user: "User", state: str) -> models.BankConnection:
    """The login with ``state`` that ``user`` started in ``organization``; anything else is the same as no login."""
    connection = await models.BankConnection.objects.filter(organization_id=organization.id, creator_id=user.id, state=state).afirst()
    if connection is None:
        raise LinkError("No login with this state: it belongs to another login attempt, another member or another organization.")
    return connection


def _refuse(connection_id: int, message: str) -> None:
    models.BankConnection.objects.filter(id=connection_id, status=models.ConnectionStatus.PENDING).update(
        status=models.ConnectionStatus.FAILED, last_error=message[:2000], last_error_code=models.BankErrorCode.LOGIN_REFUSED, secret=None, token_lease_until=None
    )


async def complete(connection: models.BankConnection, code: str | None = None, error: str | None = None, error_description: str | None = None) -> AuthSession:
    """Finish (REDIRECT) or advance (POLL) a login; a settled one is answered again as it is.

    ``error`` is the provider's own refusal, as its redirect carried it: the login ends FAILED
    with the provider's words.
    """
    if connection.status == models.ConnectionStatus.PENDING:
        if error or error_description:
            await database_sync_to_async(_refuse)(connection.id, error_description or error or "")
            await connection.arefresh_from_db()
        else:
            backend = usable(await provider_of(connection), "This login")
            connection = await backend.complete_link(connection, LinkCompletion(code=code))
    return describe(connection)


def resume(connection: models.BankConnection) -> AuthSession:
    """The same login again, to continue it after its dialog closed."""
    return describe(connection)


def _cancel(connection_id: int) -> None:
    models.BankConnection.objects.filter(id=connection_id, status=models.ConnectionStatus.PENDING).update(
        status=models.ConnectionStatus.CANCELLED, secret=None, token_expires_at=None, token_lease_until=None
    )


async def cancel(connection: models.BankConnection) -> AuthSession:
    """Drop a login that will not be finished; one already past its first step is logged out at the provider first.

    The row stays (CANCELLED, without credentials) so the login can still be read. A settled
    login is answered as it is.
    """
    if connection.status == models.ConnectionStatus.PENDING:
        provider = await provider_of(connection)
        if provider is not None:
            try:
                await backend_for(provider).revoke(connection)
            except Exception:  # noqa: S110 - best effort: the login is dropped either way
                pass
        await database_sync_to_async(_cancel)(connection.id)
        await connection.arefresh_from_db()
    return describe(connection)
