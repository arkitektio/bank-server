"""The one interface every provider kind implements.

A *provider* is a row an organization's admin created (:class:`~finance.models.BankProvider`):
its kind, its settings and credentials, and the capabilities switched on for it. A *backend* is
the code of a kind, built from such a row. Linking, syncing and revoking only ever talk to a
:class:`ProviderBackend`, so a new kind is one module implementing it plus one entry in
:mod:`finance.providers.registry`.

What a kind *can* do is its ``capabilities``; what an instance *does* is the subset stored on
its row. A backend asks :meth:`ProviderBackend.has` before fetching or storing a part.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol

from finance import models

if TYPE_CHECKING:
    from authentikate.models import Organization, User

    from finance.sync import SyncResult

Finish = Literal["REDIRECT", "POLL"]
PsuHeaders = dict[str, str]


@dataclass(frozen=True)
class CapabilityInfo:
    """A capability, as shown to whoever switches it on."""

    capability: models.ProviderCapability
    label: str
    description: str


CAPABILITIES: dict[models.ProviderCapability, CapabilityInfo] = {
    info.capability: info
    for info in (
        CapabilityInfo(models.ProviderCapability.TRANSACTIONS, "Transactions", "Fetch and store the accounts' transactions."),
        CapabilityInfo(models.ProviderCapability.BALANCES, "Balances", "Store the balance the provider reports on every sync."),
        CapabilityInfo(models.ProviderCapability.HOLDINGS, "Holdings", "Store a depot's positions on every sync."),
        CapabilityInfo(models.ProviderCapability.PRICES, "Security prices", "Use the provider's logins as a source of security prices."),
        CapabilityInfo(models.ProviderCapability.SCHEDULED_SYNC, "Scheduled sync", "Let the unattended `sync_all_accounts` action sync its accounts; off, they only sync when a user asks."),
    )
}

#: The capabilities that put data into an account; a provider with none of them has nothing to sync.
DATA_CAPABILITIES = frozenset({models.ProviderCapability.TRANSACTIONS, models.ProviderCapability.BALANCES, models.ProviderCapability.HOLDINGS})


@dataclass(frozen=True)
class LinkRequest:
    """What a user chose when starting a link. A kind ignores what it has no use for."""

    institution: str | None = None
    country: str | None = None
    redirect_url: str | None = None


@dataclass(frozen=True)
class LinkCompletion:
    """What the client brings back to finish a link (``code``: REDIRECT kinds only)."""

    code: str | None = None


@dataclass(frozen=True)
class Institution:
    """A bank a provider can reach."""

    name: str
    country: str
    logo: str | None = None
    bic: str | None = None
    maximum_consent_days: int | None = None


@dataclass(frozen=True)
class ProviderCheck:
    """What the provider itself says about a set of credentials."""

    name: str | None = None
    environment: str | None = None
    active: bool = True
    redirect_urls: list[str] = field(default_factory=list)


AuthStatus = Literal["PENDING", "DONE", "FAILED", "EXPIRED", "CANCELLED"]


@dataclass(frozen=True)
class AuthResult:
    """What a login linked, as a Structure the client can open."""

    identifier: str
    id: str
    label: str | None = None


@dataclass
class AuthSession:
    """A login, described the same way for every kind.

    A kind's ``describe`` fills in how its login is opened and finished; where the login is
    (``status`` and what follows from it) is the same for all and set by :mod:`finance.auth_sessions`.
    """

    connection: models.BankConnection
    state: str
    open_url: str
    expires_at: datetime
    finish: Finish
    interval: int | None = None
    user_code: str | None = None
    redirect_url: str | None = None
    status: AuthStatus = "PENDING"
    step: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result: AuthResult | None = None


class ProviderBackend(Protocol):
    """The code of one provider kind, built from a :class:`~finance.models.BankProvider` row."""

    kind: ClassVar[models.Provider]
    label: ClassVar[str]
    description: ClassVar[str]
    #: How a started login finishes.
    finish: ClassVar[Finish]
    #: Everything this kind can do; an instance enables a subset.
    capabilities: ClassVar[frozenset[models.ProviderCapability]]
    #: Whether a link is to one of many banks the user picks (see :meth:`institutions`).
    has_institutions: ClassVar[bool]
    #: Syncs per account per UTC day a new instance starts with (None: unlimited).
    default_daily_sync_limit: ClassVar[int | None]

    provider: models.BankProvider

    def __init__(self, provider: models.BankProvider) -> None: ...

    def has(self, capability: models.ProviderCapability) -> bool:
        """Whether this instance has ``capability`` switched on."""
        ...

    async def verify(self) -> ProviderCheck:
        """Prove the stored credentials work, by asking the provider."""
        ...

    async def institutions(self, country: str) -> list[Institution]:
        """The banks that can be linked in a country (empty for a kind without institutions)."""
        ...

    async def start_link(self, organization: "Organization", creator: "User", request: LinkRequest) -> models.BankConnection:
        """Start a login; returns the stored PENDING connection."""
        ...

    async def complete_link(self, connection: models.BankConnection, completion: LinkCompletion) -> models.BankConnection:
        """Finish (REDIRECT) or advance (POLL) the PENDING login of ``connection``; returns the row as it is afterwards.

        A login the provider definitely ended is stored as FAILED and returned, not raised. Only
        what leaves it PENDING (the provider could not be reached) is raised.
        """
        ...

    async def revoke(self, connection: models.BankConnection) -> None:
        """Withdraw the consent or login at the provider, best effort; the caller stores the outcome."""
        ...

    @classmethod
    def describe(cls, connection: models.BankConnection) -> AuthSession:
        """The auth session of a pending connection of this kind, read from the stored row."""
        ...

    async def sync(self, syncer: models.AccountSyncer, since: date | None, psu_headers: PsuHeaders | None) -> "SyncResult":
        """Fetch one syncer and store it (the caller holds the lease and records failures)."""
        ...


class BackendBase:
    """What every backend shares: the row it was built from and its enabled capabilities."""

    capabilities: ClassVar[frozenset[models.ProviderCapability]]

    def __init__(self, provider: models.BankProvider) -> None:
        self.provider = provider
        self._enabled = frozenset(models.ProviderCapability(value) for value in provider.capabilities) & self.capabilities

    def has(self, capability: models.ProviderCapability) -> bool:
        """Whether this instance has ``capability`` switched on."""
        return capability in self._enabled
