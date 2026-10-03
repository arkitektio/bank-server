"""The actions of bank's hook agent: work the hub's rekuest can ask for, per organization.

``sync_all_accounts`` syncs an organization's active syncers, unattended, within their budget;
``reembed_stale`` re-embeds its stale rows. Both are registered on the agent declared in
``bank_server.hook_agent`` (vendored ``rekuest_hook``). Every organization has the agent, so a
run is handed its organization's slug and does that organization's share of the work, nothing
else. The actions are only offered: nothing here schedules them, that is the organization's own
automation. Nothing here loops or waits — each run is one pass, started by rekuest, and a run
lost to a crash is simply followed by the next one.

An unattended sync is an ordinary :func:`finance.sync.sync_syncer` without the user's PSU
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
from bank_server.hook_agent import agent

logger = logging.getLogger(__name__)


def _due_syncers(organization: str) -> list[int]:
    """The organization's syncers with an active, unexpired connection whose budget allows a scheduled sync right now."""
    now = timezone.now()
    reserve = settings.BANK_SYNC.get("scheduled_reserve", 1)
    candidates = (
        models.AccountSyncer.objects.filter(organization__slug=organization, connection__status=models.ConnectionStatus.ACTIVE)
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


@agent.action(
    interface="sync_all_accounts",
    name="Sync all bank accounts",
    description="Sync every active bank account of the organization that has sync budget to spare, unattended.",
)
async def sync_all_accounts(organization: str) -> dict:
    synced = skipped = failed = 0
    for syncer_id in await sync_to_async(_due_syncers)(organization):
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


def _reembed(organization: str) -> int:
    from embeddings import engine
    from embeddings.healer import reembed_all

    if not engine.enabled():
        return 0
    return reembed_all([models.Transaction, models.Category, models.CategoryTerm], max_batches=50, organization=organization)


@agent.action(
    interface="reembed_stale",
    name="Re-embed stale rows",
    description="Embed the organization's transactions, categories and category terms whose vector is missing or came from another model (after a model change, or when the model was unavailable at write time).",
)
async def reembed_stale(organization: str) -> dict:
    return {"reembedded": await sync_to_async(_reembed)(organization)}
