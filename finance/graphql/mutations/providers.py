"""Setting up the organization's providers. Admins only (the schema gates every mutation here).

A provider's credentials only ever travel inwards: a private key is taken as text, checked
against the provider, stored encrypted, and never returned.
"""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from django.db import IntegrityError
from kante.errors import ValidationError
from kante.types import Info
from pydantic import ValidationError as SettingsError

from finance import crypto, enums, models, types
from finance.graphql.errors import translate as _translate
from finance.graphql.utils import aget_or_404
from finance.providers.enablebanking import EnableBankingSettings, InvalidKey, read_key
from finance.providers.errors import ProviderError
from finance.providers.registry import backend_for, kind_of

__all__ = [
    "CreateEnableBankingProviderInput",
    "UpdateEnableBankingProviderInput",
    "CreateScalableProviderInput",
    "UpdateProviderInput",
    "create_enable_banking_provider",
    "update_enable_banking_provider",
    "create_scalable_provider",
    "update_provider",
    "delete_provider",
]


@strawberry.input(description="A new Enable Banking provider: one application from the Enable Banking control panel.")
class CreateEnableBankingProviderInput:
    name: str = strawberry.field(description="What to call it, unique in the organization.")
    app_id: str = strawberry.field(description="The application id.")
    private_key: str = strawberry.field(description="The application's private key: the text of its `.pem` file. Stored encrypted and never returned.")
    redirect_urls: Optional[list[str]] = strawberry.field(default=None, description="The redirect URLs links may use, the default first. Omitted: those registered for the application at Enable Banking.")
    consent_days: int = strawberry.field(default=90, description="How long a new consent is requested for, in days.")
    psu_type: str = strawberry.field(default="personal", description="`personal` or `business`.")
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.field(default=None, description="What to switch on; everything the kind can do by default (see `providerKinds`).")
    daily_sync_limit: Optional[int] = strawberry.field(default=strawberry.UNSET, description="Syncs per account per day; null is unlimited. Omitted: the kind's default.")


@strawberry.input(description="Changes to an Enable Banking provider; omitted fields stay as they are.")
class UpdateEnableBankingProviderInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.UNSET
    app_id: Optional[str] = strawberry.UNSET
    private_key: Optional[str] = strawberry.field(default=strawberry.UNSET, description="A new private key (PEM text). Omitted or empty: the stored key is kept.")
    redirect_urls: Optional[list[str]] = strawberry.UNSET
    consent_days: Optional[int] = strawberry.UNSET
    psu_type: Optional[str] = strawberry.UNSET
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.UNSET
    daily_sync_limit: Optional[int] = strawberry.field(default=strawberry.UNSET, description="Null is unlimited.")


@strawberry.input(description="A new Scalable Capital provider. It holds no credentials: each login keeps its own.")
class CreateScalableProviderInput:
    name: str = strawberry.field(default="Scalable Capital", description="What to call it, unique in the organization.")
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.field(default=None, description="What to switch on; everything the kind can do by default (see `providerKinds`).")
    daily_sync_limit: Optional[int] = strawberry.field(default=strawberry.UNSET, description="Syncs per account per day; null is unlimited. Omitted: the kind's default.")


@strawberry.input(description="Changes any provider takes, whatever its kind; omitted fields stay as they are.")
class UpdateProviderInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.UNSET
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.UNSET
    daily_sync_limit: Optional[int] = strawberry.field(default=strawberry.UNSET, description="Null is unlimited.")


def _capabilities(kind: models.Provider, chosen: list[enums.ProviderCapability] | None) -> list[str]:
    """The chosen capabilities as stored, or everything the kind can do; refuses one the kind lacks."""
    supported = kind_of(kind).capabilities
    if chosen is None:
        return sorted(capability.value for capability in supported)
    unsupported = sorted({c.value for c in chosen} - {c.value for c in supported})
    if unsupported:
        raise ValidationError(f"{kind_of(kind).label} cannot do {', '.join(unsupported)}; see `providerKinds` for what it can.")
    return sorted({c.value for c in chosen})


def _name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValidationError("A provider needs a name.")
    return name


def _apply_common(provider: models.BankProvider, name: object, enabled: object, capabilities: object, daily_sync_limit: object) -> None:
    """Write the fields every kind has onto ``provider`` (unsaved); ``UNSET`` leaves one alone."""
    if isinstance(name, str):
        provider.name = _name(name)
    if isinstance(enabled, bool):
        provider.enabled = enabled
    if isinstance(capabilities, list):
        provider.capabilities = _capabilities(models.Provider(provider.kind), capabilities)
    if daily_sync_limit is not strawberry.UNSET:
        if isinstance(daily_sync_limit, int) and daily_sync_limit < 1:
            raise ValidationError("dailySyncLimit must be at least 1, or null for no limit.")
        provider.daily_sync_limit = daily_sync_limit if isinstance(daily_sync_limit, int) else None


def _settings(**values: object) -> EnableBankingSettings:
    try:
        return EnableBankingSettings.model_validate(values)
    except SettingsError as error:
        problems = "; ".join(f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}" for problem in error.errors())
        raise ValidationError(f"Invalid Enable Banking settings ({problems}).") from error


def _key(pem: str) -> tuple[str, str]:
    """The PEM as stored text and its fingerprint; a validation error for anything but an RSA private key."""
    try:
        data, fingerprint = read_key(pem)
    except InvalidKey as error:
        raise ValidationError(str(error)) from error
    return data.decode(), fingerprint


async def _checked_redirects(provider: models.BankProvider, requested: list[str] | None) -> list[str]:
    """Prove the provider's key and app id at Enable Banking and settle its redirect URLs.

    ``requested`` must all be registered for the application; None takes the registered ones.
    """
    try:
        check = await backend_for(provider).verify()
    except ProviderError as error:
        if 400 <= error.status < 500:
            raise ValidationError(f"Enable Banking does not accept this application id and private key: {error}") from error
        raise _translate(error) from error
    except Exception as error:
        raise _translate(error) from error
    if requested is None:
        requested = check.redirect_urls
    if not requested:
        raise ValidationError("The application has no redirect URL registered at Enable Banking; register one there first.")
    unregistered = [url for url in requested if url not in check.redirect_urls]
    if unregistered:
        raise ValidationError(f"Not registered for this application at Enable Banking: {', '.join(unregistered)}. Registered: {', '.join(check.redirect_urls) or 'none'}.")
    return requested


def _save(provider: models.BankProvider, adopt: bool = False) -> models.BankProvider:
    try:
        provider.save()
    except IntegrityError as error:
        raise ValidationError(f"The organization already has a provider named {provider.name!r}.") from error
    if adopt:
        # Consents made before providers existed (or whose provider was deleted) belong to the
        # organization's only application of that kind: this one.
        only = not models.BankProvider.objects.filter(organization_id=provider.organization_id, kind=provider.kind).exclude(id=provider.id).exists()
        if only:
            models.BankConnection.objects.filter(organization_id=provider.organization_id, provider=provider.kind, bank_provider__isnull=True).update(bank_provider=provider)
    return provider


async def create_enable_banking_provider(info: Info, input: CreateEnableBankingProviderInput) -> types.BankProvider:
    """Set up an Enable Banking application. The key and id are proven at Enable Banking first; nothing is stored when it refuses them.

    The organization's Enable Banking consents that belong to no provider attach to its first one.
    """
    request = info.context.request
    kind = models.Provider.ENABLEBANKING
    pem, fingerprint = _key(input.private_key)
    provider = models.BankProvider(organization=request.organization, creator=request.user, kind=kind, daily_sync_limit=kind_of(kind).default_daily_sync_limit)  # type: ignore[misc]  # kante's request types are authentikate's rows
    _apply_common(provider, input.name, True, strawberry.UNSET, input.daily_sync_limit)
    provider.capabilities = _capabilities(kind, input.capabilities)
    provider.settings = _settings(app_id=input.app_id.strip(), redirect_urls=input.redirect_urls or [], consent_days=input.consent_days, psu_type=input.psu_type, key_fingerprint=fingerprint).model_dump()
    provider.secret = crypto.encrypt(pem)
    redirects = await _checked_redirects(provider, input.redirect_urls)
    provider.settings = {**provider.settings, "redirect_urls": redirects}
    saved: models.BankProvider = await database_sync_to_async(_save)(provider, adopt=True)
    return saved  # type: ignore[return-value]


async def update_enable_banking_provider(info: Info, input: UpdateEnableBankingProviderInput) -> types.BankProvider:
    """Change an Enable Banking provider. A new key, app id or redirect URL is proven at Enable Banking before anything is stored; nothing else asks it."""
    provider = await aget_or_404(models.BankProvider, info, input.id, kind=models.Provider.ENABLEBANKING)
    _apply_common(provider, input.name, input.enabled, input.capabilities, input.daily_sync_limit)
    current = EnableBankingSettings.model_validate(provider.settings)
    values = current.model_dump()
    credentials_changed = False
    if isinstance(input.app_id, str) and input.app_id.strip() != current.app_id:
        values["app_id"] = input.app_id.strip()
        credentials_changed = True
    if isinstance(input.private_key, str) and input.private_key.strip():
        pem, values["key_fingerprint"] = _key(input.private_key)
        provider.secret = crypto.encrypt(pem)
        credentials_changed = True
    if isinstance(input.consent_days, int):
        values["consent_days"] = input.consent_days
    if isinstance(input.psu_type, str):
        values["psu_type"] = input.psu_type
    requested = input.redirect_urls if isinstance(input.redirect_urls, list) else current.redirect_urls
    provider.settings = _settings(**values).model_dump()
    # Enable Banking is only asked when what it vouches for changes: renaming or disabling a
    # provider must work even when its key is no longer accepted there.
    if credentials_changed or requested != current.redirect_urls:
        redirects = await _checked_redirects(provider, requested)
        provider.settings = {**provider.settings, "redirect_urls": redirects}
    saved: models.BankProvider = await database_sync_to_async(_save)(provider)
    return saved  # type: ignore[return-value]


async def create_scalable_provider(info: Info, input: CreateScalableProviderInput) -> types.BankProvider:
    """Let the organization link Scalable Capital. Its existing Scalable logins that belong to no provider attach to its first one."""
    request = info.context.request
    kind = models.Provider.SCALABLE
    provider = models.BankProvider(organization=request.organization, creator=request.user, kind=kind, daily_sync_limit=kind_of(kind).default_daily_sync_limit)  # type: ignore[misc]  # kante's request types are authentikate's rows
    _apply_common(provider, input.name, True, strawberry.UNSET, input.daily_sync_limit)
    provider.capabilities = _capabilities(kind, input.capabilities)
    saved: models.BankProvider = await database_sync_to_async(_save)(provider, adopt=True)
    return saved  # type: ignore[return-value]


async def update_provider(info: Info, input: UpdateProviderInput) -> types.BankProvider:
    """Rename, enable or disable a provider of any kind, or change what is switched on for it."""
    provider = await aget_or_404(models.BankProvider, info, input.id)
    _apply_common(provider, input.name, input.enabled, input.capabilities, input.daily_sync_limit)
    saved: models.BankProvider = await database_sync_to_async(_save)(provider)
    return saved  # type: ignore[return-value]


def _delete(provider: models.BankProvider) -> int:
    live = provider.connections.filter(status__in=[models.ConnectionStatus.ACTIVE, models.ConnectionStatus.PENDING]).count()
    if live:
        raise ValidationError(f"{live} active or pending connection(s) still go through this provider: revoke them first, or disable the provider instead.")
    provider_id = provider.id
    provider.delete()
    return provider_id


async def delete_provider(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Remove a provider and its stored key; returns its id. Refused while it has active or pending connections; accounts and transactions are kept."""
    provider = await aget_or_404(models.BankProvider, info, id)
    return strawberry.ID(str(await database_sync_to_async(_delete)(provider)))
