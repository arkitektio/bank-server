"""Merchants: who a transaction was with, and where.

A merchant is the organization's own record ("Spar"), recognized by explicit **merchant rules**
(like category rules: an IBAN, a remittance text, a regex — first by priority wins) and by **aliases**: normalized
counterparty prefixes ("spar", "spar gourmet"). A line's key is its normalized counterparty
(:func:`finance.textnorm.normalize` — "Spar Dankt 3418" → "spar"); the longest alias whose words
start the key wins, so "spar gourmet" beats "spar", while "eurospar" and "sparkasse" match
neither. Bank lines rarely carry an address, but card lines often carry a **store number**
("3418"): it becomes a location of the merchant (DISCOVERED, address unknown until someone
fills it in or geocodes it), so the same store is recognized on every later line.

Categories: a merchant may carry a default category. It is applied after rules and before
semantic guesses (source MERCHANT), and only to rows whose category nobody pinned
(NONE, SEMANTIC or MERCHANT) — never MANUAL, RULE or IMPORT.

Everything runs inside the request that needs it: the sync pipeline, or the mutation that
changed an alias or a merchant's category.
"""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction as db_transaction

from finance import models
from finance.textnorm import normalize

_STORE_CODE = re.compile(r"(?<!\d)(\d{3,6})(?!\d)")

#: Category sources a merchant may overwrite: nobody pinned these.
OVERWRITABLE = (models.CategorySource.NONE, models.CategorySource.SEMANTIC, models.CategorySource.MERCHANT)


def merchant_key(text: str | None) -> str | None:
    """The normalized counterparty a line is matched by ("Spar Dankt 3418" → "spar")."""
    return normalize(text)


def store_code(text: str | None) -> str | None:
    """The store number in a counterparty ("Spar Dankt 3418" → "3418"), if any."""
    if not text:
        return None
    found = _STORE_CODE.search(text)
    return found.group(1) if found else None


def _aliases(organization_id: int) -> list[tuple[tuple[str, ...], int]]:
    """The organization's aliases as word tuples, longest first."""
    rows = models.MerchantAlias.objects.filter(organization_id=organization_id).values_list("pattern", "merchant_id")
    return sorted(((tuple(pattern.split()), merchant_id) for pattern, merchant_id in rows), key=lambda item: -len(item[0]))


def resolve(key: str | None, aliases: list[tuple[tuple[str, ...], int]]) -> int | None:
    """The merchant whose longest alias starts ``key`` (word-wise), or None."""
    if not key:
        return None
    words = tuple(key.split())
    for pattern, merchant_id in aliases:
        if words[: len(pattern)] == pattern:
            return merchant_id
    return None


def match(organization_id: int, transaction_ids: list[int]) -> int:
    """Attach merchants (and store locations) to these transactions; returns how many changed.

    The first active merchant rule by priority wins (source RULE), else the longest alias
    (source AUTO). Rows a user linked (MANUAL) are left alone; a row linked earlier by a rule or
    an alias that nothing matches any more loses its merchant.
    """
    from finance.rules import matches

    if not transaction_ids:
        return 0
    aliases = _aliases(organization_id)
    rules = list(models.MerchantRule.objects.filter(organization_id=organization_id, active=True).order_by("priority", "id"))
    rows = list(
        models.Transaction.objects.filter(account__organization_id=organization_id, id__in=transaction_ids)
        .exclude(merchant_source=models.MerchantSource.MANUAL)
        .only("id", "counterparty", "counterparty_iban", "remittance", "amount", "merchant_id", "merchant_location_id", "merchant_source")
    )
    locations: dict[tuple[int, str], int] = {}
    changed = []
    for tx in rows:
        rule = next((rule for rule in rules if matches(rule, tx)), None)
        if rule is not None:
            merchant_id, source = rule.merchant_id, models.MerchantSource.RULE
        else:
            merchant_id, source = resolve(merchant_key(tx.counterparty), aliases), models.MerchantSource.AUTO
        if merchant_id is None:
            if tx.merchant_source in (models.MerchantSource.AUTO, models.MerchantSource.RULE):
                tx.merchant_id = tx.merchant_location_id = None
                tx.merchant_source = models.MerchantSource.NONE
                changed.append(tx)
            continue
        location_id = rule.location_id if rule is not None and rule.location_id else None
        code = store_code(tx.counterparty)
        if location_id is None and code is not None:
            if (merchant_id, code) not in locations:
                locations[(merchant_id, code)] = _store(merchant_id, code)
            location_id = locations[(merchant_id, code)]
        if (tx.merchant_id, tx.merchant_location_id, tx.merchant_source) != (merchant_id, location_id, source):
            tx.merchant_id, tx.merchant_location_id, tx.merchant_source = merchant_id, location_id, source
            changed.append(tx)
    models.Transaction.objects.bulk_update(changed, ["merchant", "merchant_location", "merchant_source"], batch_size=500)
    return len(changed)


def _store(merchant_id: int, code: str) -> int:
    """The merchant's location for a store number, created (DISCOVERED) on first sight."""
    existing = models.MerchantLocation.objects.filter(merchant_id=merchant_id, store_code=code).values_list("id", flat=True).first()
    if existing is not None:
        return existing
    merchant = models.Merchant.objects.only("name").get(id=merchant_id)
    try:
        with db_transaction.atomic():
            return models.MerchantLocation.objects.create(merchant_id=merchant_id, store_code=code, name=f"{merchant.name} {code}", source=models.LocationSource.DISCOVERED).id
    except IntegrityError:  # a concurrent sync discovered it first
        return models.MerchantLocation.objects.get(merchant_id=merchant_id, store_code=code).id


def categorize_by_merchant(organization_id: int, transaction_ids: list[int]) -> int:
    """Give transactions their merchant's category (source MERCHANT) where nobody pinned one; returns how many changed."""
    if not transaction_ids:
        return 0
    rows = (
        models.Transaction.objects.filter(account__organization_id=organization_id, id__in=transaction_ids, category_source__in=OVERWRITABLE)
        .select_related("merchant__category")
        .only("id", "category_id", "category_source", "merchant__category__id", "merchant__category__hidden")
    )
    changed = []
    for tx in rows:
        category = tx.merchant.category if tx.merchant_id and tx.merchant.category_id else None
        if category is not None and not category.hidden:
            if (tx.category_id, tx.category_source) != (category.id, models.CategorySource.MERCHANT):
                tx.category_id, tx.category_source = category.id, models.CategorySource.MERCHANT
                changed.append(tx)
        elif tx.category_source == models.CategorySource.MERCHANT:
            tx.category_id, tx.category_source = None, models.CategorySource.NONE
            changed.append(tx)
    models.Transaction.objects.bulk_update(changed, ["category", "category_source"], batch_size=500)
    return len(changed)


def organization_rows(organization_id: int, merchant_id: int | None = None) -> list[int]:
    """Transaction ids to re-run the pipeline over after a merchant edit (all, or one merchant's)."""
    rows = models.Transaction.objects.filter(account__organization_id=organization_id)
    if merchant_id is not None:
        rows = rows.filter(merchant_id=merchant_id)
    return list(rows.values_list("id", flat=True))


# --- turning frequent counterparties into merchants ----------------------------------------------


@dataclass
class Candidate:
    key: str
    count: int
    totals: dict[str, Decimal]
    samples: list[str]
    store_codes: int
    latest: models.Transaction
    transaction_ids: list[int] = field(default_factory=list)


def candidates(organization_id: int, limit: int = 20, min_count: int = 2) -> list[Candidate]:
    """Counterparties without a merchant that recur, most frequent first."""
    groups: dict[str, list[models.Transaction]] = defaultdict(list)
    rows = (
        models.Transaction.objects.filter(account__organization_id=organization_id, merchant__isnull=True, counterparty__isnull=False)
        .select_related("account")
        .defer("embedding", "raw")
        .order_by("-booking_date", "-id")
    )
    for tx in rows:
        key = merchant_key(tx.counterparty)
        if key:
            groups[key].append(tx)
    out = []
    for key, txs in groups.items():
        if len(txs) < min_count:
            continue
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for tx in txs:
            totals[tx.currency] += tx.amount
        samples = [name for name, _ in Counter(tx.counterparty for tx in txs).most_common(3)]
        codes = {store_code(tx.counterparty) for tx in txs} - {None}
        out.append(Candidate(key, len(txs), dict(totals), samples, len(codes), txs[0], [tx.id for tx in txs]))
    out.sort(key=lambda c: (-c.count, c.key))
    return out[:limit]


def keys_of(transactions: list[models.Transaction]) -> list[str]:
    """The distinct match keys of some transactions (aliases for a merchant created from them)."""
    return list(dict.fromkeys(key for key in (merchant_key(tx.counterparty) for tx in transactions) if key))


# --- what a merchant adds to its transactions' embedding ----------------------------------------

#: A description is cut to this many words: it describes the merchant, it must not drown the line.
DESCRIPTION_WORDS = 12


def build_context(merchant: models.Merchant | None, location: models.MerchantLocation | None) -> str:
    """The words a transaction's merchant and place add to its embedding.

    The merchant's name and (short) description, its hand-set default category once (a light
    touch: one word or two among the rest), the place's name and city. Street and store number
    stay out: they only tell stores apart, not what the payment was.
    """
    from finance.textnorm import context_words

    if merchant is None:
        return ""
    parts = [merchant.name, " ".join((merchant.description or "").split()[:DESCRIPTION_WORDS])]
    category = merchant.category if merchant.category_id else None
    if category is not None and not category.hidden:
        parts.append(category.name)
    if location is not None:
        parts += [location.name, location.city or ""]
    return context_words(" ".join(p for p in parts if p)) or ""


def refresh_context(organization_id: int, transaction_ids: list[int]) -> int:
    """Bring these transactions' merchant context up to date and re-embed the ones whose text changed; returns how many."""
    from embeddings.models import EMBEDDING_FIELDS
    from finance.semantic import embed_rows

    if not transaction_ids:
        return 0
    rows = (
        models.Transaction.objects.filter(account__organization_id=organization_id, id__in=transaction_ids)
        .select_related("merchant__category", "merchant_location")
        .only(
            "id", "counterparty", "remittance", "kind", "merchant_context", *EMBEDDING_FIELDS,
            "merchant__name", "merchant__description", "merchant__category_id", "merchant__category__name", "merchant__category__hidden",
            "merchant_location__name", "merchant_location__city",
        )
    )
    changed = []
    for tx in rows:
        context = build_context(tx.merchant, tx.merchant_location)
        if context != tx.merchant_context:
            tx.merchant_context = context
            changed.append(tx)
    embed_rows(changed)
    models.Transaction.objects.bulk_update(changed, ["merchant_context", *EMBEDDING_FIELDS], batch_size=500)
    return len(changed)
