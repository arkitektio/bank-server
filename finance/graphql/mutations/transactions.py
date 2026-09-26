"""Annotating transactions: category, note, transfer flag."""

from typing import Optional

import strawberry
from django.utils import timezone
from kante.types import Info

from finance import models, rules, types
from finance.graphql.utils import get_many, get_or_404
from finance.transfers import unpin

__all__ = [
    "CategorizeTransactionInput",
    "SetTransactionNoteInput",
    "MarkTransferInput",
    "categorize_transaction",
    "set_transaction_note",
    "mark_transfer",
    "categorize_transactions",
    "mark_transfers",
]


@strawberry.input(description="Set or clear a transaction's category.")
class CategorizeTransactionInput:
    id: strawberry.ID
    category: Optional[strawberry.ID] = strawberry.field(default=None, description="The category; null clears it and lets the rules decide again.")


@strawberry.input(description="Set or clear a transaction's note.")
class SetTransactionNoteInput:
    id: strawberry.ID
    note: Optional[str] = None


@strawberry.input(description="Set whether a transaction moves money between own accounts.")
class MarkTransferInput:
    id: strawberry.ID
    is_transfer: Optional[bool] = strawberry.field(default=None, description="True or false pins it; null returns it to automatic detection (by counterparty IBAN).")


def categorize_transaction(info: Info, input: CategorizeTransactionInput) -> types.Transaction:
    """Categorize a transaction by hand. Rules never change a manual category; clearing it hands it back to them."""
    tx = get_or_404(models.Transaction, info, input.id)
    if input.category:
        tx.category = get_or_404(models.Category, info, input.category)
        tx.category_source = models.CategorySource.MANUAL
        tx.save(update_fields=["category", "category_source", "updated_at"])
    else:
        tx.category, tx.category_source = None, models.CategorySource.NONE
        tx.save(update_fields=["category", "category_source", "updated_at"])
        rules.apply_rules(info.context.request.organization.id, models.Transaction.objects.filter(id=tx.id))
        tx.refresh_from_db()
    return tx  # type: ignore[return-value]


def set_transaction_note(info: Info, input: SetTransactionNoteInput) -> types.Transaction:
    """Attach a note to a transaction (booked transactions keep it across syncs)."""
    tx = get_or_404(models.Transaction, info, input.id)
    tx.note = input.note or None
    tx.save(update_fields=["note", "updated_at"])
    return tx  # type: ignore[return-value]


def mark_transfer(info: Info, input: MarkTransferInput) -> types.Transaction:
    """Pin (or un-pin) a transaction as a transfer between own accounts; transfers are left out of stats."""
    tx = get_or_404(models.Transaction, info, input.id)
    if input.is_transfer is None:
        unpin(info.context.request.organization.id, [tx.id])
        tx.refresh_from_db()
    else:
        tx.is_transfer, tx.is_transfer_manual = input.is_transfer, True
        tx.save(update_fields=["is_transfer", "is_transfer_manual", "updated_at"])
    return tx  # type: ignore[return-value]


def _in_order(info: Info, ids: list[strawberry.ID]) -> list[types.Transaction]:
    rows = {str(tx.id): tx for tx in get_many(models.Transaction, info, ids)}
    return [rows[str(i)] for i in dict.fromkeys(str(i) for i in ids)]  # type: ignore[misc]


def categorize_transactions(info: Info, ids: list[strawberry.ID], category: Optional[strawberry.ID] = None) -> list[types.Transaction]:
    """Categorize many transactions in one request. A category pins it (rules never change it); null clears it and lets the rules decide again."""
    selected = [tx.id for tx in get_many(models.Transaction, info, ids)]
    rows = models.Transaction.objects.filter(id__in=selected)
    if category:
        target = get_or_404(models.Category, info, category)
        rows.update(category=target, category_source=models.CategorySource.MANUAL, updated_at=timezone.now())
    else:
        rows.update(category=None, category_source=models.CategorySource.NONE, updated_at=timezone.now())
        rules.apply_rules(info.context.request.organization.id, models.Transaction.objects.filter(id__in=selected))
    return _in_order(info, ids)


def mark_transfers(info: Info, ids: list[strawberry.ID], is_transfer: Optional[bool] = None) -> list[types.Transaction]:
    """Pin (true/false) many transactions as transfers between own accounts in one request; null returns them to automatic detection."""
    selected = [tx.id for tx in get_many(models.Transaction, info, ids)]
    if is_transfer is None:
        unpin(info.context.request.organization.id, selected)
    else:
        models.Transaction.objects.filter(id__in=selected).update(is_transfer=is_transfer, is_transfer_manual=True, updated_at=timezone.now())
    return _in_order(info, ids)
