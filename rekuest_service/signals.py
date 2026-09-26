"""Signals on the wire — the service side of a rekuest Signal.

Declare and emit through a :class:`rekuest_service.Service`::

    dataset_created = service.signal("@mikro/arraydataset", kinds=["CREATED"], descriptors=[...])
    dataset_created.emit(dataset.pk, organization=dataset.organization.slug, descriptors={...})

rekuest matches the signal against its users' triggers and runs their actions on the object.
When this runs inside a request that carried a provenance token (the service was called in a
rekuest task), that token goes along untouched; rekuest verifies it against its own key and
makes the triggered runs children of that task. Nothing else is trusted: a service cannot name
a causing task it was not called in.

Best-effort, on purpose: the POST happens after the surrounding transaction commits (a rolled
back object is never announced), in a thread, and a failure is one warning line. Nothing here
retries, queues or loops. The module-level :func:`emit` sends through the default service.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from rekuest_service import signing

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def current_provenance_token() -> str | None:
    """The raw provenance token of the request being served, if koherent holds one."""
    try:
        from koherent.vars import get_current_provenance
    except ImportError:
        return None
    provenance = get_current_provenance()
    return getattr(provenance, "raw", None) if provenance is not None else None


def emit(kind: str, identifier: str, object: Any, *, organization: str, descriptors: dict[str, Any] | None = None) -> None:
    """Announce ``kind`` of ``identifier:object`` through the default service (kept for existing callers)."""
    from rekuest_service.service import KINDS, default_service

    if kind not in KINDS:
        raise ValueError(f"A signal kind is one of {KINDS}, not {kind!r}")
    handle = default_service.signals.get(identifier)
    if handle is not None and kind in handle.declaration.kinds:
        handle.emit(object, organization=organization, descriptors=descriptors, kind=kind)
    else:
        default_service._emit(kind, identifier, object, organization=organization, descriptors=descriptors)


def send(config: dict[str, Any], service: str, message: dict[str, Any]) -> bool:
    """POST one signal, signed for ``signal:<service>``. Never raises."""
    body = json.dumps(message).encode("utf-8")
    url = f"{config['REKUEST_URL'].rstrip('/')}/agi/signal/{service}"
    headers = {"Content-Type": "application/json", signing.SIGNATURE_V1_HEADER: signing.sign(config["SECRET"], f"signal:{service}", body)}
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=_TIMEOUT)
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001  never raises: a lost delivery is rekuest's to time out
        logger.warning("Could not signal %s %s:%s to rekuest: %s", message["kind"], message["identifier"], message["object"], error)
        return False
