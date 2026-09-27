"""The sync the hub's rekuest schedules: every active syncer, unattended, within its budget.

Registered as the rekuest action ``sync_all_accounts`` (vendored ``rekuest_service``); rekuest's
manifest read gives it ``sync.scheduled_every_seconds`` as its default schedule. Nothing here
loops or waits — each run is one pass, started by rekuest, and a run lost to a crash is simply
followed by the next one.

A scheduled sync is an ordinary :func:`finance.sync.sync_syncer` without the user's PSU
headers: it takes the same lease (so it never collides with a user's sync on any replica) and
spends from the same daily budget. It leaves ``sync.scheduled_reserve`` syncs of that budget
untouched, so a user can still sync by hand the same day.
"""

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from finance import models
from finance.errors import SyncBudgetExhausted
from finance.sync import AlreadySyncing, after_sync, sync_budget, sync_syncer
from bank_server.service import service

logger = logging.getLogger(__name__)


def _due_syncers() -> list[int]:
    """Syncers with an active, unexpired connection whose budget allows a scheduled sync right now."""
    now = timezone.now()
    reserve = settings.BANK_SYNC.get("scheduled_reserve", 1)
    candidates = (
        models.AccountSyncer.objects.filter(connection__status=models.ConnectionStatus.ACTIVE)
        .filter(Q(connection__valid_until__isnull=True) | Q(connection__valid_until__gt=now))
        .order_by("id")
    )
    due = []
    for syncer in candidates:
        budget = sync_budget(syncer, now)
        if budget.next_allowed_at is not None:
            continue
        if budget.remaining_today is not None and budget.remaining_today <= reserve:
            continue
        due.append(syncer.id)
    return due


@service.action(
    interface="sync_all_accounts",
    name="Sync all bank accounts",
    description="Sync every active bank account that has sync budget to spare, unattended.",
    default_interval=settings.BANK_SYNC.get("scheduled_every_seconds"),
)
async def sync_all_accounts() -> dict:
    synced = skipped = failed = 0
    for syncer_id in await sync_to_async(_due_syncers)():
        try:
            result = await sync_syncer(syncer_id)
            await sync_to_async(after_sync)(result)
            synced += 1
        except (AlreadySyncing, SyncBudgetExhausted):
            skipped += 1  # a user's sync holds it, or the budget ran out meanwhile
        except Exception as error:  # recorded on the syncer by sync_syncer; one line here
            failed += 1
            logger.warning("Scheduled sync of syncer %s failed: %s", syncer_id, error)
    return {"synced": synced, "skipped": skipped, "failed": failed}


def _reembed() -> int:
    from embeddings import engine
    from embeddings.healer import reembed_all

    if not engine.enabled():
        return 0
    return reembed_all([models.Transaction, models.Category, models.CategoryTerm], max_batches=50)


def _reembed_interval() -> int | None:
    embeddings = getattr(settings, "EMBEDDINGS", {})
    return embeddings.get("SWEEP_INTERVAL") if embeddings.get("ENABLED", True) else None


@service.action(
    interface="reembed_stale",
    name="Re-embed stale rows",
    description="Embed transactions, categories and category terms whose vector is missing or came from another model (after a model change, or when the model was unavailable at write time).",
    default_interval=_reembed_interval(),
)
async def reembed_stale() -> dict:
    return {"reembedded": await sync_to_async(_reembed)()}
