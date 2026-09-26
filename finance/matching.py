"""Is this imported booking the same as that synced one?

An import and a provider describe the same booking differently: a Finanzguru row has no entry
reference, other counterparty and purpose text, and a booking day that can differ from the bank's
by a day or two. So the two sides are never compared by fingerprint — they are *paired*:

* the amount and currency are equal (both signed, so the direction is equal too);
* the booking days are at most ``window_days`` apart (``imports.match_window_days``);
* when both carry a counterparty IBAN, the IBANs are equal.

Pairing is greedy, closest day first, and one-to-one: each row is used at most once, so two
identical coffees on the same day pair with two rows, not both with one.

It runs on both sides, always inside the writer's atomic block and under the account's row lock
(``select_for_update``), which is what keeps an import and a sync of the same account on two
replicas from each missing the other's rows:

* a **sync** pairs its new booked rows with the account's IMPORT rows and takes them over
  (:func:`finance.sync.persist`, :func:`finance.scalable.sync.persist`);
* an **import** pairs its rows with the account's SYNC rows and enriches them
  (:mod:`finance.imports.apply`).

Only booked rows take part: pending rows are replaced on every sync, so pairing with one would
annotate a row about to disappear.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from django.db.models import QuerySet

from finance import models
from finance.models import normalize_iban

A = TypeVar("A")
B = TypeVar("B")


def window_days() -> int:
    """How far apart the booking days of one booking may be (``imports.match_window_days``)."""
    return getattr(settings, "BANK_IMPORTS", {}).get("match_window_days", 3)


def _get(row: Any, name: str) -> Any:  # noqa: ANN401 - a dict of model fields or a Transaction
    return row.get(name) if isinstance(row, dict) else getattr(row, name)


def booking_day(row: Any) -> date | None:  # noqa: ANN401
    """The day a row was booked (else took effect, else was made)."""
    return _get(row, "booking_date") or _get(row, "value_date") or _get(row, "transaction_date")


def pair(candidates: Sequence[A], pool: Sequence[B], window: int | None = None, day: Callable[[Any], date | None] = booking_day) -> dict[int, B]:
    """Pair ``candidates`` with ``pool`` rows describing the same booking; ``{candidate index: pool row}``."""
    window = window_days() if window is None else window
    by_amount: dict[tuple[Decimal, str], list[tuple[int, B]]] = defaultdict(list)
    for index, row in enumerate(pool):
        by_amount[(Decimal(_get(row, "amount")), _get(row, "currency"))].append((index, row))

    options: list[tuple[int, int, int]] = []
    for c_index, candidate in enumerate(candidates):
        c_day = day(candidate)
        if c_day is None:
            continue
        c_iban = normalize_iban(_get(candidate, "counterparty_iban"))
        for p_index, row in by_amount.get((Decimal(_get(candidate, "amount")), _get(candidate, "currency")), []):
            p_day = day(row)
            if p_day is None:
                continue
            gap = abs((c_day - p_day).days)
            if gap > window:
                continue
            p_iban = normalize_iban(_get(row, "counterparty_iban"))
            if c_iban and p_iban and c_iban != p_iban:
                continue
            options.append((gap, c_index, p_index))

    options.sort()
    pairs: dict[int, B] = {}
    used: set[int] = set()
    for _gap, c_index, p_index in options:
        if c_index in pairs or p_index in used:
            continue
        pairs[c_index] = pool[p_index]
        used.add(p_index)
    return pairs


def pool_for(account: models.BankAccount, origin: str, days: Sequence[date | None], window: int | None = None) -> QuerySet:
    """The account's booked rows of ``origin`` that could pair with rows booked on ``days``."""
    window = window_days() if window is None else window
    known = [d for d in days if d is not None]
    rows = account.transactions.filter(origin=origin, status=models.TransactionStatus.BOOKED)
    if not known:
        return rows.none()
    return rows.filter(booking_date__gte=min(known) - timedelta(days=window), booking_date__lte=max(known) + timedelta(days=window))
