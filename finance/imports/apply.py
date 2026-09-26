"""Previewing and applying a statement import (a Finanzguru export).

**Preview** reads the file, decides which account each of its source accounts (``Referenzkonto``)
lands in, and counts what applying would do. It writes no transaction — only the proposed category
mappings, so they can be reviewed and edited before anything is applied.

**Apply** writes the rows, one account at a time, each in one atomic block under the account's
row lock (the same lock a sync's write takes, so an import and a sync of one account never
interleave). Per row, in order:

1. **Already imported** (its ``import_ref`` is on the account): refresh it. Bank fields only on
   an IMPORT row — a synced row keeps the provider's.
2. **Pairs with a synced row** (:mod:`finance.matching`): the synced row is enriched — it gets
   the import's category (unless a user or a rule decided one; a merchant's default and a
   semantic guess give way to it), note (unless it has one), transfer flag (unless a user pinned
   it) and ``import_raw``.
3. **Otherwise** a new IMPORT row. A later sync that finds the same booking takes it over.

Then the ordinary pipeline: embeddings, rules and suggestions for what is still uncategorized,
transfer detection, recurring detection, and the ``accountSyncs`` event.

Which account a source account lands in: the organization's one account with its IBAN (and
currency) — a linked one, or a dead one from before — else the account an earlier import created
for it (``import_key``), else a new account with no syncer. Several accounts with the IBAN are
ambiguous: the preview says so and applying needs the caller to choose.
"""

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from django.db import transaction as db_transaction
from django.utils import timezone

from embeddings.models import EMBEDDING_FIELDS
from finance import models
from finance.accounts import adoptable, by_iban
from finance.imports.finanzguru import FgRow, ParsedExport, is_iban, parse
from finance.imports.finanzguru_categories import base_key
from finance.matching import booking_day, pair, pool_for

logger = logging.getLogger(__name__)

BANK_FIELDS = ["booking_date", "amount", "currency", "status", "counterparty", "counterparty_iban", "remittance", "raw"]
# Category sources an import may replace: nobody decided these (or an earlier import did).
REPLACEABLE = [models.CategorySource.NONE, models.CategorySource.SEMANTIC, models.CategorySource.IMPORT]


class StatementImportError(Exception):
    """The import cannot be previewed or applied as asked (a validation error for the client)."""


# --- Which account ----------------------------------------------------------------------------


@dataclass
class Target:
    """Where one source account's rows go."""

    reference: str
    name: str | None
    currency: str
    iban: str | None
    how: str  # IBAN (an existing account with its IBAN) | IMPORTED (created by an earlier import) | NEW | AMBIGUOUS | CHOSEN | SKIPPED
    account_id: int | None = None
    candidates: list[int] = field(default_factory=list)

    @property
    def import_key(self) -> str:
        return f"fg:{self.reference}"[:500]


def resolve_target(organization_id: int, reference: str, name: str | None, currency: str) -> Target:
    """Which account rows from source account ``reference`` land in (read-only)."""
    iban = reference if is_iban(reference) else None
    target = Target(reference=reference, name=name, currency=currency, iban=iban, how="NEW")
    if iban:
        account = adoptable(organization_id, iban, currency)
        if account is not None:
            target.how, target.account_id = "IBAN", account.id
            return target
        candidates = list(by_iban(organization_id, iban, currency).values_list("id", flat=True))
        if len(candidates) > 1:
            target.how, target.candidates = "AMBIGUOUS", candidates
            return target
    existing = models.BankAccount.objects.filter(organization_id=organization_id, import_key=target.import_key).values_list("id", flat=True).first()
    if existing is not None:
        target.how, target.account_id = "IMPORTED", existing
    return target


def _groups(parsed: ParsedExport) -> dict[str, list[FgRow]]:
    groups: dict[str, list[FgRow]] = defaultdict(list)
    for row in parsed.rows:
        groups[row.reference].append(row)
    return groups


# --- Category mapping -------------------------------------------------------------------------


def _propose(organization_id: int, main: str, sub: str) -> int | None:
    """The category a new (main, sub) pair most likely means: the base taxonomy's, else the closest by terms."""
    key = base_key(main, sub)
    if key is not None:
        found = models.Category.objects.filter(organization_id=organization_id, key=key, hidden=False).values_list("id", flat=True).first()
        if found is not None:
            return found
    from finance.semantic import categories_matching

    matches = categories_matching(organization_id, " ".join(part for part in (sub, main) if part))
    return matches[0] if matches else None


def ensure_mappings(organization_id: int, pairs: set[tuple[str, str]], source: str = models.ImportSource.FINANZGURU) -> dict[tuple[str, str], int | None]:
    """Each pair's category, proposing (and storing) a mapping for a pair seen the first time."""
    from finance.taxonomy import seed_base_categories

    if pairs and not models.Category.objects.filter(organization_id=organization_id).exists():
        seed_base_categories(organization_id)
    known = {(m.main, m.sub): m.category_id for m in models.ImportCategoryMapping.objects.filter(organization_id=organization_id, source=source)}
    for main, sub in sorted(pairs - set(known)):
        mapping, _ = models.ImportCategoryMapping.objects.get_or_create(
            organization_id=organization_id, source=source, main=main[:200], sub=sub[:200], defaults={"category_id": _propose(organization_id, main, sub), "proposed": True}
        )
        known[(main, sub)] = mapping.category_id
    return known


def _pairs(parsed: ParsedExport) -> set[tuple[str, str]]:
    return {(row.main, row.sub) for row in parsed.rows if row.main}


# --- Preview ----------------------------------------------------------------------------------


def _day(row: Any) -> date | None:  # noqa: ANN401 - an FgRow or a Transaction
    return row.booking_date if isinstance(row, FgRow) else booking_day(row)


def _counts(account: models.BankAccount, rows: list[FgRow]) -> dict[str, int]:
    """How the rows would land on an existing account: already imported, paired with synced rows, new."""
    refs = set(account.transactions.filter(import_ref__in=[r.import_ref for r in rows]).values_list("import_ref", flat=True))
    rest = [r for r in rows if r.import_ref not in refs]
    pool = list(pool_for(account, models.TransactionOrigin.SYNC, [r.booking_date for r in rest]).filter(import_ref__isnull=True))
    matched = len(pair(rest, pool, day=_day)) if pool else 0
    return {"already_imported": len(refs), "matched": matched, "new": len(rest) - matched}


def build_report(organization_id: int, parsed: ParsedExport, targets: dict[str, Target] | None = None) -> dict:
    """What applying ``parsed`` would do — or, with the ``targets`` an apply used, what it did (and the proposed category mappings, stored)."""
    mappings = ensure_mappings(organization_id, _pairs(parsed))
    accounts = []
    for reference, rows in _groups(parsed).items():
        target = (targets or {}).get(reference) or resolve_target(organization_id, reference, rows[0].reference_name, rows[0].currency)
        entry: dict[str, Any] = {
            **_target_dict(target),
            "rows": len(rows),
            "first_date": min(r.booking_date for r in rows).isoformat(),
            "last_date": max(r.booking_date for r in rows).isoformat(),
        }
        if target.how == "SKIPPED":
            entry.update({"already_imported": 0, "matched": 0, "new": 0})
        elif target.account_id is not None:
            entry.update(_counts(models.BankAccount.objects.get(id=target.account_id), rows))
        else:
            entry.update({"already_imported": 0, "matched": 0, "new": len(rows)})
        accounts.append(entry)
    unmapped: dict[tuple[str, str], int] = defaultdict(int)
    for row in parsed.rows:
        if row.main and mappings.get((row.main, row.sub)) is None:
            unmapped[(row.main, row.sub)] += 1
    return {
        "rows": len(parsed.rows),
        "splits_folded": parsed.splits_folded,
        "warnings": parsed.warnings,
        "accounts": accounts,
        "unmapped_categories": [{"main": m, "sub": s, "rows": n} for (m, s), n in sorted(unmapped.items())],
    }


def _target_dict(target: Target) -> dict[str, Any]:
    out = asdict(target)
    out["account_name"] = models.BankAccount.objects.filter(id=target.account_id).values_list("name", flat=True).first() if target.account_id else None
    return out


def preview(statement_import: models.StatementImport, content: bytes) -> models.StatementImport:
    """Parse the file and store what applying it would do (status PREVIEWED, or FAILED)."""
    from finance.imports.finanzguru import ExportError

    try:
        parsed = parse(content)
    except ExportError as error:
        statement_import.status, statement_import.error = models.ImportStatus.FAILED, str(error)
        statement_import.save(update_fields=["status", "error"])
        return statement_import
    statement_import.report = build_report(statement_import.organization_id, parsed)
    statement_import.status, statement_import.error = models.ImportStatus.PREVIEWED, None
    statement_import.save(update_fields=["report", "status", "error"])
    return statement_import


# --- Apply ------------------------------------------------------------------------------------


@dataclass
class Applied:
    """What applying did, per account and in total."""

    created: int = 0
    enriched: int = 0
    updated: int = 0
    balances: int = 0
    categorized: int = 0
    accounts: list[dict] = field(default_factory=list)


def _choose(organization_id: int, target: Target, choice: dict | None) -> Target:
    """Apply the caller's choice for a source account: an existing account, a new one, or skip it."""
    if choice is None:
        if target.how == "AMBIGUOUS":
            raise StatementImportError(f"Several accounts have the IBAN of {target.reference}; choose one (or a new account) for it.")
        return target
    if choice.get("skip"):
        target.how, target.account_id = "SKIPPED", None
    elif choice.get("account") is not None:
        account_id = int(choice["account"])
        if not models.BankAccount.objects.filter(organization_id=organization_id, id=account_id).exists():
            raise StatementImportError(f"No account {account_id} in your organization.")
        target.how, target.account_id = "CHOSEN", account_id
    else:
        target.how, target.account_id = "NEW", None
    return target


def _account_for(organization_id: int, target: Target) -> models.BankAccount:
    """The target account, creating it (keyed by ``import_key``, so a concurrent apply cannot create it twice)."""
    if target.account_id is not None:
        return models.BankAccount.objects.get(id=target.account_id)
    account, _ = models.BankAccount.objects.get_or_create(
        organization_id=organization_id,
        import_key=target.import_key,
        defaults={"iban": target.iban, "name": target.name or target.reference, "currency": target.currency, "product": "Finanzguru import"},
    )
    return account


def _raw_line(row: FgRow) -> dict:
    return {
        "booking_id": row.booking_id,
        "reference": row.reference,
        "booking_date": row.booking_date.isoformat(),
        "amount": str(row.amount),
        "currency": row.currency,
        "balance": str(row.balance) if row.balance is not None else None,
        "counterparty": row.counterparty,
        "remittance": row.remittance,
    }


def _bank_fields(row: FgRow) -> dict:
    return {
        "booking_date": row.booking_date,
        "amount": row.amount,
        "currency": row.currency,
        "status": models.TransactionStatus.BOOKED,
        "counterparty": row.counterparty,
        "counterparty_iban": row.counterparty_iban,
        "remittance": row.remittance,
        "raw": {"finanzguru": _raw_line(row)},
    }


def _annotate(tx: models.Transaction, row: FgRow, category_id: int | None, statement_import: models.StatementImport) -> bool:
    """Carry the import's annotations onto ``tx`` where nobody decided otherwise; True if anything changed."""
    before = (tx.category_id, tx.category_source, tx.note, tx.is_transfer, tx.is_transfer_manual, tx.import_ref, tx.import_raw, tx.statement_import_id)
    replaceable = [*REPLACEABLE, *([models.CategorySource.MERCHANT] if hasattr(models.CategorySource, "MERCHANT") else [])]
    if tx.category_source in replaceable:
        if category_id is not None:
            tx.category_id, tx.category_source = category_id, models.CategorySource.IMPORT
        elif tx.category_source == models.CategorySource.IMPORT:
            tx.category_id, tx.category_source = None, models.CategorySource.NONE  # its mapping was cleared
    if row.note and not tx.note:
        tx.note = row.note
    pinned_by_import = bool((tx.import_raw or {}).get("transfer_pinned"))
    if row.transfer and not tx.is_transfer_manual:
        tx.is_transfer, tx.is_transfer_manual, pinned_by_import = True, True, True
    elif not row.transfer and pinned_by_import:
        tx.is_transfer_manual, pinned_by_import = False, False  # back to detection
    tx.import_ref = row.import_ref
    tx.import_raw = {**row.extra, "source": "FINANZGURU", "reference": row.reference, "main": row.main, "sub": row.sub, "transfer": row.transfer, "transfer_pinned": pinned_by_import}
    tx.statement_import = statement_import
    return before != (tx.category_id, tx.category_source, tx.note, tx.is_transfer, tx.is_transfer_manual, tx.import_ref, tx.import_raw, tx.statement_import_id)


ANNOTATION_FIELDS = ["category", "category_source", "note", "is_transfer", "is_transfer_manual", "import_ref", "import_raw", "statement_import"]


def _write_account(account_id: int, rows: list[FgRow], mappings: dict[tuple[str, str], int | None], statement_import: models.StatementImport, result: Applied) -> list[int]:
    """Write one account's rows under its row lock; returns the ids of the rows written."""
    from finance.semantic import embed_rows

    now = timezone.now()
    with db_transaction.atomic():
        account = models.BankAccount.objects.select_for_update().get(id=account_id)
        by_ref = {tx.import_ref: tx for tx in account.transactions.filter(import_ref__in=[r.import_ref for r in rows])}
        rest = [r for r in rows if r.import_ref not in by_ref]
        pool = list(pool_for(account, models.TransactionOrigin.SYNC, [r.booking_date for r in rest]).filter(import_ref__isnull=True))
        paired = pair(rest, pool, day=_day) if pool else {}
        paired_by_ref = {rest[index].import_ref: tx for index, tx in paired.items()}

        to_create: list[models.Transaction] = []
        to_update: list[models.Transaction] = []
        reembed: list[models.Transaction] = []
        counts = {"created": 0, "enriched": 0, "updated": 0}
        for row in rows:
            category_id = mappings.get((row.main, row.sub)) if row.main else None
            tx = by_ref.get(row.import_ref)
            if tx is not None:
                changed = False
                if tx.origin == models.TransactionOrigin.IMPORT:
                    fields = _bank_fields(row)
                    if any(getattr(tx, name) != value for name, value in fields.items()):
                        for name, value in fields.items():
                            setattr(tx, name, value)
                        reembed.append(tx)
                        changed = True
                changed = _annotate(tx, row, category_id, statement_import) or changed
                if changed:
                    tx.updated_at = now
                    to_update.append(tx)
                    counts["updated"] += 1
                continue
            tx = paired_by_ref.get(row.import_ref)
            if tx is not None:
                _annotate(tx, row, category_id, statement_import)
                tx.updated_at = now
                to_update.append(tx)
                counts["enriched"] += 1
                continue
            tx = models.Transaction(account=account, origin=models.TransactionOrigin.IMPORT, fingerprint=row.import_ref, **_bank_fields(row))
            _annotate(tx, row, category_id, statement_import)
            to_create.append(tx)
            counts["created"] += 1

        embed_rows(to_create + reembed)
        models.Transaction.objects.bulk_create(to_create)
        models.Transaction.objects.bulk_update(
            to_update, [*BANK_FIELDS, *ANNOTATION_FIELDS, "updated_at", *EMBEDDING_FIELDS] if reembed else [*ANNOTATION_FIELDS, "updated_at"], batch_size=500
        )
        result.balances += _balances(account, rows)
        for name, value in counts.items():
            setattr(result, name, getattr(result, name) + value)
        result.accounts.append({"account_id": account.id, **counts})
        return [tx.id for tx in to_create + to_update]


def _balances(account: models.BankAccount, rows: list[FgRow]) -> int:
    """Each day's balance after its last booking, where the provider reported none that day (type IMPT)."""
    with_balance = [r for r in rows if r.balance is not None]
    if not with_balance:
        return 0
    # Finanzguru lists newest first; within a day the first row listed is then the day's last booking.
    newest_first = with_balance[0].booking_date >= with_balance[-1].booking_date
    end_of_day: dict[date, FgRow] = {}
    for row in with_balance if newest_first else reversed(with_balance):
        end_of_day.setdefault(row.booking_date, row)
    reported = set(account.balances.exclude(balance_type="IMPT").filter(date__in=list(end_of_day)).values_list("date", flat=True))
    written = 0
    for day, row in end_of_day.items():
        if day in reported:
            continue
        models.BalanceSnapshot.objects.update_or_create(account=account, date=day, balance_type="IMPT", defaults={"amount": row.balance, "currency": row.currency})
        written += 1
    return written


def apply(statement_import_id: int, content: bytes, choices: dict[str, dict] | None = None) -> models.StatementImport:
    """Write the import into the accounts (see the module docstring); re-applying is idempotent.

    ``choices`` maps a source account (``Referenzkonto``) to ``{"account": id}`` (land in that
    account), ``{"account": None}`` (a new account) or ``{"skip": True}``.
    """
    from finance.channels import broadcast_sync
    from finance.sync import SyncResult, categorize_rows, detect_recurring
    from finance.transfers import detect_transfers

    choices = choices or {}
    with db_transaction.atomic():
        # One apply of an import at a time (the lock is held for the whole apply).
        statement_import = models.StatementImport.objects.select_for_update().get(id=statement_import_id)
        organization_id = statement_import.organization_id
        parsed = parse(content)
        groups = _groups(parsed)
        unknown = set(choices) - set(groups)
        if unknown:
            raise StatementImportError(f"The file has no source account {', '.join(sorted(unknown))}.")
        mappings = ensure_mappings(organization_id, _pairs(parsed))
        targets = {
            reference: _choose(organization_id, resolve_target(organization_id, reference, rows[0].reference_name, rows[0].currency), choices.get(reference))
            for reference, rows in groups.items()
        }

        result = Applied()
        touched: list[int] = []
        accounts: list[int] = []
        for reference, rows in groups.items():
            target = targets[reference]
            if target.how == "SKIPPED":
                continue
            account = _account_for(organization_id, target)
            target.account_id = account.id
            accounts.append(account.id)
            touched += _write_account(account.id, rows, mappings, statement_import, result)
        result.categorized = categorize_rows(organization_id, touched)
        detect_transfers(organization_id)

        statement_import.report = {
            **build_report(organization_id, parsed, targets),
            "applied": asdict(result),
        }
        statement_import.status, statement_import.error, statement_import.applied_at = models.ImportStatus.APPLIED, None, timezone.now()
        statement_import.save(update_fields=["report", "status", "error", "applied_at"])

    for account_id in accounts:
        detect_recurring(account_id)
        per_account = next((a for a in result.accounts if a["account_id"] == account_id), {})
        broadcast_sync(organization_id, SyncResult(account_id=account_id, created=per_account.get("created", 0), updated=per_account.get("enriched", 0) + per_account.get("updated", 0)))
    return statement_import
