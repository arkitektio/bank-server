"""Linking a bank through Enable Banking: the consent flow, completed by the client.

1. ``start_link`` asks Enable Banking for the bank's login URL and stores a PENDING
   :class:`~finance.models.BankConnection` carrying a fresh ``state``.
2. The user approves at the bank, which redirects to the chosen redirect URL with
   ``?code=...&state=...``. The *client* catches that (this server has no callback route).
3. ``complete_link`` exchanges the code for a session and upserts the consented accounts. Whose
   login a ``state`` is (the member who started it, in their organization) is checked before, in
   :mod:`finance.auth_sessions`; here one completion wins the exchange and a second one is a no-op.

Each consented account gets a syncer keyed by Enable Banking's cross-session
``identification_hash``, so relinking an expired consent re-attaches the same accounts with their
history, categories and notes — and an account known only from an import is adopted by its IBAN
(:mod:`finance.accounts`).

Which application the calls are made as is the caller's: :mod:`finance.providers.enablebanking`
builds the client from the provider row.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from channels.db import database_sync_to_async
from django.db import transaction as db_transaction
from django.utils import timezone as dj_timezone

from finance import models
from finance.accounts import attach_syncer
from finance.enablebanking.client import EnableBankingClient, EnableBankingConfig
from finance.providers.errors import PENDING_TTL, LinkError, ProviderError
from finance.taxonomy import seed_base_categories

#: How long one completion may hold the login while it exchanges the code.
EXCHANGE_LEASE = timedelta(seconds=60)

if TYPE_CHECKING:
    from authentikate.models import Organization, User


def pick_redirect(config: EnableBankingConfig, requested: str | None) -> str:
    """The requested redirect URL if it is registered, else the default; refuses anything else."""
    if not config.redirect_urls:
        raise LinkError("This Enable Banking provider has no redirect URL.", code=models.BankErrorCode.NOT_CONFIGURED)
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


async def start_link(provider: models.BankProvider, client: EnableBankingClient, organization: "Organization", creator: "User", aspsp_name: str, country: str, redirect_url: str | None) -> models.BankConnection:
    """Start a consent; returns the PENDING connection (its ``raw`` holds the bank login URL)."""
    config = client.config
    redirect = pick_redirect(config, redirect_url)
    state = secrets.token_urlsafe(32)
    valid_until = (datetime.now(timezone.utc) + timedelta(days=config.consent_days)).isoformat()
    auth = await client.start_auth(aspsp_name=aspsp_name, country=country, state=state, redirect_url=redirect, valid_until=valid_until)
    return await models.BankConnection.objects.acreate(
        organization=organization,
        creator=creator,
        bank_provider=provider,
        provider=models.Provider.ENABLEBANKING,
        aspsp_name=aspsp_name,
        aspsp_country=country,
        state=state,
        redirect_url=redirect,
        pending_expires_at=dj_timezone.now() + PENDING_TTL,
        raw={"authorization_id": auth.get("authorization_id"), "auth_url": auth["url"]},
    )


def _claim_pending(connection_id: int) -> tuple[models.BankConnection, bool]:
    """The connection as it is now, and whether this call may exchange its code (one winner).

    The winner holds ``token_lease_until`` while it talks to Enable Banking; a second completion
    arriving meanwhile (the callback page and a pasted redirect) sees the login still PENDING.
    """
    now = dj_timezone.now()
    with db_transaction.atomic():
        connection = models.BankConnection.objects.select_for_update().get(id=connection_id)
        if connection.status != models.ConnectionStatus.PENDING:
            return connection, False
        if connection.pending_expires_at < now:
            connection.status = models.ConnectionStatus.FAILED
            connection.last_error = "The login was not completed in time."
            connection.last_error_code = models.BankErrorCode.CODE_EXPIRED
            connection.save(update_fields=["status", "last_error", "last_error_code"])
            return connection, False
        if connection.token_lease_until and connection.token_lease_until > now:
            return connection, False
        connection.token_lease_until = now + EXCHANGE_LEASE
        connection.save(update_fields=["token_lease_until"])
        return connection, True


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
        connection.token_lease_until = None
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


def _fail(connection_id: int, error: Exception) -> models.BankConnection:
    from finance.errors import code_for

    models.BankConnection.objects.filter(id=connection_id).update(status=models.ConnectionStatus.FAILED, last_error=str(error)[:2000], last_error_code=code_for(error), token_lease_until=None)
    return models.BankConnection.objects.get(id=connection_id)


def _release(connection_id: int) -> None:
    models.BankConnection.objects.filter(id=connection_id).update(token_lease_until=None)


async def complete_link(client: EnableBankingClient, connection: models.BankConnection, code: str) -> models.BankConnection:
    """Exchange the bank's redirect ``code`` for a session and store its accounts; returns the connection as it is afterwards.

    A settled login is returned as it is (nothing is exchanged twice). A code Enable Banking
    refuses ends the login (FAILED); not getting an answer about it leaves it PENDING and raises.
    """
    connection, claimed = await database_sync_to_async(_claim_pending)(connection.id)
    if not claimed:
        return connection
    try:
        session = await client.create_session(code)
    except ProviderError as error:
        if error.code in (models.BankErrorCode.BANK_UNAVAILABLE, models.BankErrorCode.RATE_LIMITED):  # the code may still be good
            await database_sync_to_async(_release)(connection.id)
            raise
        failed: models.BankConnection = await database_sync_to_async(_fail)(connection.id, error)
        return failed
    except Exception:
        await database_sync_to_async(_release)(connection.id)
        raise
    linked: models.BankConnection = await database_sync_to_async(_activate)(connection.id, session)
    return linked


async def revoke(client: EnableBankingClient, connection: models.BankConnection) -> None:
    """Withdraw the consent at the bank, best effort (it may already be gone there)."""
    if connection.session_id and connection.status == models.ConnectionStatus.ACTIVE:
        try:
            await client.delete_session(connection.session_id)
        except Exception as error:
            connection.last_error = str(error)[:2000]
