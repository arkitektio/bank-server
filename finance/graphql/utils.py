"""Helpers every resolver uses: org-scoped fetches that fail as NOT_FOUND, and PSU headers."""

from typing import TypeVar

from django.db import models as django_models
from kante.errors import NotFound
from kante.types import Info

from finance.scoping import aget_for_org, for_org, get_for_org

M = TypeVar("M", bound=django_models.Model)


def get_or_404(model: type[M], info: Info, id: object) -> M:
    """``model`` ``id`` within the request's organization; another org's row is NOT_FOUND too."""
    try:
        return get_for_org(model, info, id=id)  # type: ignore[return-value]
    except (model.DoesNotExist, ValueError):  # type: ignore[attr-defined]
        raise NotFound(f"{model.__name__} {id} does not exist.")


async def aget_or_404(model: type[M], info: Info, id: object, **kwargs) -> M:
    """Async :func:`get_or_404`; ``kwargs`` narrow further (e.g. ``select_related``-free filters)."""
    try:
        return await aget_for_org(model, info, id=id, **kwargs)  # type: ignore[return-value]
    except (model.DoesNotExist, ValueError):  # type: ignore[attr-defined]
        raise NotFound(f"{model.__name__} {id} does not exist.")


def get_many(model: type[M], info: Info, ids: list | None) -> list[M]:
    """Several rows by id within the organization; any id not visible is NOT_FOUND."""
    if not ids:
        return []
    rows = list(for_org(model, info).filter(id__in=ids))
    if len(rows) != len(set(str(i) for i in ids)):
        raise NotFound(f"Some {model.__name__} ids do not exist.")
    return rows


def psu_headers(info: Info) -> dict[str, str]:
    """The user's IP and user agent, so the bank treats a sync as user-present (no PSD2 cap)."""
    headers = {k.lower(): v for k, v in (getattr(info.context, "headers", None) or {}).items()}
    out = {}
    ip = (headers.get("x-forwarded-for") or headers.get("x-real-ip") or "").split(",")[0].strip()
    if ip:
        out["Psu-Ip-Address"] = ip
    if headers.get("user-agent"):
        out["Psu-User-Agent"] = headers["user-agent"]
    return out
