"""GraphQL types.

Every model type subclasses :class:`OrgScoped`, so each read of it — list, by-id fetch or
nested relation — is limited to the request's organization. Amounts are ``Decimal`` (a string
on the wire), never ``Float`` or ``Int``.
"""

import datetime
from decimal import Decimal
from typing import List, Optional

import kante
import strawberry
import strawberry_django
from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import (
    CharField,
    Count,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    QuerySet,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Cast, Coalesce, JSONObject
from django.utils import timezone
from kante.types import Info
from strawberry.scalars import JSON

from finance import auth_sessions, enums, filters, models
from finance.types._shared import (
    DESCRIPTORS_DESCRIPTION,
    OrgScoped,
    resolve_descriptors,
)
from finance.types.auth import Organization, User


def _model_id() -> str:
    from embeddings import engine

    return engine.model_id()


@strawberry.type(
    description="A capability of a provider kind: something it can do, which an admin switches on or off per provider."
)
class ProviderCapabilityInfo:
    capability: enums.ProviderCapability
    label: str
    description: str


@strawberry.type(
    description="A kind of provider this server can run. What a client needs to offer it, without knowing the kinds itself."
)
class ProviderKind:
    kind: enums.Provider
    label: str
    description: str
    finish: enums.AuthFinish = strawberry.field(
        description="How a login through it finishes."
    )
    capabilities: list[ProviderCapabilityInfo] = strawberry.field(
        description="Everything the kind can do; a provider enables a subset."
    )
    has_institutions: bool = strawberry.field(
        description="True when a link is to one of many banks: pick one from `bankInstitutions` and pass it to `startLink`."
    )
    default_daily_sync_limit: int | None = strawberry.field(
        description="Syncs per account per day a new provider starts with (null: unlimited)."
    )


def provider_kind(kind: str) -> ProviderKind:
    """A kind as its backend class declares it."""
    from finance.providers.base import CAPABILITIES
    from finance.providers.registry import kind_of

    backend = kind_of(kind)
    return ProviderKind(
        kind=enums.Provider(backend.kind.value),
        label=backend.label,
        description=backend.description,
        finish=enums.AuthFinish(backend.finish),
        capabilities=[
            ProviderCapabilityInfo(
                capability=enums.ProviderCapability(info.capability.value),
                label=info.label,
                description=info.description,
            )
            for capability, info in CAPABILITIES.items()
            if capability in backend.capabilities
        ],
        has_institutions=backend.has_institutions,
        default_daily_sync_limit=backend.default_daily_sync_limit,
    )


@strawberry.type(
    description="The settings of an Enable Banking provider. Its private key is never returned."
)
class EnableBankingProviderSettings:
    app_id: str = strawberry.field(description="The Enable Banking application id.")
    redirect_urls: list[str] = strawberry.field(
        description="The redirect URLs a link may use; the first is the default."
    )
    consent_days: int = strawberry.field(
        description="How long a new consent is requested for, in days."
    )
    psu_type: str = strawberry.field(description="`personal` or `business`.")
    key_fingerprint: str = strawberry.field(
        description="A fingerprint of the stored private key's public half, to recognise which key is in use."
    )


@kante.django_type(
    models.BankProvider,
    pagination=True,
    filters=filters.BankProviderFilter,
    description="A provider the organization set up (an Enable Banking application, Scalable Capital). Banks are linked through one; credentials are never returned.",
)
class BankProvider(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    name: str
    kind: enums.Provider
    enabled: bool = strawberry_django.field(
        description="A disabled provider starts no links and syncs nothing."
    )
    daily_sync_limit: int | None = strawberry_django.field(
        description="Syncs per account per day before the service stops asking the provider (null: unlimited)."
    )
    created_at: datetime.datetime
    updated_at: datetime.datetime
    creator: User | None
    organization: Organization
    connections: list["BankConnection"] = strawberry_django.field(
        description="The consents and logins made through it."
    )

    @strawberry_django.field(
        description="The capabilities switched on for this provider."
    )
    def capabilities(self) -> list[enums.ProviderCapability]:
        return [enums.ProviderCapability(value) for value in self.capabilities]  # type: ignore[attr-defined]

    @strawberry_django.field(description="What its kind is and can do.")
    def kind_info(self) -> ProviderKind:
        return provider_kind(self.kind)

    @strawberry_django.field(
        description="Its settings when it is an Enable Banking provider, else null."
    )
    def enable_banking(self) -> EnableBankingProviderSettings | None:
        from finance.providers.enablebanking import EnableBankingSettings

        if self.kind != models.Provider.ENABLEBANKING:
            return None
        values = EnableBankingSettings.model_validate(self.settings)  # type: ignore[attr-defined]
        return EnableBankingProviderSettings(
            app_id=values.app_id,
            redirect_urls=values.redirect_urls,
            consent_days=values.consent_days,
            psu_type=values.psu_type,
            key_fingerprint=values.key_fingerprint,
        )


@strawberry.type(
    description="A Structure: what the login linked, so the app can open its page."
)
class AuthResult:
    identifier: str
    id: strawberry.ID
    label: str | None = None


@strawberry.type(
    description="A login at an external provider, the same shape in every service. Open `openUrl` in the user's browser; then, by `finish`: REDIRECT — the provider redirects to `redirectUrl` with `code` and `state`, call `completeAuth` with both; POLL — call `completeAuth` with the `state` every `interval` seconds until `status` is not PENDING."
)
class AuthSession:
    state: str = strawberry.field(
        description="Opaque, unguessable, single-use, stored server-side. THE handle of the login."
    )
    status: enums.AuthStatus
    finish: enums.AuthFinish
    open_url: str = strawberry.field(
        description="https. What the app opens in the user's browser."
    )
    expires_at: datetime.datetime = strawberry.field(
        description="Until when the first approval can happen."
    )
    redirect_url: str | None = strawberry.field(
        default=None,
        description="REDIRECT: where the provider sends the browser back to (the relay URL).",
    )
    interval: int | None = strawberry.field(
        default=None, description="POLL: seconds between two completeAuth calls."
    )
    user_code: str | None = strawberry.field(
        default=None,
        description="POLL: the code the user confirms on the provider's page.",
    )
    step: str | None = strawberry.field(
        default=None,
        description="Null until the first approval; then what is still awaited, e.g. MFA.",
    )
    error_code: str | None = strawberry.field(
        default=None,
        description="FAILED: machine-readable, the service's own error codes.",
    )
    error_message: str | None = strawberry.field(
        default=None, description="FAILED: one sentence for the user."
    )
    result: AuthResult | None = strawberry.field(
        default=None,
        description="DONE: what was linked. May be set earlier when it already exists (a relink).",
    )

    @classmethod
    def of(cls, session: "auth_sessions.AuthSession") -> "AuthSession":
        """The GraphQL shape of a described login."""
        result = session.result
        return cls(
            state=session.state,
            status=enums.AuthStatus(session.status),
            finish=enums.AuthFinish(session.finish),
            open_url=session.open_url,
            expires_at=session.expires_at,
            redirect_url=session.redirect_url,
            interval=session.interval,
            user_code=session.user_code,
            step=session.step,
            error_code=session.error_code,
            error_message=session.error_message,
            result=AuthResult(
                identifier=result.identifier,
                id=strawberry.ID(result.id),
                label=result.label,
            )
            if result
            else None,
        )


@kante.django_type(
    models.BankConnection,
    pagination=True,
    filters=filters.BankConnectionFilter,
    description="One consent at one bank. Accounts are synced through it while it is ACTIVE.",
)
class BankConnection(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    aspsp_name: str
    aspsp_country: str
    provider: enums.Provider = strawberry_django.field(
        description="The kind of provider it goes through."
    )
    bank_provider: BankProvider | None = strawberry_django.field(
        description="The organization's provider it was made through; null when it is attached to none (it then cannot sync until an admin sets its provider up again)."
    )
    status: enums.ConnectionStatus
    link_step: enums.LinkStep | None
    valid_until: datetime.datetime | None
    created_at: datetime.datetime
    linked_at: datetime.datetime | None
    last_error: str | None
    last_error_code: enums.BankErrorCode | None = strawberry_django.field(
        description="The kind of `lastError`, for the client to offer a fix."
    )
    creator: User | None
    organization: Organization

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):  # noqa: ANN001, ANN003, ANN206
        # A cancelled login is kept only so `authSession` can still answer for it.
        return (
            super()
            .get_queryset(queryset, info, **kwargs)
            .exclude(status=models.ConnectionStatus.CANCELLED)
        )

    @strawberry_django.field(
        description="The login still to be finished, when you started it and it is PENDING: continue it with `resumeAuth(state)`. Null otherwise."
    )
    def pending_auth(self, info: Info) -> AuthSession | None:
        if (
            self.status != models.ConnectionStatus.PENDING
            or self.creator_id != info.context.request.user.id  # type: ignore[attr-defined]
        ):
            return None
        session = auth_sessions.describe(self)  # type: ignore[arg-type]
        return AuthSession.of(session) if session.status == "PENDING" else None

    @strawberry_django.field(
        description="The accounts reached through this connection (through their syncers)."
    )
    def accounts(self) -> list["BankAccount"]:
        return list(
            models.BankAccount.objects.filter(syncers__connection_id=self.id)
            .distinct()
            .order_by("id")
        )  # type: ignore[return-value]

    @strawberry_django.field(description="The syncers reached through this connection.")
    def syncers(self) -> list["AccountSyncer"]:
        return list(self.syncers.order_by("id"))  # type: ignore[attr-defined, return-value]

    @strawberry_django.field(
        description="PENDING only: when the login can no longer be completed. Nothing flips it on a timer — past this, a PENDING link is dead: hide it or `cancelAuth` it."
    )
    def pending_expires_at(self) -> datetime.datetime | None:
        return (
            self.pending_expires_at
            if self.status == models.ConnectionStatus.PENDING
            else None
        )  # type: ignore[return-value]

    @strawberry_django.field(
        description="PENDING and past `pendingExpiresAt`: the login can no longer be completed."
    )
    def is_abandoned(self) -> bool:
        return (
            self.status == models.ConnectionStatus.PENDING
            and self.pending_expires_at < timezone.now()
        )  # type: ignore[operator]

    @strawberry_django.field(
        description="The earliest every account of the connection may sync again (null: now). `syncConnection` needs all of them."
    )
    def next_sync_allowed_at(self) -> datetime.datetime | None:
        from finance.sync import sync_budget

        times = [
            b.next_allowed_at
            for b in (sync_budget(s) for s in self.syncers.all())
            if b.next_allowed_at
        ]  # type: ignore[attr-defined]
        return max(times) if times else None

    @strawberry_django.field(
        description="The fewest syncs any account of the connection has left today (null: no limit)."
    )
    def syncs_remaining_today(self) -> int | None:
        from finance.sync import sync_budget

        left = [
            b.remaining_today
            for b in (sync_budget(s) for s in self.syncers.all())
            if b.remaining_today is not None
        ]  # type: ignore[attr-defined]
        return min(left) if left else None

    @strawberry_django.field(
        description="True when the consent ran out or was withdrawn: start a new link to the same bank to continue."
    )
    def needs_reauth(self) -> bool:
        if self.status == models.ConnectionStatus.EXPIRED:
            return True
        return (
            self.status == models.ConnectionStatus.ACTIVE
            and self.valid_until is not None
            and self.valid_until <= timezone.now()
        )


@kante.django_type(
    models.AccountSyncer,
    description="One way an account is pulled from a provider (an Enable Banking account, a Scalable pot). Lease, daily budget and last outcome are per syncer.",
)
class AccountSyncer(OrgScoped):
    id: strawberry.ID
    backend: enums.Provider = strawberry_django.field(
        description="Which provider it pulls from."
    )
    account: "BankAccount"
    connection: BankConnection | None = strawberry_django.field(
        description="The consent or login it is reached through; null once that is deleted."
    )
    created_at: datetime.datetime
    last_synced_at: datetime.datetime | None
    last_error: str | None
    last_error_code: enums.BankErrorCode | None = strawberry_django.field(
        description="The kind of `lastError`, for the client to offer a fix."
    )

    @strawberry_django.field(
        description="The earliest this syncer may sync (null: now) — after today's budget is spent or the provider asked to wait."
    )
    def next_sync_allowed_at(self) -> datetime.datetime | None:
        from finance.sync import sync_budget

        return sync_budget(self).next_allowed_at  # type: ignore[arg-type]

    @strawberry_django.field(
        description="Syncs left today under the provider's daily limit (null: no limit)."
    )
    def syncs_remaining_today(self) -> int | None:
        from finance.sync import sync_budget

        return sync_budget(self).remaining_today  # type: ignore[arg-type]

    @strawberry_django.field(description="True while a sync holds the syncer.")
    def is_syncing(self) -> bool:
        return (
            self.sync_lease_until is not None and self.sync_lease_until > timezone.now()  # type: ignore[attr-defined]
        )


def _live_syncers(account: models.BankAccount) -> list[models.AccountSyncer]:
    """The account's syncers whose connection is ACTIVE — the ones `syncAccount` runs."""
    return [
        s
        for s in account.syncers.select_related("connection").order_by("id")
        if s.connection and s.connection.status == models.ConnectionStatus.ACTIVE
    ]


@kante.django_type(
    models.BankAccount,
    pagination=True,
    filters=filters.BankAccountFilter,
    ordering=filters.BankAccountOrder,
    description="An account. Keeps its history across relinks; fed by its syncers and by imports.",
)
class BankAccount(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    iban: str | None
    name: str | None
    currency: str
    product: str | None
    kind: enums.AccountKind
    organization: Organization
    created_at: datetime.datetime
    syncers: list[AccountSyncer] = strawberry_django.field(
        description="How the account is pulled from providers; empty for an account fed only by imports."
    )
    transactions: list["Transaction"] = strawberry_django.field(
        pagination=True,
        filters=filters.TransactionFilter,
        ordering=filters.TransactionOrder,
        description="The account's transactions.",
    )
    balances: list["BalanceSnapshot"] = strawberry_django.field(
        description="Every balance the bank reported, one per day and type."
    )

    @strawberry_django.field(
        description="The connection of the account's live syncer (else of its newest one); null for an account fed only by imports."
    )
    def connection(self) -> BankConnection | None:
        live = _live_syncers(self)  # type: ignore[arg-type]
        if live:
            return live[0].connection  # type: ignore[return-value]
        newest = (
            self.syncers.exclude(connection=None)  # type: ignore[attr-defined]
            .select_related("connection")
            .order_by("-id")
            .first()
        )
        return newest.connection if newest else None

    @strawberry_django.field(
        description="True when no live syncer pulls the account (no connection, or only expired/revoked ones): it grows by imports only."
    )
    def is_import_only(self) -> bool:
        return not _live_syncers(self)  # type: ignore[arg-type]

    @strawberry_django.field(
        description="When the last successful sync of any syncer finished."
    )
    def last_synced_at(self) -> datetime.datetime | None:
        times = [s.last_synced_at for s in self.syncers.all() if s.last_synced_at]  # type: ignore[attr-defined]
        return max(times) if times else None

    @strawberry_django.field(
        description="Why the last sync of a syncer failed, if one did."
    )
    def last_error(self) -> str | None:
        failed = self.syncers.exclude(last_error=None).order_by("-id").first()  # type: ignore[attr-defined]
        return failed.last_error if failed else None

    @strawberry_django.field(
        description="The kind of `lastError`, for the client to offer a fix."
    )
    def last_error_code(self) -> enums.BankErrorCode | None:
        failed = self.syncers.exclude(last_error=None).order_by("-id").first()  # type: ignore[attr-defined]
        return (
            enums.BankErrorCode(failed.last_error_code)
            if failed and failed.last_error_code
            else None
        )

    @strawberry_django.field(
        description="The earliest every live syncer may sync (null: now) — after today's budget is spent or the provider asked to wait. Syncing earlier fails with RATE_LIMITED without contacting the provider."
    )
    def next_sync_allowed_at(self) -> datetime.datetime | None:
        from finance.sync import sync_budget

        times = [
            b.next_allowed_at
            for b in (sync_budget(s) for s in _live_syncers(self))  # type: ignore[arg-type]
            if b.next_allowed_at
        ]
        return max(times) if times else None

    @strawberry_django.field(
        description="The fewest syncs any live syncer has left today (null: no limit)."
    )
    def syncs_remaining_today(self) -> int | None:
        from finance.sync import sync_budget

        left = [
            b.remaining_today
            for b in (sync_budget(s) for s in _live_syncers(self))  # type: ignore[arg-type]
            if b.remaining_today is not None
        ]
        return min(left) if left else None

    @strawberry_django.field(
        description="A depot's positions as of its latest sync (empty for other accounts)."
    )
    def current_holdings(self) -> list["HoldingSnapshot"]:
        latest = self.holdings.order_by("-date").values_list("date", flat=True).first()  # type: ignore[attr-defined]
        return (
            list(self.holdings.filter(date=latest).order_by("-valuation"))  # type: ignore[attr-defined, return-value]
            if latest
            else []
        )

    @strawberry_django.field(
        description="How many positions a depot held at its latest sync (`currentHoldings`, counted; 0 for other accounts)."
    )
    def holdings_count(self) -> int:
        latest = self.holdings.order_by("-date").values_list("date", flat=True).first()  # type: ignore[attr-defined]
        return self.holdings.filter(date=latest).count() if latest else 0  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="True while a sync holds one of the account's syncers."
    )
    def is_syncing(self) -> bool:
        now = timezone.now()
        return any(
            s.sync_lease_until is not None and s.sync_lease_until > now
            for s in self.syncers.all()  # type: ignore[attr-defined]
        )

    @strawberry_django.field(
        description="The newest balance the bank reported, of the most authoritative type."
    )
    def latest_balance(self) -> Optional["BalanceSnapshot"]:
        from finance.stats import latest_balance

        return latest_balance(self)  # type: ignore[arg-type]


@kante.django_type(
    models.BalanceSnapshot,
    description="An account balance as the bank reported it on a day.",
)
class BalanceSnapshot(OrgScoped):
    id: strawberry.ID
    account: BankAccount
    date: datetime.date
    balance_type: str
    amount: Decimal
    currency: str


@kante.django_type(
    models.HoldingSnapshot,
    description="One security position in a depot on a day. One row per day and ISIN is the depot's history.",
)
class HoldingSnapshot(OrgScoped):
    id: strawberry.ID
    account: BankAccount
    date: datetime.date
    isin: str
    name: str
    security_type: str | None
    quantity: Decimal
    fifo_price: Decimal | None = strawberry_django.field(
        description="Average buy-in price per unit (FIFO)."
    )
    price: Decimal | None = strawberry_django.field(
        description="The last quoted mid price per unit."
    )
    valuation: Decimal
    currency: str

    @strawberry_django.field(
        description="valuation − quantity × fifoPrice: the unrealized gain (negative for a loss)."
    )
    def unrealized_gain(self) -> Decimal | None:
        if self.fifo_price is None:
            return None
        return (self.valuation - self.quantity * self.fifo_price).quantize(
            Decimal("0.01")
        )


@kante.django_type(
    models.Category,
    pagination=True,
    filters=filters.CategoryFilter,
    ordering=filters.CategoryOrder,
    description="A spending or income category. Categories nest; budgets and stats roll children up.",
)
class Category(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    name: str
    color: str | None
    kind: enums.CategoryKind = strawberry_django.field(
        description="Expense, income or transfer — always the root's kind, for the whole subtree."
    )
    key: str | None = strawberry_django.field(
        description="The base-taxonomy key (`food.groceries`) of a base category; null for the organization's own."
    )
    description: str = strawberry_django.field(
        description="What belongs here, in words bank lines use. Each comma-separated phrase is a term the category is recognized by."
    )
    hidden: bool = strawberry_django.field(
        description="Hidden from pickers, suggestions and automatic assignment."
    )
    parent: Optional["Category"]
    children: list["Category"]
    rules: list["CategoryRule"]
    organization: Organization
    created_at: datetime.datetime

    @strawberry_django.field(
        description="True for a base category (it has a `key`); it stays fully editable."
    )
    def is_base(self) -> bool:
        return self.key is not None

    @strawberry_django.field(
        description="The phrases this category is recognized by: its name and each phrase of its description."
    )
    def terms(self) -> list[str]:
        return list(self.terms.order_by("id").values_list("text", flat=True))  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="Uncategorized (or semantically guessed) transactions that look like they belong here, closest first: close to what the organization put in this category, or to one of its terms."
    )
    def candidates(self, limit: int = 20) -> list["Transaction"]:
        from finance import semantic

        pool = models.Transaction.objects.filter(
            account__organization_id=self.organization_id,  # type: ignore[attr-defined]
            category_source__in=[
                models.CategorySource.NONE,
                models.CategorySource.SEMANTIC,
            ],
        ).exclude(category_id=self.id)
        queryset, predicate = semantic.near_category(pool, self)  # type: ignore[arg-type]
        return list(queryset.filter(predicate)[:limit])  # type: ignore[return-value]


@kante.django_type(
    models.CategoryRule,
    pagination=True,
    filters=filters.CategoryRuleFilter,
    description="Assigns a category to matching transactions; the first active rule by priority wins.",
)
class CategoryRule(OrgScoped):
    id: strawberry.ID
    category: Category
    priority: int
    field: enums.RuleField
    match: enums.RuleMatch
    pattern: str
    direction: enums.RuleDirection
    amount_min: Decimal | None
    amount_max: Decimal | None
    active: bool
    created_at: datetime.datetime


@kante.django_type(
    models.Transaction,
    pagination=True,
    filters=filters.TransactionFilter,
    ordering=filters.TransactionOrder,
    description="A booked or pending transaction. Amounts are signed: negative is money out.",
)
class Transaction(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    account: BankAccount
    booking_date: datetime.date | None
    value_date: datetime.date | None
    transaction_date: datetime.date | None
    amount: Decimal
    currency: str
    status: enums.TransactionStatus
    counterparty: str | None
    counterparty_iban: str | None
    remittance: str | None
    entry_reference: str | None
    kind: enums.TransactionKind | None = strawberry_django.field(
        description="The provider's transaction type (Scalable: BUY, SELL, DEPOSIT, DISTRIBUTION, INTEREST, FEE, TAX, …); null for bank transactions."
    )
    isin: str | None
    quantity: Decimal | None
    category: Category | None
    category_source: enums.CategorySource
    note: str | None
    is_transfer: bool
    is_transfer_manual: bool
    merchant: Optional["Merchant"] = strawberry_django.field(
        description="Who the transaction was with."
    )
    merchant_location: Optional["MerchantLocation"] = strawberry_django.field(
        description="Where: the merchant's store, when the line names one."
    )
    merchant_source: enums.MerchantSource = strawberry_django.field(
        description="How the merchant was set (AUTO by alias, MANUAL by a user)."
    )
    origin: enums.TransactionOrigin = strawberry_django.field(
        description="Whether the bank fields were synced or imported from a file."
    )
    syncer: AccountSyncer | None = strawberry_django.field(
        description="The syncer that last wrote the bank fields; null for imported rows."
    )
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @strawberry_django.field(
        description="The exact text this transaction is embedded as: its bank line (normalized: no numbers, 'DANKT', cities or legal forms), then what its merchant and place add. Two lines are similar when these texts are."
    )
    def semantic_input(self) -> str | None:
        return self.embedding_source_text()  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="The categories this transaction most likely belongs to, best first — from similar transactions the organization categorized and from category terms. Empty when it has no embedding yet. `unsettledOnly`: also empty for a settled row (a transfer, a provider `kind`, or a category set by a user, rule, merchant or import), so a list ranks only the rows it offers suggestions on."
    )
    def suggested_categories(
        self, limit: int = 3, unsettled_only: bool = False
    ) -> list["CategorySuggestion"]:
        if unsettled_only and is_settled(self):  # type: ignore[arg-type]
            return []
        return category_suggestions(self, limit)  # type: ignore[arg-type]

    @strawberry_django.field(
        description="The organization's transactions most similar to this one (same merchant, same kind of payment), closest first. `maxDistance` (cosine, 0–2) drops the far ones."
    )
    def similar_transactions(
        self, limit: int = 10, max_distance: float | None = None
    ) -> list["Transaction"]:
        from embeddings import search

        pool = models.Transaction.objects.filter(
            account__organization_id=self.account.organization_id  # type: ignore[attr-defined]
        )
        return list(
            search.neighbours(
                pool,
                self.embedding if self.embedding_model == _model_id() else None,  # type: ignore[attr-defined, return-value]
                exclude_pk=self.pk,  # type: ignore[attr-defined, return-value]
                limit=limit,
                threshold=max_distance,
            )
        )


@kante.django_type(
    models.Budget,
    pagination=True,
    filters=filters.BudgetFilter,
    ordering=filters.BudgetOrder,
    description="A monthly spending limit for a category and its children, in one currency.",
)
class Budget(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    category: Category
    amount: Decimal
    currency: str
    start_month: datetime.date
    end_month: datetime.date | None
    created_at: datetime.datetime


@kante.django_type(
    models.RecurringPayment,
    pagination=True,
    filters=filters.RecurringPaymentFilter,
    ordering=filters.RecurringPaymentOrder,
    description="A payment that repeats at a regular interval, detected from an account's history.",
)
class RecurringPayment(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    account: BankAccount
    label: str
    amount: Decimal
    currency: str
    interval_days: int
    occurrences: int
    last_seen: datetime.date
    next_expected: datetime.date
    status: enums.RecurringStatus
    transactions: list[Transaction]


# --- Results that are not rows ----------------------------------------------------------------


@strawberry.type(description="A bank a provider can reach.")
class Institution:
    name: str
    country: str
    logo: str | None = None
    bic: str | None = None
    maximum_consent_days: int | None = strawberry.field(
        default=None, description="The longest consent this bank grants, in days."
    )


@strawberry.type(description="A likely category for a transaction.")
class CategorySuggestion:
    category: Category
    score: float = strawberry.field(
        description="The category's share (0–1) of all votes; above the auto-assign threshold with evidence, sync assigns it (source SEMANTIC)."
    )
    reason: enums.SuggestionReason
    neighbours: int = strawberry.field(
        description="How many similar categorized transactions voted for it."
    )
    evidence: list[Transaction] = strawberry.field(
        description="Up to three of those transactions."
    )


def is_settled(tx: models.Transaction) -> bool:
    """Whether a transaction needs no category suggestion: a transfer, a provider transaction type, or a category nobody would second-guess (anything but SEMANTIC)."""
    if tx.is_transfer or tx.kind:
        return True
    return (
        tx.category_id is not None  # type: ignore[attr-defined]
        and tx.category_source != models.CategorySource.SEMANTIC
    )


def category_suggestions(
    tx: models.Transaction, limit: int
) -> list[CategorySuggestion]:
    """``finance.semantic.suggest`` as GraphQL results."""
    from finance import semantic

    return [
        CategorySuggestion(
            category=s.category,  # type: ignore[arg-type]
            score=s.score,
            reason=enums.SuggestionReason(s.reason),
            neighbours=s.neighbours,
            evidence=s.evidence,  # type: ignore[arg-type]
        )
        for s in semantic.suggest(tx, limit)
    ]


@strawberry.type(description="What a sync of one account did.")
class SyncResult:
    account: BankAccount
    created: int
    updated: int
    pending_replaced: int
    balances: int
    categorized: int
    holdings: int = strawberry.field(default=0, description="Depot positions stored.")


@strawberry.type(description="An account finished syncing.")
class AccountSyncEvent:
    account_id: strawberry.ID
    created: int
    updated: int
    pending_replaced: int


@strawberry.type(
    description="Money in and out of one category, in one currency. `category` is null for uncategorized transactions."
)
class CategoryTotal:
    category: Category | None
    currency: str
    income: Decimal
    expense: Decimal = strawberry.field(description="Money out, as a positive amount.")
    net: Decimal
    count: int


@strawberry.type(
    description="Money in and out during one month or week, in one currency."
)
class CashflowBucket:
    period_start: datetime.date
    currency: str
    income: Decimal
    expense: Decimal = strawberry.field(description="Money out, as a positive amount.")
    net: Decimal
    count: int


@strawberry.type(description="Money to or from one counterparty, in one currency.")
class CounterpartyTotal:
    counterparty: str
    currency: str
    total: Decimal = strawberry.field(description="As a positive amount.")
    count: int


@strawberry.type(description="An end-of-day account balance.")
class BalancePoint:
    date: datetime.date
    amount: Decimal
    currency: str
    reported: bool = strawberry.field(
        description="True if the bank reported this balance; false if derived from transactions."
    )


@strawberry.type(description="One budget in one month.")
class BudgetStatus:
    budget: Budget
    month: datetime.date
    budgeted: Decimal
    spent: Decimal = strawberry.field(
        description="Net money out in the category and its children, as a positive amount."
    )
    remaining: Decimal = strawberry.field(description="Negative when over budget.")
    ratio: float = strawberry.field(description="spent / budgeted.")


@strawberry.type(description="An expected end-of-day balance.")
class ForecastPoint:
    date: datetime.date
    amount: Decimal
    currency: str


# --- merchants -------------------------------------------------------------------------------------


@strawberry.type(description="An amount in one currency.")
class CurrencyTotal:
    currency: str
    amount: Decimal


@strawberry.type(
    description="The latest balances of the accounts of one kind, summed in one currency (a depot's is its market valuation)."
)
class BalanceTotal:
    kind: enums.AccountKind
    currency: str
    amount: Decimal = strawberry.field(
        description="Σ of each account's newest balance, of the most authoritative type (`latestBalance`)."
    )
    accounts: int = strawberry.field(
        description="How many accounts reported a balance."
    )


_UNSET = object()


def _rows_of(relation: str) -> "QuerySet[models.Transaction, dict[str, object]]":
    """The transactions of the outer row (a merchant or a place), grouped to one group for an aggregate."""
    return (
        models.Transaction.objects.filter(**{relation: OuterRef("pk")})
        .order_by()
        .values(relation)
    )


def _count_of(relation: str) -> Coalesce:
    """How many transactions the outer row has, as one correlated subquery (no join that a filter could narrow)."""
    return Coalesce(
        Subquery(
            _rows_of(relation).annotate(n=Count("pk")).values("n")[:1],
            output_field=IntegerField(),
        ),
        Value(0),
    )


def _first_day_of(relation: str) -> Subquery:
    return Subquery(_rows_of(relation).annotate(d=Min("booking_date")).values("d")[:1])


def _last_day_of(relation: str) -> Subquery:
    return Subquery(_rows_of(relation).annotate(d=Max("booking_date")).values("d")[:1])


def _net_of(relation: str) -> ArraySubquery:
    """Σ of the booked amounts per currency, as `{currency, total}` rows (the total as text: no float on the way)."""
    rows = (
        models.Transaction.objects.filter(
            **{relation: OuterRef("pk")}, status=models.TransactionStatus.BOOKED
        )
        .order_by("currency")
        .values("currency")
    )
    return ArraySubquery(
        rows.annotate(
            row=JSONObject(
                currency=F("currency"), total=Cast(Sum("amount"), CharField())
            )
        ).values("row")
    )


@kante.django_type(
    models.MerchantAlias,
    description='A normalized counterparty prefix that means this merchant ("spar" matches "Spar Dankt 3418").',
)
class MerchantAlias(OrgScoped):
    id: strawberry.ID
    pattern: str
    merchant: "Merchant"
    created_at: datetime.datetime


@kante.django_type(
    models.MerchantLocation,
    pagination=True,
    filters=filters.MerchantLocationFilter,
    description="A place of a merchant — a store, a branch. `latitude`/`longitude` are null until known (a store discovered from a bank line starts without an address).",
)
class MerchantLocation(OrgScoped):
    id: strawberry.ID
    merchant: "Merchant"
    name: str
    store_code: str | None
    street: str | None
    postal_code: str | None
    city: str | None
    region: str | None
    country: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    source: enums.LocationSource
    osm_id: str | None
    geocoded_at: datetime.datetime | None
    notes: str
    created_at: datetime.datetime
    transactions: list["Transaction"] = strawberry_django.field(
        pagination=True,
        filters=filters.TransactionFilter,
        ordering=filters.TransactionOrder,
        description="Transactions at this place.",
    )

    @strawberry_django.field(
        description="Meters from the point of a `near` filter; null without one."
    )
    def distance_meters(self) -> float | None:
        return getattr(self, "_distance_meters", None)

    @strawberry_django.field(
        description="How many transactions happened here.",
        annotate={"_transaction_count": _count_of("merchant_location")},
    )
    def transaction_count(self) -> int:
        counted = getattr(self, "_transaction_count", None)
        return counted if isinstance(counted, int) else self.transactions.count()  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="The day of the latest transaction here.",
        annotate={"_last_visit": _last_day_of("merchant_location")},
    )
    def last_visit(self) -> datetime.date | None:
        last = getattr(self, "_last_visit", _UNSET)
        if last is _UNSET:
            return self.transactions.aggregate(last=Max("booking_date"))["last"]  # type: ignore[attr-defined]
        return last if isinstance(last, datetime.date) else None


@kante.django_type(
    models.Merchant,
    pagination=True,
    filters=filters.MerchantFilter,
    ordering=filters.MerchantOrder,
    description="Someone the organization pays or is paid by. Recognized on bank lines by its aliases; its default category categorizes its transactions (after rules, before suggestions).",
)
class Merchant(OrgScoped):
    id: strawberry.ID
    descriptors: JSON = strawberry_django.field(
        resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION
    )
    name: str
    key: str
    description: str
    website: str | None
    logo_url: str | None
    online: bool
    category: Category | None = strawberry_django.field(
        description="The default category of its transactions (source MERCHANT)."
    )
    created_at: datetime.datetime
    aliases: list[MerchantAlias]
    locations: list[MerchantLocation] = strawberry_django.field(
        filters=filters.MerchantLocationFilter, description="Its places."
    )
    rules: list["MerchantRule"] = strawberry_django.field(
        description="Rules mapping transactions to it."
    )
    transactions: list["Transaction"] = strawberry_django.field(
        pagination=True,
        filters=filters.TransactionFilter,
        ordering=filters.TransactionOrder,
        description="Its transactions.",
    )

    @strawberry_django.field(
        description="Meters from the point of a `near` filter to its closest located store; null without one."
    )
    def distance_meters(self) -> float | None:
        return getattr(self, "_distance_meters", None)

    @strawberry_django.field(
        description="How many transactions it has.",
        annotate={"_transaction_count": _count_of("merchant")},
    )
    def transaction_count(self) -> int:
        counted = getattr(self, "_transaction_count", None)
        return counted if isinstance(counted, int) else self.transactions.count()  # type: ignore[attr-defined]

    @strawberry_django.field(
        description="The net amount per currency over its booked transactions (negative: money spent there).",
        annotate={"_net": _net_of("merchant")},
    )
    def net(self) -> list[CurrencyTotal]:
        annotated = getattr(self, "_net", None)
        if isinstance(annotated, list):
            return [
                CurrencyTotal(
                    currency=str(r["currency"]), amount=Decimal(str(r["total"]))
                )
                for r in annotated
                if isinstance(r, dict)
            ]
        rows = (
            self.transactions.filter(status=models.TransactionStatus.BOOKED)  # type: ignore[attr-defined]
            .values("currency")
            .annotate(total=Sum("amount"))
            .order_by("currency")
        )
        return [CurrencyTotal(currency=r["currency"], amount=r["total"]) for r in rows]

    @strawberry_django.field(
        description="The day of its first transaction.",
        annotate={"_first_seen": _first_day_of("merchant")},
    )
    def first_seen(self) -> datetime.date | None:
        first = getattr(self, "_first_seen", _UNSET)
        if first is _UNSET:
            return self.transactions.aggregate(first=Min("booking_date"))["first"]  # type: ignore[attr-defined]
        return first if isinstance(first, datetime.date) else None

    @strawberry_django.field(
        description="The day of its latest transaction.",
        annotate={"_last_seen": _last_day_of("merchant")},
    )
    def last_seen(self) -> datetime.date | None:
        last = getattr(self, "_last_seen", _UNSET)
        if last is _UNSET:
            return self.transactions.aggregate(last=Max("booking_date"))["last"]  # type: ignore[attr-defined]
        return last if isinstance(last, datetime.date) else None

    @strawberry_django.field(
        description="The organization's merchants closest in meaning to this one (by name and description)."
    )
    def similar_merchants(self, limit: int = 5) -> list["Merchant"]:
        from embeddings import search

        pool = models.Merchant.objects.filter(organization_id=self.organization_id)  # type: ignore[attr-defined]
        vector = self.embedding if self.embedding_model == _model_id() else None  # type: ignore[attr-defined]
        return list(search.neighbours(pool, vector, exclude_pk=self.pk, limit=limit))  # type: ignore[attr-defined, return-value]


@kante.django_type(
    models.MerchantRule,
    pagination=True,
    filters=filters.MerchantRuleFilter,
    description="Maps matching transactions to a merchant (like a category rule): the first active rule by priority wins, before aliases; a manual link wins over both.",
)
class MerchantRule(OrgScoped):
    id: strawberry.ID
    merchant: Merchant
    location: MerchantLocation | None = strawberry_django.field(
        description="The pinned place, if any (else a store number on the line decides)."
    )
    priority: int
    field: enums.RuleField
    match: enums.RuleMatch
    pattern: str
    direction: enums.RuleDirection
    amount_min: Decimal | None
    amount_max: Decimal | None
    active: bool
    created_at: datetime.datetime

    @strawberry_django.field(
        description="How many transactions this rule currently links."
    )
    def transaction_count(self) -> int:
        from finance.rules import matches

        rows = models.Transaction.objects.filter(
            account__organization_id=self.organization_id,  # type: ignore[attr-defined]
            merchant_id=self.merchant_id,  # type: ignore[attr-defined]
            merchant_source=models.MerchantSource.RULE,
        ).only("counterparty", "counterparty_iban", "remittance", "amount")
        return sum(1 for tx in rows if matches(self, tx))  # type: ignore[arg-type]


@strawberry.type(
    description="A counterparty that recurs without a merchant: a merchant waiting to be created (`createMerchant(input: {fromTransactions: …})`)."
)
class MerchantCandidate:
    key: str = strawberry.field(
        description="The normalized text its lines share (becomes the alias)."
    )
    count: int
    totals: list[CurrencyTotal] = strawberry.field(
        description="Net amount per currency."
    )
    samples: list[str] = strawberry.field(
        description="Up to three counterparty spellings."
    )
    store_codes: int = strawberry.field(
        description="How many distinct store numbers its lines carry (each becomes a location)."
    )
    transaction_ids: list[strawberry.ID]
    suggested_category: Category | None = strawberry.field(
        description="What its latest transaction would most likely be categorized as."
    )


@strawberry.type(
    description="Money in and out with one merchant, in one currency. `merchant` is null for transactions without one."
)
class MerchantTotal:
    merchant: Merchant | None
    currency: str
    income: Decimal
    expense: Decimal = strawberry.field(description="Money out, as a positive amount.")
    net: Decimal
    count: int


@strawberry.type(description="A geocoder hit.")
class GeocodeResult:
    label: str
    latitude: Decimal
    longitude: Decimal
    street: str | None
    postal_code: str | None
    city: str | None
    region: str | None
    country: str | None
    osm_id: str | None


# --- GeoJSON, typed ----------------------------------------------------------------------------------
# Field names follow RFC 7946, so a response *is* GeoJSON: hand `merchantLocationsGeojson` to a
# map renderer as it comes, while the schema still types every property.


@strawberry.type(
    name="PointGeometry",
    description="A GeoJSON Point: `coordinates` is `[longitude, latitude]` (WGS84), as GeoJSON orders them.",
)
class PointGeometry:
    type: str = strawberry.field(default="Point", description="Always `Point`.")
    coordinates: list[float] = strawberry.field(
        default_factory=list, description="`[longitude, latitude]`."
    )


@strawberry.type(
    description="What a map styles a merchant place by: flat, numeric where it matters."
)
class MerchantLocationProperties:
    id: strawberry.ID
    name: str
    store_code: str | None
    source: enums.LocationSource
    street: str | None
    postal_code: str | None
    city: str | None
    country: str | None
    merchant_id: strawberry.ID
    merchant_name: str
    category_id: strawberry.ID | None
    category_name: str | None
    category_kind: enums.CategoryKind | None
    color: str | None = strawberry.field(
        description="The merchant's category color, if any."
    )
    transaction_count: int
    last_visit: datetime.date | None
    net: Decimal = strawberry.field(
        description="Net booked amount at this place (negative: money spent), in `currency`. A Decimal string like every amount."
    )
    currency: str
    distance_meters: float | None = strawberry.field(
        default=None, description="Meters from the point of a `near` filter."
    )


@strawberry.type(description="A GeoJSON Feature: one located merchant place.")
class MerchantLocationFeature:
    type: str = strawberry.field(description="Always `Feature`.")
    id: strawberry.ID
    geometry: PointGeometry
    properties: MerchantLocationProperties


@strawberry.type(
    description="A GeoJSON FeatureCollection of merchant places — valid GeoJSON as returned, and fully typed."
)
class MerchantLocationFeatureCollection:
    type: str = strawberry.field(description="Always `FeatureCollection`.")
    features: list[MerchantLocationFeature]
    bbox: list[float] | None = strawberry.field(
        default=None,
        description="`[west, south, east, north]` around the features; null when empty.",
    )
