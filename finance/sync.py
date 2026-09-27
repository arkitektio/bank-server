"""Pulling an account from its providers into the database.

An account is fed by its :class:`~finance.models.AccountSyncer` rows — one per provider identity
(an Enable Banking account, a Scalable pot). Syncing an account syncs each of its live syncers;
the lease, the daily budget and the last outcome are per syncer.

The script this grew from deleted the fetched date range and re-inserted it. That would wipe
every category and note a user set, so a sync here *upserts* instead:

* **Booked** rows are keyed by a fingerprint — the bank's ``entry_reference`` when it sends
  one, else a hash of (date, amount, currency, counterparty, IBAN, remittance). Identical
  transactions are genuine (the same coffee twice a day), so each repeat of a hash within a
  fetch gets an occurrence suffix (``#1``, ``#2``). A fetch always covers whole days, so the
  n-th repeat on a day keeps its suffix across syncs. Re-syncing updates only bank fields;
  ``category``, ``note`` and ``is_transfer`` are never touched.
Scalable accounts take their own path (:mod:`finance.scalable.sync`): their transactions carry
stable ids, so every row — pending included — is upserted by id.

* **Pending** rows change their content when they book (amount, date, sometimes the
  counterparty), so no fingerprint survives the transition. They are dropped and re-inserted on
  every sync; *annotations on pending rows are lost* once the transaction books.

Concurrency: a sync first claims the syncer with a lease (``sync_lease_until``) in one
conditional UPDATE — exactly one caller wins, on any number of replicas. The bank is fetched
outside any DB transaction; the write happens in one atomic block under the account's row lock
(which also serializes it against imports into the same account). A crashed sync frees the
syncer when its lease runs out.
"""

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Max, Min, Q
from django.db.models.functions import Coalesce
from django.utils import timezone as dj_timezone

from finance import models
from finance.enablebanking.client import ConsentExpired, EnableBankingClient
from embeddings.models import EMBEDDING_FIELDS
from finance.errors import SyncBudgetExhausted
from finance.semantic import auto_assign, embed_rows
from finance.scalable.client import ScalableClient

logger = logging.getLogger(__name__)

BANK_FIELDS = [
    "booking_date",
    "value_date",
    "transaction_date",
    "amount",
    "currency",
    "status",
    "counterparty",
    "counterparty_iban",
    "remittance",
    "entry_reference",
    "raw",
]


class AlreadySyncing(Exception):
    """Another sync holds this account right now."""


class ConnectionInactive(Exception):
    """The account's connection is revoked, failed or still pending; nothing to sync through."""


@dataclass
class SyncResult:
    """What one account sync did."""

    account_id: int
    created: int = 0
    updated: int = 0
    pending_replaced: int = 0
    balances: int = 0
    categorized: int = 0
    holdings: int = 0


def sync_settings() -> dict:
    """The ``sync`` config block."""
    return settings.BANK_SYNC


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def normalize(tx: dict) -> dict:
    """An Enable Banking transaction as model fields (signed amount, counterparty by direction)."""
    amount = Decimal(str(tx["transaction_amount"]["amount"]))
    debit = tx.get("credit_debit_indicator") == "DBIT"
    party = (tx.get("creditor") if debit else tx.get("debtor")) or {}
    party_account = (tx.get("creditor_account") if debit else tx.get("debtor_account")) or {}
    status = tx.get("status") or "BOOK"
    return {
        "booking_date": _parse_date(tx.get("booking_date")),
        "value_date": _parse_date(tx.get("value_date")),
        "transaction_date": _parse_date(tx.get("transaction_date")),
        "amount": -abs(amount) if debit else abs(amount),
        "currency": tx["transaction_amount"]["currency"],
        "status": status if status in models.TransactionStatus.values else models.TransactionStatus.OTHER,
        "counterparty": party.get("name") or None,
        "counterparty_iban": party_account.get("iban") or None,
        "remittance": " ".join(tx.get("remittance_information") or []) or None,
        "entry_reference": tx.get("entry_reference") or None,
        "raw": tx,
    }


def fingerprint_all(rows: list[dict]) -> list[str]:
    """One fingerprint per row, in order; repeats of a key within the batch get ``#n``."""
    seen: Counter[str] = Counter()
    out = []
    for row in rows:
        if row["entry_reference"]:
            base = f"ref:{row['entry_reference']}"
        else:
            day = row["booking_date"] or row["value_date"] or row["transaction_date"]
            content = "|".join(
                str(part or "")
                for part in (row["status"], day, row["amount"], row["currency"], row["counterparty"], row["counterparty_iban"], row["remittance"])
            )
            base = "h:" + hashlib.sha256(content.encode()).hexdigest()[:40]
        n = seen[base]
        seen[base] += 1
        out.append(base if n == 0 else f"{base}#{n}")
    return out


def claim(syncer_id: int, lease_seconds: int) -> bool:
    """Take the syncer's lease; False if another sync holds it."""
    now = dj_timezone.now()
    return bool(
        models.AccountSyncer.objects.filter(id=syncer_id)
        .filter(Q(sync_lease_until__isnull=True) | Q(sync_lease_until__lt=now))
        .update(sync_lease_until=now + timedelta(seconds=lease_seconds))
    )


def release(syncer_id: int) -> None:
    """Give the lease back."""
    models.AccountSyncer.objects.filter(id=syncer_id).update(sync_lease_until=None)


def fetch_since(syncer: models.AccountSyncer, overlap_days: int) -> date | None:
    """Where an incremental fetch starts; None (everything) on the syncer's first sync.

    Only the syncer's own rows count: imported history on the same account never moves the
    window, so the first sync after an import still fetches everything the provider has.
    """
    newest = syncer.transactions.filter(status=models.TransactionStatus.BOOKED).aggregate(newest=Max("booking_date"))["newest"]
    return newest - timedelta(days=overlap_days) if newest else None


def daily_limit(syncer: models.AccountSyncer) -> int | None:
    """Syncs per UTC day the syncer's provider allows (None: unlimited)."""
    conf = settings.SCALABLE if syncer.backend == models.Provider.SCALABLE else settings.ENABLEBANKING
    return (conf or {}).get("daily_sync_limit")


def _tomorrow(now: datetime) -> datetime:
    today = now.astimezone(timezone.utc).date()
    return datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)


@dataclass
class SyncBudget:
    """How many syncs the syncer has left today, and the earliest a sync is allowed (None: now)."""

    remaining_today: int | None
    next_allowed_at: datetime | None


def sync_budget(syncer: models.AccountSyncer, now: datetime | None = None) -> SyncBudget:
    """Computed on read from the stored counter, the provider's limit and any rate-limit answer."""
    now = now or dj_timezone.now()
    limit = daily_limit(syncer)
    used = syncer.syncs_today if syncer.sync_day == now.astimezone(timezone.utc).date() else 0
    remaining = None if limit is None else max(0, limit - used)
    allowed_at = syncer.rate_limited_until if syncer.rate_limited_until and syncer.rate_limited_until > now else None
    if remaining == 0:
        allowed_at = max(allowed_at or now, _tomorrow(now))
    return SyncBudget(remaining, allowed_at)


def spend(syncer_id: int) -> None:
    """Count one sync that reaches the provider, refusing (without contacting it) when none is left.

    The check and the count are one conditional UPDATE under the syncer's row lock, so two
    requests racing for the last sync of the day cannot both get it.
    """
    now = dj_timezone.now()
    today = now.astimezone(timezone.utc).date()
    with db_transaction.atomic():
        syncer = models.AccountSyncer.objects.select_for_update().get(id=syncer_id)
        budget = sync_budget(syncer, now)
        if budget.next_allowed_at is not None:
            raise SyncBudgetExhausted(budget.next_allowed_at)
        syncer.syncs_today = (syncer.syncs_today if syncer.sync_day == today else 0) + 1
        syncer.sync_day = today
        syncer.save(update_fields=["syncs_today", "sync_day"])


def mark_synced(syncer: models.AccountSyncer, now: datetime) -> None:
    """Record a successful sync on the syncer (inside the persisting transaction)."""
    models.AccountSyncer.objects.filter(id=syncer.id).update(last_synced_at=now, last_error=None, last_error_code=None)


def take_over_imported(account: models.BankAccount, new: list[tuple[str, dict]]) -> dict[str, models.Transaction]:
    """The account's IMPORT rows that new booked synced rows describe, by the synced row's fingerprint.

    The caller writes the synced bank fields into those rows (keeping their category, note,
    transfer flag and import data) instead of creating a second row for the same booking. Runs
    under the account's row lock (see :mod:`finance.matching`).
    """
    from finance.matching import pair, pool_for

    booked = [(fp, row) for fp, row in new if row["status"] == models.TransactionStatus.BOOKED]
    if not booked:
        return {}
    pool = list(pool_for(account, models.TransactionOrigin.IMPORT, [booking_day(row) for _, row in booked]))
    if not pool:
        return {}
    pairs = pair([row for _, row in booked], pool)
    return {booked[index][0]: tx for index, tx in pairs.items()}


def booking_day(row: dict) -> date | None:
    """The day a normalized row was booked (else took effect, else was made)."""
    return row["booking_date"] or row["value_date"] or row["transaction_date"]


def categorize_rows(organization_id: int, touched_ids: list[int]) -> int:
    """Categorize freshly written rows — rules, then confident suggestions; returns how many changed.

    The one step every writer runs after embedding its rows (Enable Banking and Scalable syncs, and
    statement imports), inside its atomic block. Manual and imported categories are never touched.
    """
    from finance.merchants import categorize_by_merchant, match, refresh_context
    from finance.rules import apply_rules

    rows = models.Transaction.objects.filter(id__in=touched_ids)
    match(organization_id, touched_ids)  # merchants first: a merchant's category needs its merchant
    refresh_context(organization_id, touched_ids)  # …and the merchant/place words join the embedding before suggestions read it
    categorized = apply_rules(organization_id, rows)
    categorized += categorize_by_merchant(organization_id, touched_ids)  # after rules, before suggestions
    categorized += auto_assign(organization_id, touched_ids)
    return categorized


def persist(syncer_id: int, raw_transactions: list[dict], raw_balances: list[dict], since: date | None = None) -> SyncResult:
    """Write one Enable Banking fetch (of everything from ``since`` on) in a single transaction."""
    from finance.transfers import detect_transfers

    syncer = models.AccountSyncer.objects.get(id=syncer_id)
    result = SyncResult(account_id=syncer.account_id)
    rows = [normalize(tx) for tx in raw_transactions]
    booked = [row for row in rows if row["status"] != models.TransactionStatus.PENDING]
    pending = [row for row in rows if row["status"] == models.TransactionStatus.PENDING]
    now = dj_timezone.now()

    with db_transaction.atomic():
        account = models.BankAccount.objects.select_for_update().get(id=syncer.account_id)

        # Only the pending rows the fetch covered: an older one was not re-sent, and dropping it
        # would lose it until it books.
        stale_pending = syncer.transactions.filter(status=models.TransactionStatus.PENDING)
        if since is not None:
            stale_pending = stale_pending.annotate(day=Coalesce("transaction_date", "value_date", "booking_date")).filter(Q(day__gte=since) | Q(day__isnull=True))
        result.pending_replaced = stale_pending.delete()[0]
        pending_rows = [
            models.Transaction(account=account, syncer=syncer, fingerprint=f"pdng:{fp}", **row) for fp, row in zip(fingerprint_all(pending), pending)
        ]
        embed_rows(pending_rows)
        models.Transaction.objects.bulk_create(pending_rows)

        fingerprints = fingerprint_all(booked)
        existing = {tx.fingerprint: tx for tx in account.transactions.filter(fingerprint__in=fingerprints)}
        to_create, to_update = [], []
        new = [(fp, row) for fp, row in zip(fingerprints, booked) if fp not in existing]
        imported = take_over_imported(account, new)
        for fp, row in zip(fingerprints, booked):
            tx = existing.get(fp) or imported.get(fp)
            if tx is None:
                to_create.append(models.Transaction(account=account, syncer=syncer, fingerprint=fp, **row))
                continue
            if fp in imported or tx.syncer_id != syncer.id or any(getattr(tx, name) != row[name] for name in BANK_FIELDS):
                for name in BANK_FIELDS:
                    setattr(tx, name, row[name])
                tx.fingerprint, tx.syncer, tx.origin = fp, syncer, models.TransactionOrigin.SYNC
                tx.updated_at = now
                to_update.append(tx)
        embed_rows(to_create + to_update)
        models.Transaction.objects.bulk_create(to_create)
        models.Transaction.objects.bulk_update(to_update, BANK_FIELDS + ["fingerprint", "syncer", "origin", "updated_at", *EMBEDDING_FIELDS], batch_size=500)
        result.created, result.updated = len(to_create), len(to_update)

        for balance in raw_balances:
            amount = balance.get("balance_amount") or {}
            if "amount" not in amount:
                continue
            models.BalanceSnapshot.objects.update_or_create(
                account=account,
                date=_parse_date(balance.get("reference_date")) or now.date(),
                balance_type=balance.get("balance_type") or "OTHR",
                defaults={"amount": Decimal(str(amount["amount"])), "currency": amount.get("currency") or account.currency},
            )
            result.balances += 1

        touched = [tx.id for tx in to_create + to_update + pending_rows]
        result.categorized = categorize_rows(account.organization_id, touched)
        detect_transfers(account.organization_id)

        mark_synced(syncer, now)
    return result


def record_failure(syncer_id: int, error: Exception) -> None:
    """Remember why a sync failed (with its code), and expire the consent if it is gone.

    A rate-limit answer blocks further syncs of the syncer until the provider's ``Retry-After``,
    or — PSD2 limits are per day — the next UTC day.
    """
    from finance.errors import code_for

    code = code_for(error)
    message = str(error)[:2000]
    with db_transaction.atomic():
        syncer = models.AccountSyncer.objects.select_for_update().get(id=syncer_id)
        syncer.last_error, syncer.last_error_code = message, code
        fields = ["last_error", "last_error_code"]
        if code == models.BankErrorCode.RATE_LIMITED and not isinstance(error, SyncBudgetExhausted):
            retry_after = getattr(error, "retry_after", None)
            now = dj_timezone.now()
            syncer.rate_limited_until = now + timedelta(seconds=retry_after) if retry_after else _tomorrow(now)
            fields.append("rate_limited_until")
        syncer.save(update_fields=fields)
        if code == models.BankErrorCode.CONSENT_EXPIRED and syncer.connection_id:
            # Only an ACTIVE consent can expire; a revoked one stays revoked.
            models.BankConnection.objects.filter(id=syncer.connection_id, status=models.ConnectionStatus.ACTIVE).update(
                status=models.ConnectionStatus.EXPIRED, last_error=message, last_error_code=code
            )


def detect_recurring(account_id: int) -> None:
    """Re-detect the account's recurring payments after a sync; never fails the sync."""
    from finance.recurring import detect_for_account

    try:
        detect_for_account(models.BankAccount.objects.get(id=account_id))
    except Exception:
        logger.warning("Recurring detection for account %s failed.", account_id, exc_info=True)


def _load(syncer_id: int) -> tuple[models.AccountSyncer, date | None]:
    syncer = models.AccountSyncer.objects.select_related("connection", "account").get(id=syncer_id)
    return syncer, fetch_since(syncer, sync_settings()["overlap_days"])


async def _fetch_enablebanking(syncer: models.AccountSyncer, since: date | None, psu_headers: dict[str, str] | None, client: EnableBankingClient | None) -> SyncResult:
    """Fetch an Enable Banking account and store it (the caller holds the lease and handles failures)."""

    async def fetch(eb: EnableBankingClient) -> tuple[list[dict], list[dict]]:
        txs = await eb.transactions(syncer.remote_id, since, psu_headers=psu_headers)
        balances = await eb.balances(syncer.remote_id, psu_headers=psu_headers)
        return txs, balances

    if client is not None:
        txs, balances = await fetch(client)
    else:
        async with EnableBankingClient() as eb:
            txs, balances = await fetch(eb)
    result = await database_sync_to_async(persist)(syncer.id, txs, balances, since)
    logger.info("Synced account %s: %s new, %s updated, %s pending", syncer.account_id, result.created, result.updated, result.pending_replaced)
    return result


async def _fetch_scalable(syncer: models.AccountSyncer, since: date | None, psu_headers: dict[str, str] | None, client: EnableBankingClient | None) -> SyncResult:
    """Fetch a Scalable pot and store it (the caller holds the lease and handles failures)."""
    from finance.scalable import sync as scalable_sync
    from finance.scalable.client import Unauthorized
    from finance.scalable.tokens import refreshed_session, session_for

    # An order can stay pending for weeks: page back far enough to see every pending row again.
    oldest_pending = await syncer.transactions.filter(status=models.TransactionStatus.PENDING).aaggregate(oldest=Min("booking_date"))
    if since and oldest_pending["oldest"]:
        since = min(since, oldest_pending["oldest"])
    async with ScalableClient() as sc:
        session = await session_for(syncer.connection_id, sc)
        try:
            fetched = await scalable_sync.fetch(syncer, session, sc, since)
        except Unauthorized:
            # As the CLI does: refresh once and retry. Only a failed refresh (invalid_grant) means relogin.
            session = await refreshed_session(syncer.connection_id, sc, session)
            fetched = await scalable_sync.fetch(syncer, session, sc, since)
    result = SyncResult(account_id=syncer.account_id)
    await database_sync_to_async(scalable_sync.persist)(syncer.id, fetched, result)
    if fetched.holdings:
        # The depot's last month of prices from Scalable, in this same request; never fails the sync.
        from finance.prices import service as prices

        try:
            isins = sorted({item["isin"] for item in fetched.holdings if item.get("isin")})
            today = dj_timezone.now().date()
            await prices.refresh(syncer.organization_id, isins, today - timedelta(days=31), today, only=[models.PriceSource.SCALABLE])
        except Exception:
            logger.warning("Refreshing prices after the depot sync of account %s failed.", syncer.account_id, exc_info=True)
    logger.info("Synced Scalable account %s: %s new, %s updated, %s holdings", syncer.account_id, result.created, result.updated, result.holdings)
    return result


# How each backend fetches and stores a syncer. A new provider is one more entry.
BACKENDS = {
    models.Provider.ENABLEBANKING: _fetch_enablebanking,
    models.Provider.SCALABLE: _fetch_scalable,
}


async def sync_syncer(syncer_id: int, psu_headers: dict[str, str] | None = None, client: EnableBankingClient | None = None) -> SyncResult:
    """Fetch one syncer from its provider and store it, inside this request.

    Raises :class:`AlreadySyncing` if another request holds the syncer and
    :class:`~finance.errors.SyncBudgetExhausted` — without contacting the provider — when the
    day's budget is spent or the provider asked us to wait. ``psu_headers`` (the user's IP and
    user agent) tell a PSD2 bank that the user is present.
    """
    if not await database_sync_to_async(claim)(syncer_id, sync_settings()["lease_seconds"]):
        raise AlreadySyncing(f"Syncer {syncer_id} is already being synced.")
    try:
        syncer, since = await database_sync_to_async(_load)(syncer_id)
        connection = syncer.connection
        if connection is None or connection.status != models.ConnectionStatus.ACTIVE:
            status = connection.status if connection else "missing"
            raise ConnectionInactive(f"The account's bank connection is {status.lower()}; link the bank again to sync it.")
        if connection.valid_until and connection.valid_until <= dj_timezone.now():
            raise ConsentExpired(0, f"The consent ran out at {connection.valid_until.isoformat()}.", "sync")
        await database_sync_to_async(spend)(syncer_id)
        return await BACKENDS[syncer.backend](syncer, since, psu_headers, client)
    except (ConnectionInactive, SyncBudgetExhausted):
        raise  # nothing reached the provider: the syncer's last sync outcome stands
    except Exception as error:
        await database_sync_to_async(record_failure)(syncer_id, error)
        raise
    finally:
        await database_sync_to_async(release)(syncer_id)


def _syncers_to_run(account_id: int) -> list[int]:
    """The account's syncers with an ACTIVE connection; else its newest syncer (whose sync then explains why not)."""
    syncers = list(models.AccountSyncer.objects.filter(account_id=account_id).select_related("connection").order_by("-id"))
    live = [s.id for s in syncers if s.connection and s.connection.status == models.ConnectionStatus.ACTIVE]
    if live:
        return sorted(live)
    if syncers:
        return [syncers[0].id]
    raise ConnectionInactive("The account has no bank connection (it is fed by imports only); link the bank to sync it.")


async def sync_account(account_id: int, psu_headers: dict[str, str] | None = None, client: EnableBankingClient | None = None) -> SyncResult:
    """Sync every live syncer of the account (see :func:`sync_syncer`); the first failure raises.

    An account whose syncers are all inactive fails with :class:`ConnectionInactive` — as does one
    without any syncer (imported only).
    """
    total = SyncResult(account_id=account_id)
    for syncer_id in await database_sync_to_async(_syncers_to_run)(account_id):
        result = await sync_syncer(syncer_id, psu_headers=psu_headers, client=client)
        for name in ("created", "updated", "pending_replaced", "balances", "categorized", "holdings"):
            setattr(total, name, getattr(total, name) + getattr(result, name))
    await database_sync_to_async(after_sync)(total)
    return total


def after_sync(result: SyncResult) -> None:
    """What follows new rows on an account: recurring detection, then the org's subscribers hear of it."""
    from finance.channels import broadcast_sync

    detect_recurring(result.account_id)
    organization_id = models.BankAccount.objects.filter(id=result.account_id).values_list("organization_id", flat=True).get()
    broadcast_sync(organization_id, result)
    _signal_sync(result)


def _signal_sync(result: SyncResult) -> None:
    """Tell the hub's rekuest the account was synced: transactions are bulk-created (no model
    signal fires for them), so this one UPDATED carries how many there were."""
    from bank_server.service import account_signal

    account = models.BankAccount.objects.select_related("organization").get(id=result.account_id)
    account_signal.emit(
        account.pk,
        organization=account.organization.slug,
        kind="UPDATED",
        descriptors={
            "@bank/kind": str(account.kind),
            "@bank/currency": account.currency or "",
            "@bank/new_transactions": result.created,
            "@bank/updated_transactions": result.updated,
        },
    )
