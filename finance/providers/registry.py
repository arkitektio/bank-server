"""Which code runs a provider kind. A new kind is one more entry in :data:`KINDS`."""

from pydantic import BaseModel

from finance import models
from finance.providers.base import ProviderBackend
from finance.providers.enablebanking import EnableBankingBackend, EnableBankingSettings
from finance.providers.errors import NotConfigured
from finance.providers.scalable import ScalableBackend, ScalableSettings

KINDS: dict[models.Provider, type[ProviderBackend]] = {
    models.Provider.ENABLEBANKING: EnableBankingBackend,
    models.Provider.SCALABLE: ScalableBackend,
}

#: What a kind's ``settings`` column holds.
SETTINGS: dict[models.Provider, type[BaseModel]] = {
    models.Provider.ENABLEBANKING: EnableBankingSettings,
    models.Provider.SCALABLE: ScalableSettings,
}


def kind_of(kind: str) -> type[ProviderBackend]:
    """The backend class of a kind."""
    return KINDS[models.Provider(kind)]


def backend_for(provider: models.BankProvider) -> ProviderBackend:
    """The backend of a provider row."""
    return kind_of(provider.kind)(provider)


def usable(provider: models.BankProvider | None, what: str) -> ProviderBackend:
    """The backend of a provider that may be used for new work; :class:`NotConfigured` when there is none or it is disabled."""
    if provider is None:
        raise NotConfigured(f"{what} has no provider: an admin has to set one up (again) before it can be used.")
    if not provider.enabled:
        raise NotConfigured(f"The provider {provider.name!r} is disabled.")
    return backend_for(provider)


async def provider_of(connection: models.BankConnection) -> models.BankProvider | None:
    """The provider row a connection goes through (None: not attached to one)."""
    if connection.bank_provider_id is None:
        return None
    return await models.BankProvider.objects.aget(id=connection.bank_provider_id)
