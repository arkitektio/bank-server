"""An async client for Scalable Capital's official CLI API.

A port of the HTTP layer of https://github.com/ScalableCapital/scalable-cli: OAuth 2.0 device
authorization at ``secure.scalable.capital`` (scope ``offline_access``, so there is a refresh
token) and one GraphQL endpoint. Every request — token endpoint and GraphQL alike — carries a
DPoP proof signed with the connection's key (:mod:`finance.scalable.dpop`); the tokens are
bound to that key. A server may answer a proof with a ``DPoP-Nonce`` challenge; like the CLI we
retry exactly once with the nonce.

Only the operations in :data:`finance.scalable.queries.OPERATIONS` can be sent.
"""

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
from django.conf import settings

from finance.models import BankErrorCode
from finance.providers.errors import ProviderError
from finance.scalable.dpop import DpopKey
from finance.scalable.queries import OPERATIONS

logger = logging.getLogger(__name__)

SCOPE = "offline_access openid email"


class ScalableError(ProviderError):
    """Scalable refused a request. ``code`` is given when the answer means something specific to a client."""

    def __init__(self, message: str, status: int = 0, code: BankErrorCode | None = None) -> None:
        super().__init__(message)
        self.status = status
        if code is not None:
            self.explicit_code = code


class ReloginRequired(ScalableError):
    """The refresh token is gone (expired, revoked or reused); the user must link again."""

    explicit_code = BankErrorCode.CONSENT_EXPIRED


class Unauthorized(ScalableError):
    """GraphQL rejected the access token; refresh once and retry (only a failed refresh means relogin)."""


class ScalableRateLimited(ScalableError):
    """Scalable throttled us; ``retry_after`` seconds, when it said."""

    explicit_code = BankErrorCode.RATE_LIMITED

    def __init__(self, message: str, status: int = 429, retry_after: int | None = None) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


@dataclass
class ScalableConfig:
    """What the client needs; built from ``settings.SCALABLE``. Defaults are the CLI's production channel."""

    issuer: str = "https://secure.scalable.capital"
    audience: str = "https://de.scalable.capital/api-gateway"
    client_id: str = "yBM3BrpRgwSTJZRdJllvtD6jJEmyxWfE"
    graphql_url: str = "https://de.scalable.capital/api/cli/graphql"
    user_agent: str = "arkitekt-bank"
    timeout_seconds: float = 30

    @classmethod
    def from_settings(cls) -> "ScalableConfig":
        """The deployment's Scalable endpoints (the CLI's production channel unless the config says otherwise)."""
        conf: dict[str, Any] = getattr(settings, "SCALABLE", None) or {}
        return cls(**conf)


@dataclass
class DevicePoll:
    """One poll of the device authorization: ``pending``, ``slow_down`` or ``ok`` (with ``token``)."""

    state: str
    token: dict | None = None


class ScalableClient:
    """One client per operation; open it with ``async with``."""

    def __init__(self, config: ScalableConfig | None = None) -> None:  # None: the deployment's endpoints
        self.config = config or ScalableConfig.from_settings()
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "ScalableClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds), headers={"User-Agent": self.config.user_agent})
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, url: str, key: DpopKey, *, form: dict | None = None, json: dict | None = None, access_token: str | None = None) -> tuple[int, Any]:
        """POST with a DPoP proof; answers one nonce challenge. Returns (status, parsed body)."""
        assert self._session is not None, "use `async with ScalableClient() as client`"
        nonce = None
        for attempt in range(2):
            headers = {"DPoP": key.proof("POST", url, nonce=nonce, access_token=access_token)}
            if access_token:
                headers["Authorization"] = f"DPoP {access_token}"
            async with self._session.post(url, data=form, json=json, headers=headers) as response:
                status = response.status
                challenge = response.headers.get("DPoP-Nonce")
                retry_after = response.headers.get("Retry-After")
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = {"error": "invalid_response", "error_description": (await response.text())[:500]}
            if status < 400:
                return status, body
            if status == 429:
                seconds = int(retry_after) if retry_after and retry_after.isdigit() else None
                raise ScalableRateLimited(f"Scalable is rate limiting requests (retry after {retry_after or '?'}s).", status, seconds)
            # The CLI's rule: a nonce header plus a 401 (resource servers signal ``use_dpop_nonce`` in
            # WWW-Authenticate) or an OAuth nonce/proof error in the body.
            text = str(body).lower()
            if attempt == 0 and challenge and (status == 401 or "use_dpop_nonce" in text or "invalid_dpop_proof" in text):
                nonce = challenge
                continue
            return status, body
        raise AssertionError("unreachable")

    def _oauth_error(self, action: str, status: int, body: Any) -> ScalableError:
        error = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
        # Never echo error_description: it can carry token material.
        if error in ("invalid_grant", "expired_token"):
            return ReloginRequired(f"{action} failed ({error}); link Scalable again.", status)
        return ScalableError(f"{action} failed (HTTP {status}, {error}).", status)

    async def device_code(self, key: DpopKey) -> dict:
        """Start a device authorization: ``user_code``, ``verification_uri(_complete)``, ``device_code``, ``interval``, ``expires_in``."""
        status, body = await self._post(
            f"{self.config.issuer}/oauth/device/code",
            key,
            form={"client_id": self.config.client_id, "audience": self.config.audience, "scope": SCOPE},
        )
        if status >= 400:
            raise self._oauth_error("Starting the Scalable login", status, body)
        return body

    async def poll_token(self, key: DpopKey, device_code: str) -> DevicePoll:
        """Ask once whether the user approved the device code."""
        status, body = await self._post(
            f"{self.config.issuer}/oauth/token",
            key,
            form={"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device_code, "client_id": self.config.client_id},
        )
        if status < 400:
            return DevicePoll("ok", body)
        error = body.get("error") if isinstance(body, dict) else None
        if error == "authorization_pending":
            return DevicePoll("pending")
        if error == "slow_down":
            return DevicePoll("slow_down")
        if error == "access_denied":
            raise ScalableError("The Scalable login was denied.", status, code=BankErrorCode.MFA_REJECTED)
        if error == "expired_token":
            raise ScalableError("The Scalable login code expired; start a new link.", status, code=BankErrorCode.CODE_EXPIRED)
        raise self._oauth_error("Completing the Scalable login", status, body)

    async def refresh(self, key: DpopKey, refresh_token: str, session_id: str | None) -> dict:
        """Trade the refresh token for new tokens. Scalable rotates it: the old one is dead after this."""
        form = {"grant_type": "refresh_token", "client_id": self.config.client_id, "refresh_token": refresh_token}
        if session_id:
            form["session_id"] = session_id
        status, body = await self._post(f"{self.config.issuer}/oauth/token", key, form=form)
        if status >= 400:
            raise self._oauth_error("Refreshing the Scalable session", status, body)
        if not body.get("refresh_token"):
            raise ReloginRequired("Scalable returned no replacement refresh token; link Scalable again.", status)
        return body

    async def revoke(self, key: DpopKey, refresh_token: str) -> None:
        """Revoke the refresh token (logout)."""
        status, body = await self._post(f"{self.config.issuer}/oauth/revoke", key, form={"token": refresh_token, "client_id": self.config.client_id})
        if status >= 400:
            raise self._oauth_error("Revoking the Scalable session", status, body)

    async def graphql(self, key: DpopKey, access_token: str, operation: str, variables: dict) -> dict:
        """Run one allow-listed operation; returns its ``data``."""
        query = OPERATIONS.get(operation)
        if query is None:
            raise ValueError(f"Operation {operation!r} is not allowed.")
        status, body = await self._post(
            self.config.graphql_url,
            key,
            json={"operationName": operation, "query": query, "variables": variables},
            access_token=access_token,
        )
        if status == 401:
            raise Unauthorized(f"Scalable rejected the access token for {operation}.", status)
        if status >= 400:
            raise ScalableError(f"Scalable {operation} failed (HTTP {status}).", status)
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            messages = "; ".join(str(e.get("message", e))[:200] for e in errors[:3])
            raise ScalableError(f"Scalable {operation} returned errors: {messages}", status)
        return (body or {}).get("data") or {}
