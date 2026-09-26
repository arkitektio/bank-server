"""Flagging money moved between an organization's own accounts.

Without this, paying the credit card from the checking account counts twice in the stats —
once as an expense on one account and once as income on the other.
"""

from django.db.models.functions import Replace, Upper
from django.db.models import Q, Value

from finance import models


def _clean(iban: str) -> str:
    return iban.replace(" ", "").upper()


def detect_transfers(organization_id: int) -> int:
    """Set ``is_transfer`` from the counterparty IBAN, for every row a user did not set by hand."""
    own = {_clean(iban) for iban in models.BankAccount.objects.filter(organization_id=organization_id).exclude(iban=None).values_list("iban", flat=True)}
    rows = (
        # Rows a provider classified (``kind`` set, e.g. Scalable trades) keep the provider's flag.
        models.Transaction.objects.filter(account__organization_id=organization_id, is_transfer_manual=False, kind__isnull=True)
        .annotate(clean_iban=Upper(Replace("counterparty_iban", Value(" "), Value(""))))
    )
    flagged = rows.filter(clean_iban__in=own, is_transfer=False).update(is_transfer=True) if own else 0
    unflagged = rows.filter(is_transfer=True).filter(Q(counterparty_iban__isnull=True) | ~Q(clean_iban__in=own)).update(is_transfer=False)
    return flagged + unflagged


def unpin(organization_id: int, transaction_ids: list[int]) -> None:
    """Hand the transfer flag of these rows back to automatic detection.

    Provider-classified rows (``kind`` set) get the provider's flag back — trades and deposits
    are transfers — and bank rows are re-detected by counterparty IBAN.
    """
    from finance.scalable.sync import TRANSFER_KINDS

    rows = models.Transaction.objects.filter(account__organization_id=organization_id, id__in=transaction_ids)
    rows.update(is_transfer_manual=False)
    rows.filter(kind__in=TRANSFER_KINDS).update(is_transfer=True)
    rows.filter(kind__isnull=False).exclude(kind__in=TRANSFER_KINDS).update(is_transfer=False)
    detect_transfers(organization_id)
