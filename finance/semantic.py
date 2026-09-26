"""Categorizing by similarity: to what the organization already categorized, and to category terms.

Two signals, both computed in the request that needs them (pgvector, exact scans — see
:mod:`embeddings.search` for why there is no ANN index):

* **Neighbours.** The organization's transactions categorized by a user (MANUAL), a rule
  (RULE) or a merchant's default (MERCHANT) that sit close to this one vote for their category, weighted by closeness, a user's
  choice counting double. Never SEMANTIC rows: a guess must not confirm itself.
* **Terms.** Each category is recognized by its name and the phrases of its description
  (:class:`~finance.models.CategoryTerm`), embedded one by one. A close term votes for its
  category. This is what places a never-seen merchant ("HOFER DANKT") before anyone
  categorized one; it is also how a user teaches a category a word: edit the description.

A suggestion's ``score`` is its share of all votes. :func:`auto_assign` assigns the top one
(source SEMANTIC) only when the share is high *and* it has evidence: enough agreeing
neighbours, or a near-exact term hit. Rules override SEMANTIC, users override everything.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.db.models.functions import Least
from pgvector.django import CosineDistance

from embeddings import engine
from finance import models

logger = logging.getLogger(__name__)

NEIGHBOURS, TERMS, BOTH = "NEIGHBOURS", "TERMS", "BOTH"

#: Rows whose category someone decided (a user, a rule, a merchant's default, an imported
#: statement — its history was categorized in the other app): they vote. A SEMANTIC row never
#: does — a guess must not confirm itself.
VOTING_SOURCES = [models.CategorySource.MANUAL, models.CategorySource.RULE, models.CategorySource.MERCHANT, models.CategorySource.IMPORT]


def conf() -> dict:
    return settings.CATEGORIZATION


# --- embedding rows that skip save() -------------------------------------------------------------


def embed_rows(rows: list[Any]) -> None:
    """Fill ``embedding``/``embedding_model`` on rows about to be bulk-written (which skips ``save()``).

    Only rows whose text (or model) changed are embedded, in one batch. When the model cannot
    be loaded the rows are left unembedded (``embedding_model=""``) for the ``reembed_stale``
    action; a sync never fails because of embeddings.
    """
    if not rows or not engine.enabled():
        return
    stale = [row for row in rows if row.embedding_is_stale()]
    if not stale:
        return
    sources = [row.embedding_source_text() for row in stale]
    try:
        vectors = iter(engine.embed_texts([source for source in sources if source is not None]))
    except engine.EmbeddingsUnavailable as error:
        logger.warning("Could not embed %s %s rows (%s); leaving them for reembed_stale", len(stale), type(stale[0]).__name__, error)
        for row in stale:
            row.embedding, row.embedding_model = None, ""
        return
    current = engine.model_id()
    for row, source in zip(stale, sources, strict=True):
        row.embedding = next(vectors) if source is not None else None
        row.embedding_model = current
        row._embedding_source_seen = source


def sync_terms(category: models.Category, texts: list[str]) -> None:
    """Make the category's terms exactly ``texts`` (embedded), keeping unchanged ones."""
    with db_transaction.atomic():
        existing = {term.text: term for term in category.terms.all()}
        category.terms.exclude(text__in=texts).delete()
        new = [models.CategoryTerm(category=category, text=text) for text in texts if text not in existing]
        embed_rows(new)
        models.CategoryTerm.objects.bulk_create(new, ignore_conflicts=True)


# --- suggestions ---------------------------------------------------------------------------------


@dataclass
class Suggestion:
    category: models.Category
    score: float
    reason: str
    neighbours: int = 0
    closest_term: float | None = None
    evidence: list[models.Transaction] = field(default_factory=list)


def _compatible_kinds(amount: Any) -> list[str]:
    if amount is None or amount == 0:
        return list(models.CategoryKind.values)
    if amount < 0:
        return [models.CategoryKind.EXPENSE, models.CategoryKind.TRANSFER]
    return [models.CategoryKind.INCOME, models.CategoryKind.TRANSFER]


def _usable(tx: models.Transaction) -> bool:
    return engine.enabled() and tx.embedding is not None and tx.embedding_model == engine.model_id()


def suggest(tx: models.Transaction, limit: int = 3) -> list[Suggestion]:
    """The categories this transaction most likely belongs to, best first (empty without a usable vector)."""
    if not _usable(tx):
        return []
    c = conf()
    organization_id = tx.account.organization_id
    kinds = _compatible_kinds(tx.amount)
    weights: dict[int, float] = defaultdict(float)
    counts: dict[int, int] = defaultdict(int)
    evidence: dict[int, list[models.Transaction]] = defaultdict(list)
    closest: dict[int, float] = {}

    pool = models.Transaction.objects.filter(
        account__organization_id=organization_id,
        category_source__in=VOTING_SOURCES,
        category__hidden=False,
        category__kind__in=kinds,
        embedding_model=engine.model_id(),
        embedding__isnull=False,
    ).exclude(pk=tx.pk)
    if tx.amount is not None and tx.amount < 0:
        pool = pool.filter(amount__lt=0)
    elif tx.amount is not None and tx.amount > 0:
        pool = pool.filter(amount__gt=0)
    cut = c["vote_distance"]
    near = (
        pool.annotate(_d=CosineDistance("embedding", tx.embedding))
        .filter(_d__lt=cut)
        .order_by("_d", "pk")
        .defer("embedding")[: c["neighbours"]]
    )
    for neighbour in near:
        weight = (1 - neighbour._d / cut) * (2.0 if neighbour.category_source == models.CategorySource.MANUAL else 1.0)
        weights[neighbour.category_id] += weight
        counts[neighbour.category_id] += 1
        if len(evidence[neighbour.category_id]) < 3:
            evidence[neighbour.category_id].append(neighbour)

    term_cut = c["term_distance"]
    terms = (
        models.CategoryTerm.objects.filter(category__organization_id=organization_id, category__hidden=False, category__kind__in=kinds, embedding_model=engine.model_id(), embedding__isnull=False)
        .annotate(_d=CosineDistance("embedding", tx.embedding))
        .filter(_d__lt=term_cut)
        .order_by("_d")
        .values_list("category_id", "_d")[:50]
    )
    for category_id, distance in terms:
        if category_id in closest:
            continue  # one vote per category: its closest term
        closest[category_id] = distance
        weights[category_id] += c["term_weight"] * (1 - distance / term_cut)

    total = sum(weights.values())
    if not total:
        return []
    categories = models.Category.objects.in_bulk(list(weights))
    ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:limit]
    out = []
    for category_id, weight in ranked:
        reason = BOTH if counts[category_id] and category_id in closest else NEIGHBOURS if counts[category_id] else TERMS
        out.append(Suggestion(categories[category_id], round(weight / total, 4), reason, counts[category_id], closest.get(category_id), evidence[category_id]))
    return out


def confident(suggestion: Suggestion) -> bool:
    """Whether a top suggestion may be assigned without asking."""
    c = conf()
    if suggestion.score < c["auto_assign_threshold"]:
        return False
    exact_term = suggestion.closest_term is not None and suggestion.closest_term <= c["term_assign_distance"]
    return suggestion.neighbours >= c["min_evidence"] or exact_term


def auto_assign(organization_id: int, transaction_ids: list[int]) -> int:
    """Give uncategorized transactions their confident top suggestion (source SEMANTIC); returns how many."""
    if not conf()["auto_assign"] or not engine.enabled() or not transaction_ids:
        return 0
    rows = models.Transaction.objects.filter(
        account__organization_id=organization_id, id__in=transaction_ids, category_source=models.CategorySource.NONE
    ).select_related("account")
    changed = []
    for tx in rows:
        suggestions = suggest(tx, limit=1)
        if suggestions and confident(suggestions[0]):
            tx.category_id, tx.category_source = suggestions[0].category.id, models.CategorySource.SEMANTIC
            changed.append(tx)
    models.Transaction.objects.bulk_update(changed, ["category", "category_source"], batch_size=500)
    return len(changed)


def reset_semantic(organization_id: int, transactions: QuerySet) -> int:
    """Hand SEMANTIC rows back to NONE (before re-running rules and assignment over them)."""
    return transactions.filter(account__organization_id=organization_id, category_source=models.CategorySource.SEMANTIC).update(
        category=None, category_source=models.CategorySource.NONE
    )


def categories_matching(organization_id: int, text: str) -> list[int]:
    """Visible categories with a term close to ``text`` ("supermarket" → Groceries), closest first."""
    from finance.textnorm import normalize

    if not engine.enabled():
        return []
    try:
        vector = engine.embed_query(normalize(text) or text)
    except engine.EmbeddingsUnavailable:
        return []
    if vector is None:
        return []
    rows = (
        models.CategoryTerm.objects.filter(category__organization_id=organization_id, category__hidden=False, embedding_model=engine.model_id(), embedding__isnull=False)
        .annotate(_d=CosineDistance("embedding", vector))
        .filter(_d__lt=conf()["term_distance"])
        .order_by("_d")
        .values_list("category_id", flat=True)[:20]
    )
    return list(dict.fromkeys(rows))


# --- "near this category" ------------------------------------------------------------------------


def near_category(queryset: QuerySet, category: models.Category, prefix: str = "") -> tuple[QuerySet, Q]:
    """``queryset`` (transactions) ordered by closeness to ``category``, with the predicate for "close".

    Closeness is the smaller of: the distance to the centroid of the category's MANUAL/RULE
    transactions (what the organization put there), and the distance to its closest term (what
    the category says it is). Close means below the search threshold. Transactions without a
    current vector never match.
    """
    if prefix or not engine.enabled():
        return queryset, Q(pk__in=[])  # nested filters get no semantic leg (as in hybrid_search)
    alias = f"_near_category_{category.pk}"
    if alias not in queryset.query.annotations:
        term = (
            models.CategoryTerm.objects.filter(category=category, embedding_model=engine.model_id(), embedding__isnull=False)
            .annotate(_d=CosineDistance("embedding", OuterRef(f"{prefix}embedding")))
            .order_by("_d")
            .values("_d")[:1]
        )
        distances = [Subquery(term)]
        centroid = category_centroid(category)
        if centroid is not None:
            distances.append(CosineDistance(f"{prefix}embedding", centroid))
        expression = Least(*distances) if len(distances) > 1 else distances[0]
        queryset = queryset.annotate(**{alias: expression}).order_by(F(alias).asc(nulls_last=True), "pk")
    predicate = Q(**{f"{prefix}embedding_model": engine.model_id()}) & Q(**{f"{alias}__lt": engine.distance_threshold()})
    return queryset, predicate


def category_centroid(category: models.Category) -> list[float] | None:
    """The mean vector of the category's MANUAL/RULE transactions, or None when it has none."""
    import numpy as np

    vectors = list(
        category.transactions.filter(
            category_source__in=VOTING_SOURCES, embedding_model=engine.model_id(), embedding__isnull=False
        ).values_list("embedding", flat=True)[:500]
    )
    if not vectors:
        return None
    mean = np.mean(np.array(vectors, dtype=float), axis=0)
    norm = float(np.linalg.norm(mean))
    return (mean / norm).tolist() if norm else None
