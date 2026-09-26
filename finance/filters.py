"""Filtering and ordering for the list fields.

``strawberry_django`` turns these into GraphQL inputs the paginated list fields accept, e.g.
``transactions(filters: {dateFrom: "2026-01-01", uncategorized: true}, order: {bookingDate: DESC})``.
"""

import datetime
from decimal import Decimal

import strawberry
import strawberry_django
from django.db.models import F, Max, Q, QuerySet
from kante.types import Info

from embeddings import search
from embeddings.search import hybrid_search
from strawberry import auto

from finance import enums, geo, models, semantic
from finance.scoping import for_org
from finance.textnorm import normalize


# How deep `includeChildCategories` follows nested categories.
CATEGORY_DEPTH = 5


def _ids(prefix: str, field: str, value: list[strawberry.ID]) -> Q:
    return Q(**{f"{prefix}{field}__in": value})


@strawberry_django.filter_type(models.BankConnection)
class BankConnectionFilter:
    """Filtering options for bank connections."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these connections."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def status(self, value: enums.ConnectionStatus, prefix: str) -> Q:
        """Only connections in this status."""
        return Q(**{f"{prefix}status": value.value})


@strawberry_django.filter_type(models.BankAccount)
class BankAccountFilter:
    """Filtering options for bank accounts."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these accounts."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def connection(self, value: strawberry.ID, prefix: str) -> Q:
        """Only accounts reached through this connection."""
        return Q(**{f"{prefix}syncers__connection_id": value})

    @strawberry_django.filter_field
    def kind(self, value: enums.AccountKind, prefix: str) -> Q:
        """Only accounts of this kind (e.g. DEPOT for a portfolio page)."""
        return Q(**{f"{prefix}kind": value.value})

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """Case-insensitive match on name or IBAN."""
        return Q(**{f"{prefix}name__icontains": value}) | Q(**{f"{prefix}iban__icontains": value.replace(" ", "")})


@strawberry_django.order_type(models.Transaction)
class TransactionOrder:
    """Ordering options for transactions."""

    booking_date: auto
    amount: auto
    created_at: auto


@strawberry.input(description="A map viewport: the WGS84 box between south/north latitudes and west/east longitudes.")
class BoundsInput:
    south: float
    west: float
    north: float
    east: float


@strawberry.input(description="A circle on the map: WGS84 latitude/longitude and a radius in meters.")
class NearInput:
    latitude: float
    longitude: float
    radius_meters: float = strawberry.field(default=1000.0, description="Radius in meters (default 1 km).")


@strawberry_django.filter_type(models.Transaction)
class TransactionFilter:
    """Filtering options for transactions."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these transactions."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def accounts(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only transactions on these accounts."""
        return _ids(prefix, "account_id", value)

    @strawberry_django.filter_field
    def date_from(self, value: datetime.date, prefix: str) -> Q:
        """Booked on or after this day."""
        return Q(**{f"{prefix}booking_date__gte": value})

    @strawberry_django.filter_field
    def date_to(self, value: datetime.date, prefix: str) -> Q:
        """Booked on or before this day."""
        return Q(**{f"{prefix}booking_date__lte": value})

    @strawberry_django.filter_field
    def categories(self, value: list[strawberry.ID], prefix: str) -> Q:
        """In one of these categories; their children too with `includeChildCategories`."""
        query = _ids(prefix, "category_id", value)
        if getattr(self, "include_child_categories", None) is True:
            # Walk the parent chain in the query itself (no lookup while filters are built).
            path = "category__parent"
            for _ in range(CATEGORY_DEPTH):
                query |= Q(**{f"{prefix}{path}_id__in": value})
                path += "__parent"
        return query

    @strawberry_django.filter_field
    def include_child_categories(self, value: bool, prefix: str) -> Q:
        """With `categories`: also match their child categories, as budgets roll children up."""
        return Q()

    @strawberry_django.filter_field
    def kind(self, value: enums.TransactionKind, prefix: str) -> Q:
        """Only transactions of this provider type."""
        return Q(**{f"{prefix}kind": value.value})

    @strawberry_django.filter_field
    def kinds(self, value: list[enums.TransactionKind], prefix: str) -> Q:
        """Only transactions of one of these provider types (e.g. BUY and SELL for a depot's trades)."""
        return Q(**{f"{prefix}kind__in": [kind.value for kind in value]})

    @strawberry_django.filter_field
    def uncategorized(self, value: bool, prefix: str) -> Q:
        """Only (or never) transactions without a category."""
        return Q(**{f"{prefix}category__isnull": value})

    @strawberry_django.filter_field
    def amount_min(self, value: Decimal, prefix: str) -> Q:
        """Signed amount at least this (negative is money out)."""
        return Q(**{f"{prefix}amount__gte": value})

    @strawberry_django.filter_field
    def amount_max(self, value: Decimal, prefix: str) -> Q:
        """Signed amount at most this."""
        return Q(**{f"{prefix}amount__lte": value})

    @strawberry_django.filter_field
    def direction(self, value: enums.Direction, prefix: str) -> Q:
        """Only money in, or only money out."""
        return Q(**{f"{prefix}amount__gt" if value == enums.Direction.IN else f"{prefix}amount__lt": 0})

    @strawberry_django.filter_field(description="Search by text: a case-insensitive substring of counterparty, remittance or note; semantic similarity to them; or a category whose terms mean the text (\"supermarket\" finds what is in Groceries). Substring matches rank first, then by similarity; an explicit `ordering` replaces that ranking.")
    def search(self, info: Info, queryset: QuerySet, value: str, prefix: str) -> tuple[QuerySet, Q]:
        lexical = Q(**{f"{prefix}counterparty__icontains": value}) | Q(**{f"{prefix}remittance__icontains": value}) | Q(**{f"{prefix}note__icontains": value})
        queryset, predicate = hybrid_search(queryset, prefix, normalize(value) or value, lexical)
        # A concept ("supermarket") also means the categories whose terms say it. Only when no
        # line contains the text: a merchant query ("spar") must not widen to its whole category.
        if not prefix and not queryset.filter(lexical).exists():
            categories = semantic.categories_matching(info.context.request.organization.id, value)
            if categories:
                predicate |= Q(category_id__in=categories)
        return queryset, predicate

    @strawberry_django.filter_field(description="Order by similarity to the given transaction, nearest first (no cut-off, composes with other filters and pagination). Empty when it is not in this organization or has no embedding yet.")
    def similar_to(self, info: Info, queryset: QuerySet, value: strawberry.ID, prefix: str) -> tuple[QuerySet, Q]:
        if prefix:
            return queryset, Q()
        # The anchor through for_org: another organization's vector must not steer this query.
        anchor = for_org(models.Transaction, info).filter(pk=value).values_list("embedding", flat=True).first()
        return search.neighbourhood(queryset, anchor, exclude_pk=value)

    @strawberry_django.filter_field(description="Keep transactions that look like they belong to this category — close to what the organization put there, or to one of its terms — closest first.")
    def near_category(self, info: Info, queryset: QuerySet, value: strawberry.ID, prefix: str) -> tuple[QuerySet, Q]:
        category = for_org(models.Category, info).filter(pk=value).first()
        if category is None:
            return queryset, Q(pk__in=[])
        return semantic.near_category(queryset, category, prefix)

    @strawberry_django.filter_field
    def merchants(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only transactions with one of these merchants."""
        return _ids(prefix, "merchant_id", value)

    @strawberry_django.filter_field
    def locations(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only transactions at one of these merchant locations."""
        return _ids(prefix, "merchant_location_id", value)

    @strawberry_django.filter_field
    def merchant_source(self, value: enums.MerchantSource, prefix: str) -> Q:
        """Only transactions whose merchant was set this way (NONE: no merchant yet)."""
        return Q(**{f"{prefix}merchant_source": value.value})

    @strawberry_django.filter_field(description="Only transactions at a merchant location within `radiusMeters` of a point, nearest first.")
    def near(self, info: Info, queryset: QuerySet, value: NearInput, prefix: str) -> tuple[QuerySet, Q]:
        annotations, where = geo.near(prefix, "merchant_location__point", value.latitude, value.longitude, value.radius_meters)
        return queryset.annotate(**annotations).order_by("_distance_meters", "pk"), Q(**where)

    @strawberry_django.filter_field
    def category_source(self, value: enums.CategorySource, prefix: str) -> Q:
        """Only transactions categorized this way (e.g. SEMANTIC, to review automatic guesses)."""
        return Q(**{f"{prefix}category_source": value.value})

    @strawberry_django.filter_field
    def status(self, value: enums.TransactionStatus, prefix: str) -> Q:
        """Only booked, or only pending, transactions."""
        return Q(**{f"{prefix}status": value.value})

    @strawberry_django.filter_field
    def is_transfer(self, value: bool, prefix: str) -> Q:
        """Only (or never) transfers between own accounts."""
        return Q(**{f"{prefix}is_transfer": value})


@strawberry_django.filter_type(models.Category)
class CategoryFilter:
    """Filtering options for categories."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these categories."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def roots(self, value: bool, prefix: str) -> Q:
        """Only (or never) top-level categories."""
        return Q(**{f"{prefix}parent__isnull": value})

    @strawberry_django.filter_field
    def kind(self, value: enums.CategoryKind, prefix: str) -> Q:
        """Only categories of this kind."""
        return Q(**{f"{prefix}kind": value.value})

    @strawberry_django.filter_field(description="Search by text: a substring of name or description, or semantic similarity to both.")
    def search(self, info: Info, queryset: QuerySet, value: str, prefix: str) -> tuple[QuerySet, Q]:
        lexical = Q(**{f"{prefix}name__icontains": value}) | Q(**{f"{prefix}description__icontains": value})
        return hybrid_search(queryset, prefix, value, lexical)

    @strawberry_django.filter_field
    def hidden(self, value: bool, prefix: str) -> Q:
        """Only hidden (or only visible) categories."""
        return Q(**{f"{prefix}hidden": value})

    @strawberry_django.filter_field
    def base(self, value: bool, prefix: str) -> Q:
        """Only base categories (or only the organization's own)."""
        return Q(**{f"{prefix}key__isnull": not value})


@strawberry_django.filter_type(models.CategoryRule)
class CategoryRuleFilter:
    """Filtering options for rules."""

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only rules assigning this category."""
        return Q(**{f"{prefix}category_id": value})

    @strawberry_django.filter_field
    def active(self, value: bool, prefix: str) -> Q:
        """Only active (or inactive) rules."""
        return Q(**{f"{prefix}active": value})


@strawberry_django.filter_type(models.Budget)
class BudgetFilter:
    """Filtering options for budgets."""

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only budgets of this category."""
        return Q(**{f"{prefix}category_id": value})

    @strawberry_django.filter_field
    def active_in(self, value: datetime.date, prefix: str) -> Q:
        """Only budgets that apply in this day's month."""
        first = value.replace(day=1)
        return Q(**{f"{prefix}start_month__lte": first}) & (Q(**{f"{prefix}end_month__isnull": True}) | Q(**{f"{prefix}end_month__gte": first}))


@strawberry_django.filter_type(models.RecurringPayment)
class RecurringPaymentFilter:
    """Filtering options for recurring payments."""

    @strawberry_django.filter_field
    def accounts(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only on these accounts."""
        return _ids(prefix, "account_id", value)

    @strawberry_django.filter_field
    def status(self, value: enums.RecurringStatus, prefix: str) -> Q:
        """Only in this status."""
        return Q(**{f"{prefix}status": value.value})


@strawberry_django.order_type(models.BankAccount)
class BankAccountOrder:
    """Ordering options for accounts."""

    name: auto
    kind: auto
    currency: auto
    created_at: auto

    @strawberry_django.order_field
    def last_synced_at(self, queryset: QuerySet, info: Info, value: strawberry_django.Ordering, prefix: str) -> tuple[QuerySet, list]:
        """By the newest successful sync of any of the account's syncers."""
        return queryset.annotate(_last_synced_at=Max(f"{prefix}syncers__last_synced_at")), [value.resolve("_last_synced_at")]


@strawberry_django.order_type(models.Category)
class CategoryOrder:
    """Ordering options for categories."""

    name: auto
    kind: auto
    created_at: auto


@strawberry_django.order_type(models.RecurringPayment)
class RecurringPaymentOrder:
    """Ordering options for recurring payments (`nextExpected: ASC` is "coming up")."""

    next_expected: auto
    last_seen: auto
    amount: auto
    label: auto


@strawberry_django.order_type(models.Budget)
class BudgetOrder:
    """Ordering options for budgets."""

    amount: auto
    start_month: auto
    created_at: auto


@strawberry_django.filter_type(models.Merchant)
class MerchantFilter:
    """Filtering options for merchants."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these merchants."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field(description="Search by text: a substring of name, description or an alias, or semantic similarity to name and description.")
    def search(self, info: Info, queryset: QuerySet, value: str, prefix: str) -> tuple[QuerySet, Q]:
        lexical = Q(**{f"{prefix}name__icontains": value}) | Q(**{f"{prefix}description__icontains": value}) | Q(**{f"{prefix}aliases__pattern__icontains": (normalize(value) or value)})
        queryset, predicate = hybrid_search(queryset, prefix, value, lexical)
        return queryset.distinct() if not prefix else queryset, predicate

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only merchants with this default category."""
        return Q(**{f"{prefix}category_id": value})

    @strawberry_django.filter_field
    def online(self, value: bool, prefix: str) -> Q:
        """Only online-only (or only physical) merchants."""
        return Q(**{f"{prefix}online": value})

    @strawberry_django.filter_field(description="Only merchants with a located store within `radiusMeters` of a point, nearest first (`distanceMeters` on each).")
    def near(self, info: Info, queryset: QuerySet, value: NearInput, prefix: str) -> tuple[QuerySet, Q]:
        if prefix:
            return queryset, Q()
        distance = geo.nearest_location_meters(value.latitude, value.longitude)
        queryset = queryset.annotate(_distance_meters=distance).order_by(F("_distance_meters").asc(nulls_last=True), "pk")
        return queryset, Q(_distance_meters__lte=value.radius_meters)


@strawberry_django.filter_type(models.MerchantLocation)
class MerchantLocationFilter:
    """Filtering options for merchant locations."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these locations."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def merchant(self, value: strawberry.ID, prefix: str) -> Q:
        """Only locations of this merchant."""
        return Q(**{f"{prefix}merchant_id": value})

    @strawberry_django.filter_field
    def city(self, value: str, prefix: str) -> Q:
        """Only locations in this city (case-insensitive)."""
        return Q(**{f"{prefix}city__iexact": value})

    @strawberry_django.filter_field
    def unlocated(self, value: bool, prefix: str) -> Q:
        """Only locations without (or with) coordinates — e.g. discovered stores still to geocode."""
        return Q(**{f"{prefix}latitude__isnull": value}) if value else Q(**{f"{prefix}latitude__isnull": False})

    @strawberry_django.filter_field(description="Only locations within `radiusMeters` of a point, nearest first (`distanceMeters` on each).")
    def near(self, info: Info, queryset: QuerySet, value: NearInput, prefix: str) -> tuple[QuerySet, Q]:
        annotations, where = geo.near(prefix, "point", value.latitude, value.longitude, value.radius_meters)
        return queryset.annotate(**annotations).order_by("_distance_meters", "pk"), Q(**where)

    @strawberry_django.filter_field(description="Only located places inside a map viewport.")
    def within(self, info: Info, queryset: QuerySet, value: BoundsInput, prefix: str) -> tuple[QuerySet, Q]:
        annotations, where = geo.within(prefix, "point", value.south, value.west, value.north, value.east)
        return queryset.annotate(**annotations), Q(**where)

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only places of merchants with this default category."""
        return Q(**{f"{prefix}merchant__category_id": value})


@strawberry_django.order_type(models.Merchant)
class MerchantOrder:
    """Ordering options for merchants."""

    name: auto
    created_at: auto


@strawberry_django.filter_type(models.MerchantRule)
class MerchantRuleFilter:
    """Filtering options for merchant rules."""

    @strawberry_django.filter_field
    def merchant(self, value: strawberry.ID, prefix: str) -> Q:
        """Only rules mapping to this merchant."""
        return Q(**{f"{prefix}merchant_id": value})

    @strawberry_django.filter_field
    def active(self, value: bool, prefix: str) -> Q:
        """Only active (or inactive) rules."""
        return Q(**{f"{prefix}active": value})
