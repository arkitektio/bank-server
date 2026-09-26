"""Merchants, their aliases and locations, and attaching them to transactions.

Every edit that changes what matches or categorizes (aliases, a merchant's category, merging,
assigning) re-runs the shared pipeline (:func:`finance.sync.categorize_rows`: match merchants,
rules, merchant categories, suggestions) over the affected rows, inside this request.
"""

from decimal import Decimal
from typing import Optional

import strawberry
from django.db import IntegrityError, transaction as db_transaction
from django.utils import timezone
from kante.errors import NotFound, ValidationError
from kante.types import Info

from finance import enums, geocoding, merchants, models, types
from finance.graphql.errors import translate
from finance.graphql.utils import get_many, get_or_404
from finance.scoping import for_org
from finance.textnorm import normalize

__all__ = [
    "CreateMerchantInput",
    "UpdateMerchantInput",
    "MerchantLocationInput",
    "UpdateMerchantLocationInput",
    "create_merchant",
    "update_merchant",
    "delete_merchant",
    "merge_merchants",
    "add_merchant_alias",
    "remove_merchant_alias",
    "create_merchant_location",
    "update_merchant_location",
    "delete_merchant_location",
    "geocode_merchant_location",
    "assign_merchant",
    "upsert_merchant",
    "MerchantRef",
    "LocationRef",
    "AssignMerchantInput",
    "UpsertMerchantInput",
    "CreateMerchantRuleInput",
    "UpdateMerchantRuleInput",
    "create_merchant_rule",
    "update_merchant_rule",
    "delete_merchant_rule",
]


def _refresh(organization_id: int, transaction_ids: list[int]) -> None:
    from finance.sync import categorize_rows

    if transaction_ids:
        with db_transaction.atomic():
            categorize_rows(organization_id, transaction_ids)


def _pattern(text: str) -> str:
    pattern = normalize(text)
    if not pattern:
        raise ValidationError(f"{text!r} has no words to match by (numbers and boilerplate like 'DANKT' are ignored).")
    return pattern


def _add_aliases(merchant: models.Merchant, patterns: list[str]) -> None:
    taken = dict(
        models.MerchantAlias.objects.filter(organization_id=merchant.organization_id, pattern__in=patterns).exclude(merchant=merchant).values_list("pattern", "merchant__name")
    )
    if taken:
        raise ValidationError("; ".join(f"{pattern!r} already means {name!r}" for pattern, name in taken.items()))
    models.MerchantAlias.objects.bulk_create([models.MerchantAlias(organization_id=merchant.organization_id, merchant=merchant, pattern=p) for p in patterns], ignore_conflicts=True)


@strawberry.input(description="A merchant, by `id` or by `key` (its stable normalized name, e.g. \"spar\") — give exactly one.")
class MerchantRef:
    id: Optional[strawberry.ID] = None
    key: Optional[str] = strawberry.field(default=None, description="The merchant's key; normalized like an alias (\"Spar\" → \"spar\").")


@strawberry.input(description="A place of the merchant, by `id` or by `storeCode` (the store number bank lines carry) — give exactly one.")
class LocationRef:
    id: Optional[strawberry.ID] = None
    store_code: Optional[str] = None


def resolve_merchant(info: Info, ref: MerchantRef) -> models.Merchant:
    """The organization's merchant a ref names; NOT_FOUND otherwise (another organization's too)."""
    if (ref.id is None) == (ref.key is None):
        raise ValidationError("Give a merchant's `id` or its `key`, not both.")
    if ref.id is not None:
        return get_or_404(models.Merchant, info, ref.id)
    key = normalize(ref.key) or ""
    found = for_org(models.Merchant, info).filter(key=key).first()
    if found is None:
        raise NotFound(f"No merchant with key {key!r}.")
    return found


def resolve_location(info: Info, merchant: models.Merchant, ref: LocationRef, create: bool) -> models.MerchantLocation:
    """The merchant's place a ref names; a store number that is new is created (DISCOVERED) when ``create``."""
    if (ref.id is None) == (ref.store_code is None):
        raise ValidationError("Give a location's `id` or its `storeCode`, not both.")
    if ref.id is not None:
        place = get_or_404(models.MerchantLocation, info, ref.id)
        if place.merchant_id != merchant.id:
            raise ValidationError("The location must belong to the merchant given.")
        return place
    code = ref.store_code.strip()
    place = models.MerchantLocation.objects.filter(merchant=merchant, store_code=code).first()
    if place is None:
        if not create:
            raise NotFound(f"{merchant.name!r} has no place with store number {code!r}.")
        place = models.MerchantLocation.objects.get(id=merchants._store(merchant.id, code))
    return place


def _category(info: Info, category: Optional[strawberry.ID], category_key: Optional[str]) -> models.Category | None:
    if category and category_key:
        raise ValidationError("Give `category` or `categoryKey`, not both.")
    if category_key:
        found = for_org(models.Category, info).filter(key=category_key).first()
        if found is None:
            raise NotFound(f"No category with key {category_key!r}.")
        return found
    return get_or_404(models.Category, info, category) if category else None


@strawberry.input(description="A new merchant. Aliases default to the match keys of `fromTransactions` (else the name).")
class CreateMerchantInput:
    name: str
    key: Optional[str] = strawberry.field(default=None, description="The stable key to link by; the normalized name by default. It never changes on rename.")
    description: str = ""
    website: Optional[str] = None
    logo_url: Optional[str] = None
    category: Optional[strawberry.ID] = strawberry.field(default=None, description="Default category of its transactions.")
    category_key: Optional[str] = strawberry.field(default=None, description="…or the default category by its base key (\"food.groceries\").")
    online: bool = False
    aliases: Optional[list[str]] = strawberry.field(default=None, description="Texts that mean this merchant on a bank line; normalized (\"Spar Dankt\" → \"spar\").")
    from_transactions: Optional[list[strawberry.ID]] = strawberry.field(default=None, description="Transactions to derive aliases from and attach (e.g. a `merchantCandidates` entry's `transactionIds`).")


@strawberry.input(description="Changes to a merchant; omitted fields stay as they are.")
class UpdateMerchantInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.field(default=strawberry.UNSET, description="A new display name; the `key` stays (links by key keep working).")
    key: Optional[str] = strawberry.field(default=strawberry.UNSET, description="A new key — only when you mean to change what clients link by.")
    description: Optional[str] = strawberry.UNSET
    website: Optional[str] = strawberry.UNSET
    logo_url: Optional[str] = strawberry.UNSET
    category: Optional[strawberry.ID] = strawberry.field(default=strawberry.UNSET, description="The default category, or null for none; its transactions follow.")
    category_key: Optional[str] = strawberry.field(default=strawberry.UNSET, description="…or the default category by its base key.")
    online: Optional[bool] = strawberry.UNSET


def create_merchant(info: Info, input: CreateMerchantInput) -> types.Merchant:
    """Create a merchant, match its aliases over every transaction, and categorize what it now covers."""
    organization = info.context.request.organization
    category = _category(info, input.category, input.category_key)
    seeds = get_many(models.Transaction, info, input.from_transactions) if input.from_transactions else []
    patterns = [_pattern(a) for a in input.aliases] if input.aliases else (merchants.keys_of(seeds) or [_pattern(input.name)])
    with db_transaction.atomic():
        try:
            with db_transaction.atomic():
                merchant = models.Merchant.objects.create(
                    organization=organization, name=input.name.strip(), key=_pattern(input.key or input.name), description=input.description.strip(),
                    website=input.website, logo_url=input.logo_url, category=category, online=input.online,
                )
        except IntegrityError:
            raise ValidationError(f"A merchant with key {_pattern(input.key or input.name)!r} already exists (use upsertMerchant to update it).")
        _add_aliases(merchant, list(dict.fromkeys(patterns)))
        _refresh(organization.id, merchants.organization_rows(organization.id))
        # Seeds the aliases do not cover (explicit aliases given) are attached by hand.
        loose = [tx.id for tx in seeds if not models.Transaction.objects.filter(id=tx.id, merchant=merchant).exists()]
        if loose:
            models.Transaction.objects.filter(id__in=loose).update(merchant=merchant, merchant_source=models.MerchantSource.MANUAL)
            _refresh(organization.id, loose)
    return merchant  # type: ignore[return-value]


def update_merchant(info: Info, input: UpdateMerchantInput) -> types.Merchant:
    """Change a merchant. A new default category re-categorizes its transactions (never pinned ones)."""
    merchant = get_or_404(models.Merchant, info, input.id)
    return _apply_update(info, merchant, input)


def _apply_update(info: Info, merchant: models.Merchant, input) -> models.Merchant:  # noqa: ANN001 - Update/UpsertMerchantInput
    recategorize = False
    if input.name is not strawberry.UNSET and input.name:
        merchant.name = input.name.strip()
    if getattr(input, "key", strawberry.UNSET) not in (strawberry.UNSET, None) and not isinstance(input, UpsertMerchantInput):
        merchant.key = _pattern(input.key)
    for field in ("description", "website", "logo_url", "online"):
        value = getattr(input, field)
        if value is not strawberry.UNSET and not (field in ("description", "online") and value is None):
            setattr(merchant, field, value.strip() if isinstance(value, str) and field == "description" else value)
    if input.category is not strawberry.UNSET or input.category_key is not strawberry.UNSET:
        category = input.category if input.category is not strawberry.UNSET else None
        category_key = input.category_key if input.category_key is not strawberry.UNSET else None
        merchant.category = _category(info, category, category_key)
        recategorize = True
    try:
        with db_transaction.atomic():
            merchant.save()
    except IntegrityError:
        raise ValidationError(f"A merchant with key {merchant.key!r} already exists.")
    rows = merchants.organization_rows(merchant.organization_id, merchant.id)
    if recategorize:
        _refresh(merchant.organization_id, rows)
    else:
        merchants.refresh_context(merchant.organization_id, rows)  # its name or description is part of their embedding
    return merchant  # type: ignore[return-value]


@strawberry.input(description="Create a merchant, or update the one with this key. Omitted fields keep their value on update; `aliases` are added (never removed).")
class UpsertMerchantInput:
    key: str = strawberry.field(description="What the merchant is found by; normalized (\"McDonald's\" → \"mcdonald\").")
    name: Optional[str] = strawberry.field(default=strawberry.UNSET, description="Display name; the key's text when creating without one.")
    description: Optional[str] = strawberry.UNSET
    website: Optional[str] = strawberry.UNSET
    logo_url: Optional[str] = strawberry.UNSET
    category: Optional[strawberry.ID] = strawberry.UNSET
    category_key: Optional[str] = strawberry.UNSET
    online: Optional[bool] = strawberry.UNSET
    aliases: Optional[list[str]] = strawberry.field(default=None, description="Texts to add as aliases (the key itself is always one).")


def upsert_merchant(info: Info, input: UpsertMerchantInput) -> types.Merchant:
    """Create or update the merchant with this key, add its aliases, and match and categorize everything it now covers."""
    organization = info.context.request.organization
    key = _pattern(input.key)
    with db_transaction.atomic():
        merchant = models.Merchant.objects.select_for_update().filter(organization=organization, key=key).first()
        if merchant is None:
            merchant = models.Merchant.objects.create(organization=organization, key=key, name=(input.name or input.key).strip())
        _apply_update(info, merchant, input)
        _add_aliases(merchant, list(dict.fromkeys([key, *(_pattern(a) for a in (input.aliases or []))])))
        _refresh(organization.id, merchants.organization_rows(organization.id))
    merchant.refresh_from_db()
    return merchant  # type: ignore[return-value]


def delete_merchant(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Delete a merchant with its aliases and locations. Its transactions lose it (and its category) and get categorized again."""
    merchant = get_or_404(models.Merchant, info, id)
    organization_id = merchant.organization_id
    with db_transaction.atomic():
        ids = merchants.organization_rows(organization_id, merchant.id)
        models.Transaction.objects.filter(id__in=ids).update(merchant=None, merchant_location=None, merchant_source=models.MerchantSource.NONE)
        merchant.delete()
        _refresh(organization_id, ids)
    return id


def merge_merchants(info: Info, merchant: strawberry.ID, into: strawberry.ID) -> types.Merchant:
    """Fold `merchant` into `into`: its aliases, locations (same store number → one location) and transactions move; `merchant` is deleted."""
    src = get_or_404(models.Merchant, info, merchant)
    dst = get_or_404(models.Merchant, info, into)
    if src.id == dst.id:
        raise ValidationError("A merchant cannot be merged into itself.")
    with db_transaction.atomic():
        models.MerchantAlias.objects.filter(merchant=src).update(merchant=dst)
        target_codes = dict(models.MerchantLocation.objects.filter(merchant=dst, store_code__isnull=False).values_list("store_code", "id"))
        for location in models.MerchantLocation.objects.filter(merchant=src):
            if location.store_code and location.store_code in target_codes:
                models.Transaction.objects.filter(merchant_location=location).update(merchant_location_id=target_codes[location.store_code])
                location.delete()
            else:
                location.merchant = dst
                location.save(update_fields=["merchant"])
        models.Transaction.objects.filter(merchant=src).update(merchant=dst)
        src.delete()
        _refresh(dst.organization_id, merchants.organization_rows(dst.organization_id, dst.id))
    return dst  # type: ignore[return-value]


def add_merchant_alias(info: Info, merchant: strawberry.ID, text: str) -> types.MerchantAlias:
    """Teach a merchant another spelling; every transaction is matched again."""
    target = get_or_404(models.Merchant, info, merchant)
    pattern = _pattern(text)
    with db_transaction.atomic():
        _add_aliases(target, [pattern])
        _refresh(target.organization_id, merchants.organization_rows(target.organization_id))
    return models.MerchantAlias.objects.get(organization_id=target.organization_id, pattern=pattern)  # type: ignore[return-value]


def remove_merchant_alias(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Forget a spelling; transactions it matched are matched again (and may lose their merchant)."""
    alias = get_or_404(models.MerchantAlias, info, id)
    organization_id = alias.organization_id
    with db_transaction.atomic():
        alias.delete()
        _refresh(organization_id, merchants.organization_rows(organization_id))
    return id


@strawberry.input(description="A new place of a merchant.")
class MerchantLocationInput:
    merchant: strawberry.ID
    name: str
    store_code: Optional[str] = strawberry.field(default=None, description="The store number bank lines carry for this place.")
    street: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    notes: str = ""


@strawberry.input(description="Changes to a place; omitted fields stay as they are.")
class UpdateMerchantLocationInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    store_code: Optional[str] = strawberry.UNSET
    street: Optional[str] = strawberry.UNSET
    postal_code: Optional[str] = strawberry.UNSET
    city: Optional[str] = strawberry.UNSET
    region: Optional[str] = strawberry.UNSET
    country: Optional[str] = strawberry.UNSET
    latitude: Optional[Decimal] = strawberry.UNSET
    longitude: Optional[Decimal] = strawberry.UNSET
    notes: Optional[str] = strawberry.UNSET


_ADDRESS = ("name", "store_code", "street", "postal_code", "city", "region", "country", "latitude", "longitude", "notes")


def _check_coordinates(location: models.MerchantLocation) -> None:
    if (location.latitude is None) != (location.longitude is None):
        raise ValidationError("Give both latitude and longitude, or neither.")
    if location.latitude is not None and not (-90 <= location.latitude <= 90 and -180 <= location.longitude <= 180):  # type: ignore[operator]
        raise ValidationError("Latitude must be within ±90 and longitude within ±180.")
    if location.country:
        location.country = location.country.upper()


def _save_location(location: models.MerchantLocation) -> models.MerchantLocation:
    _check_coordinates(location)
    try:
        with db_transaction.atomic():
            location.save()
    except IntegrityError:
        raise ValidationError(f"This merchant already has a place with store number {location.store_code!r}.")
    location.refresh_from_db()
    return location


def create_merchant_location(info: Info, input: MerchantLocationInput) -> types.MerchantLocation:
    """Add a place to a merchant (by hand)."""
    merchant = get_or_404(models.Merchant, info, input.merchant)
    location = models.MerchantLocation(merchant=merchant, source=models.LocationSource.MANUAL, **{f: getattr(input, f) for f in _ADDRESS})
    return _save_location(location)  # type: ignore[return-value]


def update_merchant_location(info: Info, input: UpdateMerchantLocationInput) -> types.MerchantLocation:
    """Correct a place (e.g. fill in the address of a discovered store); it becomes MANUAL."""
    location = get_or_404(models.MerchantLocation, info, input.id)
    for field in _ADDRESS:
        value = getattr(input, field)
        if value is not strawberry.UNSET and not (field in ("name", "notes") and value is None):
            setattr(location, field, value)
    location.source = models.LocationSource.MANUAL
    saved = _save_location(location)
    _refresh_place(saved)
    return saved  # type: ignore[return-value]


def _refresh_place(location: models.MerchantLocation) -> None:
    """The place's name and city are part of its transactions' embedding."""
    organization_id = location.merchant.organization_id
    merchants.refresh_context(organization_id, list(location.transactions.values_list("id", flat=True)))


def delete_merchant_location(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Delete a place; its transactions keep their merchant."""
    get_or_404(models.MerchantLocation, info, id).delete()
    return id


async def geocode_merchant_location(info: Info, id: strawberry.ID, query: Optional[str] = None) -> types.MerchantLocation:
    """Look the place up (OpenStreetMap) and fill in its address and coordinates (source GEOCODED).

    Without `query` the place's own address is searched, else "merchant name, city". The lookup
    happens in this request; nothing geocodes on its own.
    """
    from channels.db import database_sync_to_async

    location = await database_sync_to_async(lambda: get_or_404(models.MerchantLocation, info, id))()
    merchant_name = await database_sync_to_async(lambda: location.merchant.name)()
    try:
        if query:
            places = await geocoding.search(query, limit=1)
        elif location.street or location.postal_code:
            places = await geocoding.search(street=location.street, postal_code=location.postal_code, city=location.city, country=location.country, limit=1)
        else:
            places = await geocoding.search(", ".join(part for part in (merchant_name, location.city, location.country) if part), limit=1)
    except Exception as error:
        raise translate(error) from error
    if not places:
        raise ValidationError("The geocoder found no place for this; pass a `query` (e.g. an address).")
    place = places[0]
    location.street, location.postal_code, location.city = place.street, place.postal_code, place.city
    location.region, location.country, location.latitude, location.longitude = place.region, place.country, place.latitude, place.longitude
    location.osm_id, location.geocoded_at, location.source = place.osm_id, timezone.now(), models.LocationSource.GEOCODED
    saved = await database_sync_to_async(_save_location)(location)
    await database_sync_to_async(_refresh_place)(saved)
    return saved  # type: ignore[return-value]


@strawberry.input(description="Link transactions to a merchant (and place) by hand — MANUAL, never re-matched. A null `merchant` hands them back to alias matching.")
class AssignMerchantInput:
    transactions: list[strawberry.ID]
    merchant: Optional[MerchantRef] = strawberry.field(default=None, description="By id or key; null clears the manual link.")
    location: Optional[LocationRef] = strawberry.field(default=None, description="By id or store number (created if new, see `createLocation`).")
    create_location: bool = strawberry.field(default=True, description="Create the place when `location.storeCode` is new for the merchant.")


def assign_merchant(info: Info, input: AssignMerchantInput) -> list[types.Transaction]:
    """Set the merchant (and place) of many transactions — by merchant id or key, place id or store number."""
    rows = get_many(models.Transaction, info, input.transactions)
    selected = [tx.id for tx in rows]
    organization_id = info.context.request.organization.id
    if input.merchant is None and input.location is not None:
        raise ValidationError("A location needs its merchant.")
    with db_transaction.atomic():
        if input.merchant is not None:
            target = resolve_merchant(info, input.merchant)
            place = resolve_location(info, target, input.location, input.create_location) if input.location is not None else None
            models.Transaction.objects.filter(id__in=selected).update(merchant=target, merchant_location=place, merchant_source=models.MerchantSource.MANUAL)
        else:
            models.Transaction.objects.filter(id__in=selected).update(merchant=None, merchant_location=None, merchant_source=models.MerchantSource.NONE)
        _refresh(organization_id, selected)
    order = {tx.id: i for i, tx in enumerate(rows)}
    return sorted(models.Transaction.objects.filter(id__in=selected), key=lambda tx: order[tx.id])  # type: ignore[return-value]



# --- merchant rules ------------------------------------------------------------------------------


@strawberry.input(description="A new merchant rule: matching transactions get the merchant (before aliases; never over a manual link).")
class CreateMerchantRuleInput:
    merchant: MerchantRef
    field: enums.RuleField
    pattern: str
    match: enums.RuleMatch = enums.RuleMatch.CONTAINS
    direction: enums.RuleDirection = enums.RuleDirection.ANY
    priority: int = strawberry.field(default=100, description="Lower runs first; the first matching rule wins.")
    amount_min: Optional[Decimal] = None
    amount_max: Optional[Decimal] = None
    location: Optional[LocationRef] = strawberry.field(default=None, description="Pin a place (by id or store number, created if new).")
    active: bool = True
    apply: bool = strawberry.field(default=True, description="Re-match existing transactions right away.")


@strawberry.input(description="Changes to a merchant rule; omitted fields stay as they are.")
class UpdateMerchantRuleInput:
    id: strawberry.ID
    merchant: Optional[MerchantRef] = strawberry.UNSET
    field: Optional[enums.RuleField] = strawberry.UNSET
    pattern: Optional[str] = strawberry.UNSET
    match: Optional[enums.RuleMatch] = strawberry.UNSET
    direction: Optional[enums.RuleDirection] = strawberry.UNSET
    priority: Optional[int] = strawberry.UNSET
    amount_min: Optional[Decimal] = strawberry.UNSET
    amount_max: Optional[Decimal] = strawberry.UNSET
    location: Optional[LocationRef] = strawberry.field(default=strawberry.UNSET, description="A place to pin, or null to let the store number decide.")
    active: Optional[bool] = strawberry.UNSET
    apply: bool = True


def _rematch_all(organization_id: int) -> None:
    _refresh(organization_id, merchants.organization_rows(organization_id))


def create_merchant_rule(info: Info, input: CreateMerchantRuleInput) -> types.MerchantRule:
    """Create a rule mapping matching transactions to a merchant (e.g. an IBAN, or a remittance text)."""
    from finance.graphql.mutations.categories import _validate

    merchant = resolve_merchant(info, input.merchant)
    rule = models.MerchantRule(
        organization=info.context.request.organization,
        merchant=merchant,
        location=resolve_location(info, merchant, input.location, True) if input.location else None,
        field=input.field.value,
        pattern=input.pattern,
        match=input.match.value,
        direction=input.direction.value,
        priority=input.priority,
        amount_min=input.amount_min,
        amount_max=input.amount_max,
        active=input.active,
    )
    _validate(rule)  # type: ignore[arg-type]
    rule.save()
    if input.apply:
        _rematch_all(rule.organization_id)
    return rule  # type: ignore[return-value]


def update_merchant_rule(info: Info, input: UpdateMerchantRuleInput) -> types.MerchantRule:
    """Change a merchant rule."""
    from finance.graphql.mutations.categories import _validate

    rule = get_or_404(models.MerchantRule, info, input.id)
    if input.merchant is not strawberry.UNSET and input.merchant is not None:
        rule.merchant = resolve_merchant(info, input.merchant)
        rule.location = None
    for name in ("field", "match", "direction"):
        value = getattr(input, name)
        if value is not strawberry.UNSET and value is not None:
            setattr(rule, name, value.value)
    for name in ("pattern", "priority", "active"):
        value = getattr(input, name)
        if value is not strawberry.UNSET and value is not None:
            setattr(rule, name, value)
    for name in ("amount_min", "amount_max"):
        value = getattr(input, name)
        if value is not strawberry.UNSET:
            setattr(rule, name, value)
    if input.location is not strawberry.UNSET:
        rule.location = resolve_location(info, rule.merchant, input.location, True) if input.location else None
    _validate(rule)  # type: ignore[arg-type]
    rule.save()
    if input.apply:
        _rematch_all(rule.organization_id)
    return rule  # type: ignore[return-value]


def delete_merchant_rule(info: Info, id: strawberry.ID, apply: bool = True) -> strawberry.ID:
    """Delete a merchant rule. With `apply`, what it alone linked is matched again (aliases may still claim it)."""
    rule = get_or_404(models.MerchantRule, info, id)
    organization_id = rule.organization_id
    rule.delete()
    if apply:
        _rematch_all(organization_id)
    return id
