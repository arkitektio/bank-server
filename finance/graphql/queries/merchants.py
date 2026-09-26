"""Merchant lookups, candidates, per-merchant stats and address search."""

import datetime
import json
from decimal import Decimal
from typing import Optional

import strawberry
import strawberry_django
from django.db.models import Count, F, Max, Q, Sum
from kante.types import Info

from finance import filters, geo, geocoding, merchants, models, semantic, stats, types
from finance.graphql.errors import translate
from finance.graphql.utils import get_many, get_or_404
from finance.scoping import for_org

__all__ = ["merchant", "merchant_location", "merchant_candidates", "spending_by_merchant", "geocode_search", "merchant_locations_geojson"]


def merchant(info: Info, id: strawberry.ID) -> types.Merchant:
    """A merchant by id."""
    return get_or_404(models.Merchant, info, id)  # type: ignore[return-value]


def merchant_location(info: Info, id: strawberry.ID) -> types.MerchantLocation:
    """A merchant location by id."""
    return get_or_404(models.MerchantLocation, info, id)  # type: ignore[return-value]


def merchant_candidates(info: Info, limit: int = 20, min_count: int = 2) -> list[types.MerchantCandidate]:
    """Counterparties that recur without a merchant, most frequent first — create one with `createMerchant(input: {name, fromTransactions})`."""
    out = []
    for candidate in merchants.candidates(info.context.request.organization.id, limit, min_count):
        suggestion = semantic.suggest(candidate.latest, 1)
        out.append(
            types.MerchantCandidate(
                key=candidate.key,
                count=candidate.count,
                totals=[types.CurrencyTotal(currency=c, amount=a) for c, a in sorted(candidate.totals.items())],
                samples=candidate.samples,
                store_codes=candidate.store_codes,
                transaction_ids=[strawberry.ID(str(i)) for i in candidate.transaction_ids],
                suggested_category=suggestion[0].category if suggestion else None,  # type: ignore[arg-type]
            )
        )
    return out


def spending_by_merchant(
    info: Info,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
    accounts: list[strawberry.ID] | None = None,
    include_transfers: bool = False,
    limit: int | None = None,
) -> list[types.MerchantTotal]:
    """Income, expense and net per merchant and currency over booked transactions, biggest expense first."""
    account_ids = [a.id for a in get_many(models.BankAccount, info, accounts)] if accounts else None
    totals = stats.spending_by_merchant(stats.window(for_org(models.Transaction, info), date_from, date_to, account_ids, include_transfers), limit)
    found = {m.id: m for m in for_org(models.Merchant, info).filter(id__in=[t.merchant_id for t in totals if t.merchant_id])}
    return [
        types.MerchantTotal(merchant=found.get(t.merchant_id), currency=t.currency, income=t.income, expense=t.expense, net=t.net, count=t.count)  # type: ignore[arg-type]
        for t in totals
    ]


async def geocode_search(info: Info, query: str, limit: int = 5) -> list[types.GeocodeResult]:
    """Places matching an address or a name (OpenStreetMap), for a location picker. Looked up in this request."""
    try:
        places = await geocoding.search(query, limit=limit)
    except Exception as error:
        raise translate(error) from error
    return [types.GeocodeResult(**place.__dict__) for place in places]


def merchant_locations_geojson(info: Info, filters: Optional[filters.MerchantLocationFilter] = None, limit: int = 5000) -> types.MerchantLocationFeatureCollection:
    """The organization's located merchant places as a (typed) GeoJSON FeatureCollection, for a map renderer.

    One Point feature per place, geometry built by PostGIS (``[longitude, latitude]``). Takes the
    same filters as `merchantLocations` — `within` (the map's viewport), `near`, `merchant`,
    `category`, `city`. Places without coordinates are left out.
    """
    rows = for_org(models.MerchantLocation, info).filter(point__isnull=False)
    if filters is not None:
        rows = strawberry_django.filters.apply(filters, rows, info)
    distances = "_distance_meters" in rows.query.annotations
    rows = rows.annotate(
        _geometry=geo.AsGeoJSON(F("point")),
        _count=Count("transactions", distinct=True),
        _last=Max("transactions__booking_date"),
        _net=Sum("transactions__amount", filter=Q(transactions__status=models.TransactionStatus.BOOKED)),
    ).select_related("merchant__category")
    features = []
    for place in rows[: max(0, min(limit, 20000))]:
        geometry = json.loads(place._geometry)
        category = place.merchant.category
        features.append(
            types.MerchantLocationFeature(
                type="Feature",
                id=strawberry.ID(str(place.id)),
                geometry=types.PointGeometry(type="Point", coordinates=[float(c) for c in geometry["coordinates"]]),
                properties=types.MerchantLocationProperties(
                    id=strawberry.ID(str(place.id)),
                    name=place.name,
                    store_code=place.store_code,
                    source=place.source,  # type: ignore[arg-type]
                    street=place.street,
                    postal_code=place.postal_code,
                    city=place.city,
                    country=place.country,
                    merchant_id=strawberry.ID(str(place.merchant_id)),
                    merchant_name=place.merchant.name,
                    category_id=strawberry.ID(str(category.id)) if category else None,
                    category_name=category.name if category else None,
                    category_kind=category.kind if category else None,  # type: ignore[arg-type]
                    color=category.color if category else None,
                    transaction_count=place._count,
                    last_visit=place._last,
                    net=(place._net or Decimal("0")).quantize(Decimal("0.01")),
                    currency="EUR",
                    distance_meters=round(place._distance_meters, 1) if distances and place._distance_meters is not None else None,
                ),
            )
        )
    bbox = None
    if features:
        lons = [f.geometry.coordinates[0] for f in features]
        lats = [f.geometry.coordinates[1] for f in features]
        bbox = [min(lons), min(lats), max(lons), max(lats)]
    return types.MerchantLocationFeatureCollection(type="FeatureCollection", features=features, bbox=bbox)
