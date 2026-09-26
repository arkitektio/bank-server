"""Linking Scalable Capital: the CLI's device login, driven step by step by the client.

1. ``start_scalable_link`` generates the connection's DPoP key, asks Scalable for a device code
   and stores a PENDING :class:`~finance.models.BankConnection` (``link_step=DEVICE``). The user
   opens ``verificationUriComplete`` and logs in there.
2. The client calls ``complete_scalable_link(state)`` until it returns ACTIVE. Each call does at
   most one step and never waits, so any replica can serve it:

   * ``DEVICE`` — poll the token endpoint once (respecting the interval Scalable asks for); on
     approval store the tokens and move on to ``MFA``.
   * ``MFA`` — if the account has 2FA-on-login, start the challenge (the user approves it on
     their trusted device) and check it on later calls.
   * then discover the broker portfolios and overnight savings accounts and activate.

   A step holds the connection's ``token_lease_until``, so two concurrent calls never both
   redeem the device code. Only a state from the caller's organization is accepted.

Accounts are keyed by ``scalable:<person>:...``, so relinking an expired login re-attaches the
same accounts with their history.
"""

import uuid
from datetime import timedelta

from channels.db import database_sync_to_async
from django.db import transaction as db_transaction
from django.utils import timezone

from finance import models
from finance.accounts import attach_syncer
from finance.linking import PENDING_TTL, LinkError
from finance.taxonomy import seed_base_categories
from finance.scalable import tokens
from finance.scalable.client import ScalableClient, ScalableConfig, ScalableError
from finance.scalable.dpop import DpopKey

INSTITUTION = "Scalable Capital"


async def start_scalable_link(organization, creator) -> tuple[models.BankConnection, dict]:  # noqa: ANN001 - authentikate models
    """Start a device login; returns the PENDING connection and Scalable's device-code answer."""
    config = ScalableConfig.from_settings()
    key = DpopKey.generate()
    async with ScalableClient(config) as client:
        device = await client.device_code(key)
    ttl = min(PENDING_TTL, timedelta(seconds=int(device.get("expires_in") or 900)))
    connection = await models.BankConnection.objects.acreate(
        organization=organization,
        creator=creator,
        provider=models.Provider.SCALABLE,
        aspsp_name=INSTITUTION,
        aspsp_country="DE",
        state=uuid.uuid4().hex,
        redirect_url="",
        pending_expires_at=timezone.now() + ttl,
        secret=tokens.seal({"dpop": key.to_pem(), "device_code": device["device_code"]}),
        link_step=models.LinkStep.DEVICE,
        link_poll_interval=int(device.get("interval") or 5),
        link_next_poll_at=timezone.now(),
        # Non-secret, what a resumed session shows again (see finance.auth_sessions).
        raw={"verification_uri": device.get("verification_uri"), "verification_uri_complete": device.get("verification_uri_complete"), "user_code": device.get("user_code")},
    )
    return connection, device


def _claim(organization_id: int, state: str) -> tuple[models.BankConnection, bool]:
    """The organization's Scalable link for ``state``, and whether this call may advance it."""
    now = timezone.now()
    connection = models.BankConnection.objects.filter(organization_id=organization_id, state=state, provider=models.Provider.SCALABLE).first()
    if connection is None:
        raise LinkError("No Scalable link with this state in your organization.")
    if connection.status == models.ConnectionStatus.ACTIVE:
        return connection, False
    if connection.status != models.ConnectionStatus.PENDING:
        # Repeat why it ended (a denied second factor stays MFA_REJECTED on every later call).
        raise LinkError(f"This Scalable link is {connection.status.lower()}: {connection.last_error or 'start a new one'}.", code=connection.last_error_code or models.BankErrorCode.INVALID_STATE)
    if connection.pending_expires_at < now:
        _fail(connection.id, "The login was not completed in time.", models.BankErrorCode.CODE_EXPIRED)
        raise LinkError("This Scalable link expired; start a new one.", code=models.BankErrorCode.CODE_EXPIRED)
    claimed = tokens._claim(connection.id)
    if claimed:
        connection.refresh_from_db()
    return connection, claimed


def _fail(connection_id: int, message: str, code: str | None) -> None:
    models.BankConnection.objects.filter(id=connection_id).update(
        status=models.ConnectionStatus.FAILED, last_error=message[:2000], last_error_code=code, secret=None, token_lease_until=None
    )


def _activate(connection_id: int, secret: dict, portfolios: list[str], savings: list[dict]) -> models.BankConnection:
    now = timezone.now()
    secret = {key: value for key, value in secret.items() if key != "device_code"}
    with db_transaction.atomic():
        connection = models.BankConnection.objects.select_for_update().get(id=connection_id)
        connection.status = models.ConnectionStatus.ACTIVE
        connection.link_step = models.LinkStep.DONE
        connection.linked_at = now
        connection.last_error = connection.last_error_code = None
        connection.secret = tokens.seal(secret)
        connection.save()
        person = connection.provider_user_id

        def upsert(identity: str, uid: str, name: str, kind: str, raw: dict) -> None:
            attach_syncer(connection, f"scalable:{person}:{identity}", uid, iban=None, name=name, currency="EUR", product=INSTITUTION, kind=kind, raw=raw)

        for portfolio in portfolios:
            upsert(f"broker:{portfolio}:cash", portfolio, "Scalable Broker (cash)", models.AccountKind.CASH, {"portfolio_id": portfolio})
            upsert(f"broker:{portfolio}:depot", portfolio, "Scalable Broker (depot)", models.AccountKind.DEPOT, {"portfolio_id": portfolio})
        for account in savings:
            name = (account.get("personalizations") or {}).get("name") or "Scalable Tagesgeld"
            upsert(f"savings:{account['id']}", account["id"], name, models.AccountKind.SAVINGS, {"savings_account_id": account["id"]})
        if not models.Category.objects.filter(organization_id=connection.organization_id).exists():
            seed_base_categories(connection.organization_id)
    return connection


def _update(connection_id: int, **fields) -> None:  # noqa: ANN003
    models.BankConnection.objects.filter(id=connection_id).update(**fields)


async def _step(connection: models.BankConnection, client: ScalableClient) -> models.BankConnection:
    """Advance the link by at most one waiting point."""
    secret = tokens.read_secret(connection)
    key = DpopKey.from_pem(secret["dpop"])
    now = timezone.now()

    if connection.link_step == models.LinkStep.DEVICE:
        if connection.link_next_poll_at and connection.link_next_poll_at > now:
            return connection
        poll = await client.poll_token(key, secret["device_code"])
        if poll.state == "pending":
            await database_sync_to_async(_update)(connection.id, link_next_poll_at=now + timedelta(seconds=connection.link_poll_interval))
            return connection
        if poll.state == "slow_down":
            interval = connection.link_poll_interval + 2
            await database_sync_to_async(_update)(connection.id, link_poll_interval=interval, link_next_poll_at=now + timedelta(seconds=interval))
            return connection
        secret, expires_at, person = tokens.absorb(secret, poll.token or {})
        if not person:
            raise ScalableError("Scalable's token carries no person id.")
        await database_sync_to_async(_update)(connection.id, secret=tokens.seal(secret), token_expires_at=expires_at, provider_user_id=person, link_step=models.LinkStep.MFA)
        connection.provider_user_id, connection.link_step = person, models.LinkStep.MFA

    person = connection.provider_user_id
    access = secret["access"]
    if connection.link_mfa_session_id is None:
        state = (await client.graphql(key, access, "Is2faOnLoginEnabled", {"input": {"userId": person}})).get("is2faOnLoginEnabled") or {}
        if state.get("enabled") and not state.get("hasApprovedSession"):
            started = await client.graphql(key, access, "Start2faOnLogin", {"input": {"userId": person, "deviceName": "CLI", "deviceType": "CLI"}})
            mfa = (started.get("start2faOnLogin") or {}).get("mfaSessionId")
            if not mfa:
                raise ScalableError("Scalable asked for a second factor but started no challenge.")
            await database_sync_to_async(_update)(connection.id, link_mfa_session_id=mfa)
            connection.link_mfa_session_id = mfa
            return connection
    else:
        result = await client.graphql(key, access, "Validate2faOnLogin", {"input": {"userId": person, "mfaSessionId": connection.link_mfa_session_id}})
        status = (result.get("validate2faOnLogin") or {}).get("status")
        if status == "PENDING":
            return connection
        if status != "SUCCESS":
            code = models.BankErrorCode.CODE_EXPIRED if status == "TIMEOUT_RETRY" else models.BankErrorCode.MFA_REJECTED
            raise ScalableError(f"The second-factor approval failed ({status}).", code=code)

    ids = await client.graphql(key, access, "ResolveBrokerIds", {"id": person})
    portfolios = [p["id"] for p in ((ids.get("account") or {}).get("brokerPortfolios") or [])]
    found = await client.graphql(key, access, "DiscoverOvernightAccounts", {"accountId": person})
    savings = [
        a for a in ((found.get("account") or {}).get("savingsAccounts") or [])
        if a.get("__typename") == "OvernightSavingsAccount" and a.get("state") == "ACTIVE"
    ]
    return await database_sync_to_async(_activate)(connection.id, secret, portfolios, savings)


async def complete_scalable_link(organization_id: int, state: str) -> models.BankConnection:
    """Advance the link one step; returns the connection (PENDING until the user approved everything)."""
    connection, claimed = await database_sync_to_async(_claim)(organization_id, state)
    if not claimed:
        return connection
    try:
        async with ScalableClient() as client:
            connection = await _step(connection, client)
    except ScalableError as error:  # a definite answer from Scalable: this login is over
        from finance.errors import code_for

        code = code_for(error)
        await database_sync_to_async(_fail)(connection.id, str(error), code)
        raise LinkError(str(error), code=code) from error
    finally:
        await database_sync_to_async(tokens._release)(connection.id)
    return await models.BankConnection.objects.aget(id=connection.id)


async def revoke_scalable(connection: models.BankConnection) -> None:
    """Log the connection out at Scalable (best effort) and forget its credentials."""
    try:
        secret = tokens.read_secret(connection)
        if secret.get("refresh"):
            async with ScalableClient() as client:
                await client.revoke(DpopKey.from_pem(secret["dpop"]), secret["refresh"])
    except Exception as error:  # the login may already be gone at Scalable
        connection.last_error = str(error)[:2000]
    await database_sync_to_async(_update)(connection.id, secret=None, token_expires_at=None)
