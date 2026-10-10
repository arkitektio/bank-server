"""A Scalable connection's credentials, and the one safe way to get a fresh access token.

Scalable's access tokens live 20 minutes; the refresh token *rotates* — every refresh returns a
new one and kills the old. Two replicas refreshing with the same refresh token would at best
lose a token and at worst trip reuse detection, revoking the whole login. So a refresh first
takes the connection's ``token_lease_until`` in one conditional UPDATE (one winner, on any
number of replicas); everyone else waits for the winner's write and reads the new token. The
rotated token is written right after the refresh returns, with ``.update()`` so no history row
is recorded.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from channels.db import database_sync_to_async
from django.db.models import Q
from django.utils import timezone

from finance import crypto, models
from finance.scalable.client import ReloginRequired, ScalableClient, ScalableError
from finance.scalable.dpop import DpopKey

# A token with less than this left is refreshed rather than used.
MARGIN = timedelta(seconds=60)
LEASE = timedelta(seconds=60)
WAIT_SECONDS = 30.0


@dataclass
class Session:
    """What a GraphQL call needs."""

    access_token: str
    key: DpopKey
    person_id: str


def read_secret(connection: models.BankConnection) -> dict:
    if not connection.secret:
        raise ReloginRequired("This Scalable connection holds no credentials; link Scalable again.")
    return json.loads(crypto.decrypt(connection.secret))


def seal(secret: dict) -> str:
    return crypto.encrypt(json.dumps(secret))


def claim_value(token: str, name: str) -> str | None:
    """A (namespaced) claim of an access token. Not verified: it came straight from the issuer over TLS."""
    claims = jwt.decode(token, options={"verify_signature": False})
    return claims.get(f"https://de.scalable.capital/{name}") or claims.get(name)


def absorb(secret: dict, token: dict) -> tuple[dict, datetime, str | None]:
    """Fold a token response into the secret; returns (secret, access expiry, person id)."""
    access = token["access_token"]
    secret = {**secret, "access": access, "refresh": token.get("refresh_token") or secret.get("refresh")}
    secret["session_id"] = claim_value(access, "session_id") or secret.get("session_id")
    expires_in = int(token.get("expires_in") or 600)
    return secret, timezone.now() + timedelta(seconds=expires_in), claim_value(access, "person_id")


def _claim(connection_id: int) -> bool:
    now = timezone.now()
    return bool(
        models.BankConnection.objects.filter(id=connection_id)
        .filter(Q(token_lease_until__isnull=True) | Q(token_lease_until__lt=now))
        .update(token_lease_until=now + LEASE)
    )


def _release(connection_id: int) -> None:
    models.BankConnection.objects.filter(id=connection_id).update(token_lease_until=None)


def _store(connection_id: int, secret: dict, expires_at: datetime) -> None:
    models.BankConnection.objects.filter(id=connection_id).update(secret=seal(secret), token_expires_at=expires_at)


def _fresh(connection: models.BankConnection) -> Session | None:
    if connection.token_expires_at is None or connection.token_expires_at - MARGIN <= timezone.now():
        return None
    secret = read_secret(connection)
    return Session(secret["access"], DpopKey.from_pem(secret["dpop"]), connection.provider_user_id or "")


def _invalidate(connection_id: int, access_token: str) -> None:
    """Mark the access token stale, unless another process already replaced it."""
    connection = models.BankConnection.objects.get(id=connection_id)
    if connection.secret and read_secret(connection).get("access") == access_token:
        models.BankConnection.objects.filter(id=connection_id).update(token_expires_at=None)


async def refreshed_session(connection_id: int, client: ScalableClient, rejected: Session) -> Session:
    """A new session after GraphQL rejected ``rejected``'s access token (forces one refresh, still single-flight)."""
    await database_sync_to_async(_invalidate)(connection_id, rejected.access_token)
    return await session_for(connection_id, client)


async def session_for(connection_id: int, client: ScalableClient) -> Session:
    """A usable access token for the connection, refreshing (once, across replicas) if needed."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        connection = await models.BankConnection.objects.aget(id=connection_id)
        if connection.status != models.ConnectionStatus.ACTIVE:
            raise ReloginRequired(f"The Scalable connection is {connection.status.lower()}; link Scalable again.")
        session = _fresh(connection)
        if session is not None:
            return session
        if await database_sync_to_async(_claim)(connection_id):
            try:
                # Another replica may have refreshed between our read and our claim.
                connection = await models.BankConnection.objects.aget(id=connection_id)
                session = _fresh(connection)
                if session is not None:
                    return session
                secret = read_secret(connection)
                key = DpopKey.from_pem(secret["dpop"])
                token = await client.refresh(key, secret["refresh"], secret.get("session_id"))
                secret, expires_at, _ = absorb(secret, token)
                await database_sync_to_async(_store)(connection_id, secret, expires_at)
                return Session(secret["access"], key, connection.provider_user_id or "")
            finally:
                await database_sync_to_async(_release)(connection_id)
        if time.monotonic() > deadline:
            raise ScalableError("Another process is refreshing the Scalable session; try again shortly.")
        await asyncio.sleep(0.1)
