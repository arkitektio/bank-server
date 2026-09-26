"""A pending link, described the same way for every provider.

The service never handles the browser leg of a login. It returns an *auth session*: what the
client opens (``open_url``) and how the login finishes (``finish``):

* ``REDIRECT`` — Enable Banking: the bank redirects the browser to ``redirect_url`` with
  ``?code&state`` (through a relay the client catches); the client calls ``completeBankLink``.
* ``POLL`` — Scalable: the client calls ``completeScalableLink(state)`` every ``interval``
  seconds until the connection is ACTIVE; the user checks ``user_code`` on the provider's page.

Everything is read from the stored PENDING connection, so any replica can describe, resume or
cancel it, and a client that lost its dialog can pick the login up again (:func:`resume`).
"""

from dataclasses import dataclass
from datetime import datetime

from channels.db import database_sync_to_async
from kante.errors import PermissionDenied

from finance import models
from finance.linking import LinkError


@dataclass
class AuthSession:
    connection: models.BankConnection
    state: str
    open_url: str
    expires_at: datetime
    finish: str  # "REDIRECT" | "POLL"
    interval: int | None = None
    user_code: str | None = None
    redirect_url: str | None = None


def describe(connection: models.BankConnection) -> AuthSession:
    """The auth session of a (just started or pending) connection."""
    raw = connection.raw or {}
    if connection.provider == models.Provider.SCALABLE:
        return AuthSession(
            connection=connection,
            state=connection.state,
            open_url=raw.get("verification_uri_complete") or raw.get("verification_uri") or "",
            expires_at=connection.pending_expires_at,
            finish="POLL",
            interval=connection.link_poll_interval,
            user_code=raw.get("user_code"),
        )
    return AuthSession(
        connection=connection,
        state=connection.state,
        open_url=raw.get("auth_url") or "",
        expires_at=connection.pending_expires_at,
        finish="REDIRECT",
        redirect_url=connection.redirect_url or None,
    )


def _pending_of_creator(connection: models.BankConnection, user) -> None:  # noqa: ANN001 - authentikate User
    if connection.creator_id != user.id:
        raise PermissionDenied("Only the user who started this link can resume or cancel it.")
    if connection.status != models.ConnectionStatus.PENDING:
        raise LinkError(f"This link is {connection.status.lower()}, not pending.")


def resume(connection: models.BankConnection, user) -> AuthSession:  # noqa: ANN001
    """The stored auth session of the caller's PENDING link, to continue a login after the dialog closed."""
    from django.utils import timezone

    _pending_of_creator(connection, user)
    if connection.pending_expires_at < timezone.now():
        raise LinkError("This link expired; start a new one.", code=models.BankErrorCode.CODE_EXPIRED)
    return describe(connection)


async def cancel(connection: models.BankConnection, user) -> int:  # noqa: ANN001
    """Delete the caller's PENDING link; a Scalable login already past the code step is logged out first."""
    await database_sync_to_async(_pending_of_creator)(connection, user)
    if connection.provider == models.Provider.SCALABLE and connection.secret:
        from finance.scalable.linking import revoke_scalable

        await revoke_scalable(connection)
    connection_id = connection.id
    await connection.adelete()
    return connection_id
