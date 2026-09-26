"""An async client for the Enable Banking API.

The port of the ``transactions.py`` script's HTTP calls: an RS256 app JWT signed with the
application's private key (``kid`` = app id), ``/auth`` → ``/sessions`` for the consent, and
paged ``/accounts/{uid}/transactions``. Async (aiohttp) so a sync never blocks the event loop
the GraphQL server runs on.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import jwt
from django.conf import settings

logger = logging.getLogger(__name__)

# A token is re-signed this long before it would expire.
_TOKEN_TTL = 3600
_TOKEN_MARGIN = 300


class EnableBankingError(Exception):
    """Enable Banking refused a request."""

    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"Enable Banking {path} failed ({status}): {body[:500]}")
        self.status = status
        self.body = body
        self.path = path


class ConsentExpired(EnableBankingError):
    """The session's consent is gone (expired, withdrawn, or unknown); the user must relink."""


class RateLimited(EnableBankingError):
    """The bank or Enable Banking throttled us (PSD2 access limits); try later."""


class NotConfigured(Exception):
    """This deployment has no Enable Banking credentials."""

    def __init__(self) -> None:
        super().__init__("Enable Banking is not configured on this server (no `enablebanking` block in its config).")


@dataclass
class EnableBankingConfig:
    """What the client needs; built from ``settings.ENABLEBANKING``."""

    app_id: str
    private_key_path: str
    api_url: str = "https://api.enablebanking.com"
    redirect_urls: list[str] = field(default_factory=list)
    consent_days: int = 90
    psu_type: str = "personal"
    timeout_seconds: float = 60
    daily_sync_limit: int | None = 4

    @classmethod
    def from_settings(cls) -> "EnableBankingConfig":
        """The deployment's configuration; raises :class:`NotConfigured` when there is none."""
        conf = getattr(settings, "ENABLEBANKING", None)
        if not conf:
            raise NotConfigured()
        return cls(**conf)


_EXPIRED_MARKERS = ("EXPIRED", "SESSION_DOES_NOT_EXIST", "CLOSED_SESSION", "REVOKED", "ACCESS_DENIED")


class EnableBankingClient:
    """One client per operation; open it with ``async with``."""

    def __init__(self, config: EnableBankingConfig | None = None) -> None:
        self.config = config or EnableBankingConfig.from_settings()
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_exp = 0.0

    async def __aenter__(self) -> "EnableBankingClient":
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _auth_header(self) -> dict[str, str]:
        now = time.time()
        if self._token is None or now > self._token_exp - _TOKEN_MARGIN:
            iat = int(now)
            self._token = jwt.encode(
                {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": iat, "exp": iat + _TOKEN_TTL},
                Path(self.config.private_key_path).read_bytes(),
                algorithm="RS256",
                headers={"kid": self.config.app_id},
            )
            self._token_exp = iat + _TOKEN_TTL
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None, psu_headers: dict[str, str] | None = None) -> Any:
        assert self._session is not None, "use `async with EnableBankingClient() as client`"
        headers = {**self._auth_header(), **(psu_headers or {})}
        url = self.config.api_url.rstrip("/") + path
        async with self._session.request(method, url, json=json, params=params, headers=headers) as response:
            body = await response.text()
            if response.status < 400:
                return await response.json(content_type=None) if body else None
        if response.status == 429:
            raise RateLimited(response.status, body, path)
        if response.status in (401, 403, 404, 410, 422) and any(marker in body.upper() for marker in _EXPIRED_MARKERS):
            raise ConsentExpired(response.status, body, path)
        raise EnableBankingError(response.status, body, path)

    async def aspsps(self, country: str | None = None) -> list[dict]:
        """The banks (ASPSPs) Enable Banking can reach, optionally in one country."""
        data = await self._request("GET", "/aspsps", params={"country": country} if country else None)
        return data.get("aspsps", [])

    async def start_auth(self, *, aspsp_name: str, country: str, state: str, redirect_url: str, valid_until: str) -> dict:
        """Start a consent; returns ``{"url": <bank login>, "authorization_id": ...}``."""
        return await self._request(
            "POST",
            "/auth",
            json={
                "access": {"valid_until": valid_until},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": self.config.psu_type,
            },
        )

    async def create_session(self, code: str) -> dict:
        """Exchange the redirect's ``code`` for a session holding the consented accounts."""
        return await self._request("POST", "/sessions", json={"code": code})

    async def get_session(self, session_id: str) -> dict:
        """The session's current state (``status`` is ``AUTHORIZED`` while usable)."""
        return await self._request("GET", f"/sessions/{session_id}")

    async def delete_session(self, session_id: str) -> None:
        """Withdraw the consent at the bank."""
        await self._request("DELETE", f"/sessions/{session_id}")

    async def transactions(self, account_uid: str, since: date | None, psu_headers: dict[str, str] | None = None) -> list[dict]:
        """All transactions since a date, or as far back as the bank allows if ``since`` is None."""
        base: dict[str, str] = {"date_from": since.isoformat()} if since else {"strategy": "longest"}
        out: list[dict] = []
        params = base
        while True:
            data = await self._request("GET", f"/accounts/{account_uid}/transactions", params=params, psu_headers=psu_headers)
            out.extend(data.get("transactions", []))
            key = data.get("continuation_key")
            if not key:
                return out
            params = {**base, "continuation_key": key}

    async def balances(self, account_uid: str, psu_headers: dict[str, str] | None = None) -> list[dict]:
        """The account's current balances, one per balance type."""
        data = await self._request("GET", f"/accounts/{account_uid}/balances", psu_headers=psu_headers)
        return data.get("balances", [])
