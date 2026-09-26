"""The HookAgent signature, both directions — must match rekuest's ``facade/hooks.py`` exactly.

``X-Rekuest-Signature-V1: t=<unix seconds>,v1=<hex>`` over ``v1:{agent_id}:{t}:`` + body, keyed
by the shared secret. The agent id comes in ``X-Rekuest-Agent``: it is part of the signed
payload, so a forged id fails verification like a forged body does.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_V1_HEADER = "X-Rekuest-Signature-V1"
AGENT_HEADER = "X-Rekuest-Agent"


def _payload(agent_id: str, timestamp: int, body: bytes) -> bytes:
    return f"v1:{agent_id}:{timestamp}:".encode() + body


def sign(secret: str, agent_id: str, body: bytes, timestamp: int | None = None) -> str:
    """The ``t=…,v1=…`` header value for ``body`` sent as (or to) ``agent_id``."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode("utf-8"), _payload(agent_id, timestamp, body), hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(secret: str, agent_id: str, body: bytes, header: str | None, max_skew: int) -> bool:
    """Whether ``header`` is a fresh, valid V1 signature of ``body`` for ``agent_id``."""
    if not secret or not agent_id or not header:
        return False
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    try:
        timestamp = int(parts["t"])
        digest = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(int(time.time()) - timestamp) > max_skew:
        return False
    expected = hmac.new(secret.encode("utf-8"), _payload(agent_id, timestamp, body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)
