"""Linking a bank: the consent flow, completed by the client.

1. ``start_link`` asks Enable Banking for the bank's login URL and stores a PENDING
   :class:`~finance.models.BankConnection` carrying a fresh ``state``.
2. The user approves at the bank, which redirects to the chosen redirect URL with
   ``?code=...&state=...``. The *client* catches that (this server has no callback route).
3. ``complete_link`` accepts the code only for a PENDING connection with that ``state`` in the
   caller's organization — the script's state-mismatch check, enforced server-side — exchanges
   it for a session and upserts the consented accounts.

Each consented account gets a syncer keyed by Enable Banking's cross-session
``identification_hash``, so relinking an expired consent re-attaches the same accounts with their
history, categories and notes — and an account known only from an import is adopted by its IBAN
(:mod:`finance.accounts`).
"""

import uuid
from datetime import datetime, timedelta, timezone

from channels.db import database_sync_to_async
from django.db import transaction as db_transaction
from django.utils import timezone as dj_timezone

from finance import models
from finance.accounts import attach_syncer
from finance.enablebanking.client import EnableBankingClient, EnableBankingConfig
from finance.taxonomy import seed_base_categories

PENDING_TTL = timedelta(minutes=30)


class LinkError(Exception):
    """The link cannot be started or completed as asked.

    ``code`` is the :class:`~finance.models.BankErrorCode` a client acts on; None marks invalid
    input (a validation error).
    """

    def __init__(self, message: str, code: "models.BankErrorCode | None" = models.BankErrorCode.INVALID_STATE) -> None:
        super().__init__(message)
        self.code = code


def pick_redirect(config: EnableBankingConfig, requested: str | None) -> str:
    """The requested redirect URL if it is registered, else the default; refuses anything else."""
    if not config.redirect_urls:
        raise LinkError("No redirect URL is configured for Enable Banking.", code=models.BankErrorCode.NOT_CONFIGURED)
    if requested is None:
        return config.redirect_urls[0]
    if requested not in config.redirect_urls:
        raise LinkError(f"Redirect URL {requested!r} is not registered; use one of {config.redirect_urls}.", code=None)
    return requested


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def start_link(organization, creator, aspsp_name: str, country: str, redirect_url: str | None, client: EnableBankingClient | None = None) -> tuple[models.BankConnection, str]:  # noqa: ANN001 - authentikate models
    """Start a consent; returns the PENDING connection and the bank login URL for the user."""
    config = client.config if client else EnableBankingConfig.from_settings()
    redirect = pick_redirect(config, redirect_url)
    state = uuid.uuid4().hex
    valid_until = (datetime.now(timezone.utc) + timedelta(days=config.consent_days)).isoformat()

    async def call(eb: EnableBankingClient) -> dict:
        return await eb.start_auth(aspsp_name=aspsp_name, country=country, state=state, redirect_url=redirect, valid_until=valid_until)

    if client is not None:
        auth = await call(client)
    else:
        async with EnableBankingClient(config) as eb:
            auth = await call(eb)

    connection = await models.BankConnection.objects.acreate(
        organization=organization,
        creator=creator,
        aspsp_name=aspsp_name,
        aspsp_country=country,
        state=state,
        redirect_url=redirect,
        pending_expires_at=dj_timezone.now() + PENDING_TTL,
        raw={"authorization_id": auth.get("authorization_id"), "auth_url": auth["url"]},
    )
    return connection, auth["url"]


def _claim_pending(organization_id: int, state: str) -> models.BankConnection:
    """Mark the organization's PENDING connection for ``state`` as being completed (one winner)."""
    with db_transaction.atomic():
        connection = (
            models.BankConnection.objects.select_for_update()
            .filter(organization_id=organization_id, state=state, status=models.ConnectionStatus.PENDING)
            .first()
        )
        if connection is None:
            raise LinkError("No pending bank link with this state in your organization: the URL belongs to another login attempt, or it was already used.")
        if connection.pending_expires_at < dj_timezone.now():
            connection.status = models.ConnectionStatus.FAILED
            connection.last_error = "The link was not completed in time."
            connection.last_error_code = models.BankErrorCode.CODE_EXPIRED
            connection.save(update_fields=["status", "last_error", "last_error_code"])
            raise LinkError("This bank link expired; start a new one.", code=models.BankErrorCode.CODE_EXPIRED)
        # Burn the state now, so a second completion with the same code cannot race this one.
        connection.status = models.ConnectionStatus.FAILED
        connection.last_error = "Completion in progress."
        connection.save(update_fields=["status", "last_error"])
        return connection


def account_identity(account: dict) -> str:
    """Stable identity of an account across sessions."""
    iban = (account.get("account_id") or {}).get("iban")
    return account.get("identification_hash") or (f"iban:{iban}" if iban else f"uid:{account['uid']}")


def _activate(connection_id: int, session: dict) -> models.BankConnection:
    now = dj_timezone.now()
    with db_transaction.atomic():
        connection = models.BankConnection.objects.select_for_update().get(id=connection_id)
        connection.status = models.ConnectionStatus.ACTIVE
        connection.session_id = session["session_id"]
        connection.valid_until = _parse_datetime((session.get("access") or {}).get("valid_until"))
        connection.linked_at = now
        connection.last_error = connection.last_error_code = None
        connection.raw = {**connection.raw, "session": session}
        connection.save()

        for account in session.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            attach_syncer(
                connection,
                account_identity(account)[:500],
                account["uid"],
                iban=(account.get("account_id") or {}).get("iban"),
                name=account.get("name") or account.get("details"),
                currency=account.get("currency") or "EUR",
                product=account.get("product"),
                raw=account,
            )
        if not models.Category.objects.filter(organization_id=connection.organization_id).exists():
            seed_base_categories(connection.organization_id)
    return connection


def _fail(connection_id: int, error: Exception) -> None:
    from finance.errors import code_for

    models.BankConnection.objects.filter(id=connection_id).update(status=models.ConnectionStatus.FAILED, last_error=str(error)[:2000], last_error_code=code_for(error))


async def complete_link(organization_id: int, code: str, state: str, client: EnableBankingClient | None = None) -> models.BankConnection:
    """Exchange the bank's redirect ``code`` for a session and store its accounts."""
    connection = await database_sync_to_async(_claim_pending)(organization_id, state)
    try:
        if client is not None:
            session = await client.create_session(code)
        else:
            async with EnableBankingClient() as eb:
                session = await eb.create_session(code)
    except Exception as error:
        await database_sync_to_async(_fail)(connection.id, error)
        raise
    return await database_sync_to_async(_activate)(connection.id, session)


async def revoke_link(connection: models.BankConnection, client: EnableBankingClient | None = None) -> models.BankConnection:
    """Withdraw the consent at the bank (best effort) and stop syncing its accounts. Data stays."""
    if connection.provider == models.Provider.SCALABLE:
        from finance.scalable.linking import revoke_scalable

        await revoke_scalable(connection)
    elif connection.session_id and connection.status == models.ConnectionStatus.ACTIVE:
        try:
            if client is not None:
                await client.delete_session(connection.session_id)
            else:
                async with EnableBankingClient() as eb:
                    await eb.delete_session(connection.session_id)
        except Exception as error:  # the consent may already be gone at the bank
            connection.last_error = str(error)[:2000]
    connection.status = models.ConnectionStatus.REVOKED
    await connection.asave(update_fields=["status", "last_error"])
    return connection
