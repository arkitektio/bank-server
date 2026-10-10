"""The bank data model.

Everything belongs to an organization: rows that carry no ``organization`` column
(transactions, balance snapshots) reach one through a required FK, which is what
:mod:`finance.scoping` follows.

Money is always a ``Decimal`` with an explicit currency — never a float. Amounts
are signed: negative is money out, positive money in.
"""

from django.db import models
from authentikate.models import Organization, User
from koherent.fields import ProvenanceField

from django.contrib.postgres.indexes import GistIndex

from embeddings.models import EMBEDDING_FIELDS, EmbeddedDescriptionMixin, embedding_indexes
from finance.geo import GeographyPointField, MakePoint

def money(**kwargs) -> models.DecimalField:
    """A money column: two decimal places, up to 16 before the point."""
    return models.DecimalField(max_digits=18, decimal_places=2, **kwargs)


class ConnectionStatus(models.TextChoices):
    """Lifecycle of a bank consent."""

    PENDING = "PENDING", "Waiting for the user to approve at the bank"
    ACTIVE = "ACTIVE", "Consent granted; accounts can be synced"
    EXPIRED = "EXPIRED", "Consent ran out or was withdrawn at the bank; relink to continue"
    REVOKED = "REVOKED", "Revoked by a user of this service"
    FAILED = "FAILED", "The link was never completed"
    CANCELLED = "CANCELLED", "The user dropped the login before it was finished"


class Provider(models.TextChoices):
    """Who a connection reaches its accounts through."""

    ENABLEBANKING = "ENABLEBANKING", "A PSD2 bank consent via Enable Banking"
    SCALABLE = "SCALABLE", "A Scalable Capital broker login via Scalable's official CLI API"


class ProviderCapability(models.TextChoices):
    """Something a provider kind can do, switched on or off per provider."""

    TRANSACTIONS = "TRANSACTIONS", "Fetch and store the accounts' transactions"
    BALANCES = "BALANCES", "Store the balance the provider reports on every sync"
    HOLDINGS = "HOLDINGS", "Store a depot's positions on every sync"
    PRICES = "PRICES", "Use the provider's logins as a source of security prices"
    SCHEDULED_SYNC = "SCHEDULED_SYNC", "Let the unattended sync action sync its accounts"


class AccountKind(models.TextChoices):
    """What an account holds."""

    CASH = "CASH", "A cash account (checking, card, broker clearing account)"
    DEPOT = "DEPOT", "A securities depot; its balance is the market valuation"
    SAVINGS = "SAVINGS", "A savings account (e.g. overnight money)"


class LinkStep(models.TextChoices):
    """Where a Scalable link is: waiting for the device-code approval, then for the second factor."""

    DEVICE = "DEVICE", "Waiting for the user to approve the login code"
    MFA = "MFA", "Waiting for the user to approve the login on their trusted device"
    DONE = "DONE", "Logged in"


class BankErrorCode(models.TextChoices):
    """What went wrong, for a client to offer the right fix (also each GraphQL error's ``extensions.code``)."""

    CONSENT_EXPIRED = "CONSENT_EXPIRED", "The consent or login is gone; relink"
    RATE_LIMITED = "RATE_LIMITED", "The provider's limit is reached; try again later"
    MFA_REJECTED = "MFA_REJECTED", "The login or its second factor was denied; log in again"
    CODE_EXPIRED = "CODE_EXPIRED", "The login code or link ran out; get a new one"
    INVALID_STATE = "INVALID_STATE", "No such pending link (other login attempt, already used, other organization); start over"
    BANK_UNAVAILABLE = "BANK_UNAVAILABLE", "The provider could not be reached or failed; try later"
    BANK_ERROR = "BANK_ERROR", "The provider refused the request"
    CONNECTION_INACTIVE = "CONNECTION_INACTIVE", "The connection is revoked, failed or still pending"
    SYNC_IN_PROGRESS = "SYNC_IN_PROGRESS", "Another sync holds the account right now"
    NOT_CONFIGURED = "NOT_CONFIGURED", "This server has no credentials for the provider"
    LOGIN_REFUSED = "LOGIN_REFUSED", "The provider refused the login (the user cancelled there, or the bank said no); start over"


class TransactionKind(models.TextChoices):
    """A provider's transaction type (Scalable's broker and savings types); null for bank transactions."""

    BUY = "BUY", "Security bought"
    SELL = "SELL", "Security sold"
    SAVINGS_PLAN = "SAVINGS_PLAN", "Savings-plan execution"
    DEPOSIT = "DEPOSIT", "Cash paid in"
    WITHDRAWAL = "WITHDRAWAL", "Cash paid out"
    DISTRIBUTION = "DISTRIBUTION", "Dividend or fund distribution"
    INTEREST = "INTEREST", "Interest"
    FEE = "FEE", "Fee"
    TAX = "TAX", "Tax"
    TAX_RETURN = "TAX_RETURN", "Tax refund"
    TRANSFER_IN = "TRANSFER_IN", "Securities transferred in"
    TRANSFER_OUT = "TRANSFER_OUT", "Securities transferred out"
    CASH_TRANSFER_IN = "CASH_TRANSFER_IN", "Cash transferred in"
    CASH_TRANSFER_OUT = "CASH_TRANSFER_OUT", "Cash transferred out"
    SWAP_IN = "SWAP_IN", "Security swapped in"
    SWAP_OUT = "SWAP_OUT", "Security swapped out"
    CURRENCY_SWITCH_BUY = "CURRENCY_SWITCH_BUY", "Currency bought"
    CURRENCY_SWITCH_SELL = "CURRENCY_SWITCH_SELL", "Currency sold"
    POCKET_MONEY = "POCKET_MONEY", "Pocket money"
    REINVESTMENT = "REINVESTMENT", "Reinvestment"
    REINVESTMENT_DISTRIBUTION = "REINVESTMENT_DISTRIBUTION", "Reinvested distribution"
    REINVESTMENT_POCKET_MONEY = "REINVESTMENT_POCKET_MONEY", "Reinvested pocket money"
    CORPORATE_ACTION = "CORPORATE_ACTION", "Split, merger or other corporate action"
    OTHER = "OTHER", "A type this service does not know yet (see raw)"


class TransactionStatus(models.TextChoices):
    """Booking status as reported by the bank."""

    BOOKED = "BOOK", "Booked"
    PENDING = "PDNG", "Pending"
    OTHER = "OTHR", "Other"


class TransactionOrigin(models.TextChoices):
    """Where a transaction's bank fields came from."""

    SYNC = "SYNC", "Pulled by a syncer from the provider"
    IMPORT = "IMPORT", "Imported from a file; a sync that finds the same booking takes it over"


class CategorySource(models.TextChoices):
    """Who set a transaction's category — decides whether rules may change it."""

    NONE = "NONE", "Uncategorized"
    RULE = "RULE", "Set by a rule"
    MANUAL = "MANUAL", "Set by a user; rules never touch it"
    SEMANTIC = "SEMANTIC", "Set because similar, already categorized transactions agreed; rules and users override it"
    IMPORT = "IMPORT", "Taken from an imported statement (through the import's category mapping); rules never touch it"
    MERCHANT = "MERCHANT", "The merchant's default category; rules and users override it, it overrides semantic guesses"


class MerchantSource(models.TextChoices):
    """How a transaction got its merchant."""

    NONE = "NONE", "No merchant"
    AUTO = "AUTO", "Matched by one of the merchant's aliases; re-matched when aliases change"
    RULE = "RULE", "Set by a merchant rule; re-matched when rules change"
    MANUAL = "MANUAL", "Set by a user; matching never touches it"


class LocationSource(models.TextChoices):
    """Where a merchant location came from."""

    DISCOVERED = "DISCOVERED", "Created from a store number on a bank line; address unknown until filled in"
    MANUAL = "MANUAL", "Entered by a user"
    GEOCODED = "GEOCODED", "Address and coordinates looked up with the geocoder"


class CategoryKind(models.TextChoices):
    """What a category's money is."""

    EXPENSE = "EXPENSE", "Expense"
    INCOME = "INCOME", "Income"
    TRANSFER = "TRANSFER", "Transfer between own accounts; excluded from stats"


class RuleField(models.TextChoices):
    """The transaction field a rule looks at."""

    COUNTERPARTY = "COUNTERPARTY", "Counterparty name"
    REMITTANCE = "REMITTANCE", "Remittance information"
    IBAN = "IBAN", "Counterparty IBAN"


class RuleMatch(models.TextChoices):
    """How a rule's pattern is compared (always case-insensitive)."""

    CONTAINS = "CONTAINS", "Contains"
    EQUALS = "EQUALS", "Equals"
    REGEX = "REGEX", "Regular expression"


class RuleDirection(models.TextChoices):
    """Which transactions a rule applies to by sign."""

    ANY = "ANY", "Money in and out"
    OUT = "OUT", "Money out only"
    IN = "IN", "Money in only"


class ImportSource(models.TextChoices):
    """Which app or format a statement import came from."""

    FINANZGURU = "FINANZGURU", "A Finanzguru transaction export (xlsx, or the same columns as CSV)"


class ImportStatus(models.TextChoices):
    """Where a statement import is."""

    PREVIEWED = "PREVIEWED", "Parsed and matched against the accounts; nothing written yet"
    APPLIED = "APPLIED", "Written into the accounts"
    FAILED = "FAILED", "The file could not be read"


class RecurringStatus(models.TextChoices):
    """A detected recurring payment, as judged by a user."""

    DETECTED = "DETECTED", "Detected, not yet reviewed"
    CONFIRMED = "CONFIRMED", "Confirmed; used for forecasts"
    IGNORED = "IGNORED", "Ignored; never proposed again"


class BankProvider(models.Model):
    """A provider an organization set up: one kind, its settings and credentials, and what is switched on.

    For Enable Banking that is one application (its id in ``settings``, its private key
    Fernet-encrypted in ``secret``, see :mod:`finance.crypto`); a Scalable provider holds no
    credentials. The code of a kind is its backend (:mod:`finance.providers`), which validates
    ``settings`` and reads ``capabilities``; a kind needing other settings needs no migration.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_providers", help_text="The organization this provider belongs to.")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_providers", help_text="The admin who set it up.")
    kind = models.CharField(max_length=20, choices=Provider.choices, help_text="Which provider this is an instance of.")
    name = models.CharField(max_length=200, help_text="What the organization calls it.")
    enabled = models.BooleanField(default=True, help_text="A disabled provider starts no links and syncs nothing; its consents can still be revoked.")
    capabilities = models.JSONField(default=list, blank=True, help_text="The ProviderCapability values switched on, a subset of what the kind can do.")
    settings = models.JSONField(default=dict, blank=True, help_text="The kind's non-secret settings (Enable Banking: application id, redirect URLs, consent days).")
    secret = models.TextField(null=True, blank=True, help_text="Encrypted credentials of the kind (Enable Banking: the application's private key). Never exposed.")
    daily_sync_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Syncs per account per UTC day before the service stops asking the provider; null is unlimited.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the provider was set up.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When it was last changed.")
    provenance = ProvenanceField(excluded_fields=["secret"])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="bank_provider_org_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} [{self.kind}]"


class BankConnection(models.Model):
    """One consent at one bank (an Enable Banking session) or one Scalable Capital login.

    A Scalable connection keeps its credentials — the DPoP private key and the rotating refresh
    token bound to it — Fernet-encrypted in ``secret`` (see :mod:`finance.crypto`).
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_connections", help_text="The organization this connection belongs to.")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_connections", help_text="The user who started the link.")
    aspsp_name = models.CharField(max_length=1000, help_text="The bank's name, exactly as Enable Banking lists it.")
    aspsp_country = models.CharField(max_length=2, help_text="The bank's ISO country code.")
    state = models.CharField(max_length=100, unique=True, help_text="Opaque value tying the bank's redirect back to this link attempt.")
    redirect_url = models.CharField(max_length=2000, help_text="Where the bank redirects after approval.")
    status = models.CharField(max_length=20, choices=ConnectionStatus.choices, default=ConnectionStatus.PENDING, help_text="Where this consent is in its lifecycle.")
    session_id = models.CharField(max_length=200, null=True, blank=True, unique=True, help_text="The Enable Banking session id, once linked.")
    valid_until = models.DateTimeField(null=True, blank=True, help_text="When the bank consent runs out.")
    pending_expires_at = models.DateTimeField(help_text="A link not completed by then is refused.")
    last_error = models.TextField(null=True, blank=True, help_text="The last error talking to the bank, if any.")
    last_error_code = models.CharField(max_length=30, null=True, blank=True, help_text="The machine-readable kind of ``last_error`` (a BankErrorCode).")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the link was started.")
    linked_at = models.DateTimeField(null=True, blank=True, help_text="When the link was completed.")
    raw = models.JSONField(default=dict, blank=True, help_text="The session as Enable Banking returned it.")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.ENABLEBANKING, help_text="The kind of provider the accounts are reached through.")
    bank_provider = models.ForeignKey(BankProvider, on_delete=models.SET_NULL, null=True, blank=True, related_name="connections", help_text="The organization's provider this consent was made through; null until it is attached to one.")
    secret = models.TextField(null=True, blank=True, help_text="Encrypted provider credentials (Scalable: DPoP key and tokens). Never exposed.")
    provider_user_id = models.CharField(max_length=200, null=True, blank=True, help_text="The user's id at the provider (Scalable: the person id).")
    token_expires_at = models.DateTimeField(null=True, blank=True, help_text="When the stored access token runs out (Scalable).")
    token_lease_until = models.DateTimeField(null=True, blank=True, help_text="Held by a token refresh until then, so one replica rotates the refresh token at a time.")
    link_step = models.CharField(max_length=10, choices=LinkStep.choices, null=True, blank=True, help_text="Where a Scalable link is in its login.")
    link_poll_interval = models.PositiveIntegerField(default=5, help_text="Seconds between polls of the device authorization, as the provider asks.")
    link_next_poll_at = models.DateTimeField(null=True, blank=True, help_text="The device authorization is not polled again before this.")
    link_mfa_session_id = models.CharField(max_length=200, null=True, blank=True, help_text="The pending second-factor challenge of a Scalable link.")
    # Credentials and polling state rotate constantly and must not pile up in history rows.
    provenance = ProvenanceField(excluded_fields=["secret", "token_expires_at", "token_lease_until", "link_poll_interval", "link_next_poll_at", "link_mfa_session_id"])

    def __str__(self) -> str:
        return f"{self.aspsp_name} ({self.aspsp_country}) [{self.status}]"


def normalize_iban(iban: str | None) -> str | None:
    """An IBAN without spaces, upper-cased; None for an empty one."""
    cleaned = (iban or "").replace(" ", "").upper()
    return cleaned or None


class BankAccount(models.Model):
    """An account: its identity, and the history of every syncer and import that fed it.

    What reaches the account at a provider lives on its :class:`AccountSyncer` rows (one per
    provider identity); an account with no syncer is fed only by imports (a bank that is no
    longer linked). A relink re-attaches the same account — by the syncer's identity, else by
    IBAN — so the history, categories and notes stay.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_accounts", help_text="The organization this account belongs to.")
    iban = models.CharField(max_length=64, null=True, blank=True, help_text="The account's IBAN, if known.")
    iban_normalized = models.CharField(max_length=64, null=True, blank=True, help_text="The IBAN without spaces, upper-cased: what accounts are matched by.")
    import_key = models.CharField(max_length=500, null=True, blank=True, help_text="How an import identified an account that has no IBAN (``fg:<Referenzkonto>``).")
    name = models.CharField(max_length=1000, null=True, blank=True, help_text="The account's name at the bank.")
    currency = models.CharField(max_length=3, help_text="The account's ISO currency.")
    product = models.CharField(max_length=1000, null=True, blank=True, help_text="The bank's product name for the account.")
    kind = models.CharField(max_length=10, choices=AccountKind.choices, default=AccountKind.CASH, help_text="Cash, securities depot, or savings.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the account was first seen.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "import_key"], condition=models.Q(import_key__isnull=False), name="bank_acct_org_import_key"),
        ]
        indexes = [models.Index(fields=["organization", "iban_normalized"], name="bank_acct_org_iban")]

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.iban_normalized = normalize_iban(self.iban)
        if kwargs.get("update_fields") is not None and "iban" in kwargs["update_fields"]:
            kwargs["update_fields"] = {*kwargs["update_fields"], "iban_normalized"}
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name or self.iban or f"account {self.pk}"


class AccountSyncer(models.Model):
    """One way an account is pulled from a provider: an Enable Banking account or a Scalable pot.

    Keyed by ``(backend, identification_key)`` — Enable Banking's cross-session identification
    hash (else the IBAN), Scalable's person + pot — so relinking points the same syncer at the new
    connection. The lease, the daily budget and the last outcome are per syncer: they belong to
    the provider, not to the account.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_syncers", help_text="The organization this syncer belongs to.")
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="syncers", help_text="The account it feeds.")
    connection = models.ForeignKey(BankConnection, on_delete=models.SET_NULL, null=True, blank=True, related_name="syncers", help_text="The consent or login it is currently reached through.")
    backend = models.CharField(max_length=20, choices=Provider.choices, help_text="Which provider it pulls from.")
    remote_id = models.CharField(max_length=200, help_text="The account's id at the provider within the current connection (EB: the session's account uid; Scalable: the portfolio or savings id).")
    identification_key = models.CharField(max_length=500, help_text="Stable identity at the provider across connections.")
    raw = models.JSONField(default=dict, blank=True, help_text="The account as the provider returned it.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the syncer was first linked.")
    last_synced_at = models.DateTimeField(null=True, blank=True, help_text="When the last successful sync finished.")
    sync_lease_until = models.DateTimeField(null=True, blank=True, help_text="Held by a running sync until then; a crashed sync frees it once passed.")
    sync_day = models.DateField(null=True, blank=True, help_text="The UTC day ``syncs_today`` refers to.")
    syncs_today = models.PositiveIntegerField(default=0, help_text="Syncs that reached the provider on ``sync_day`` (a provider's daily limit counts these).")
    rate_limited_until = models.DateTimeField(null=True, blank=True, help_text="The provider rate-limited this syncer; no sync is attempted before then.")
    last_error = models.TextField(null=True, blank=True, help_text="Why the last sync failed, if it did.")
    last_error_code = models.CharField(max_length=30, null=True, blank=True, help_text="The machine-readable kind of ``last_error`` (a BankErrorCode).")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "backend", "identification_key"], name="bank_sync_org_backend_ident"),
        ]

    def __str__(self) -> str:
        return f"{self.backend} {self.remote_id} → account {self.account_id}"


class Category(EmbeddedDescriptionMixin, models.Model):
    """A spending or income category. Categories nest (groceries under food).

    A category's ``kind`` is always its root's: the whole subtree is expense, income or
    transfer, so stats and budgets never disagree about a child. Base categories carry the
    ``key`` of their node in :mod:`finance.taxonomy`; everything about them stays editable.
    Name + description are embedded, which is what lets a never-seen merchant land in the
    right category (:mod:`finance.semantic`).
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_categories", help_text="The organization this category belongs to.")
    name = models.CharField(max_length=200, help_text="The category's name, unique among its siblings.")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True, related_name="children", help_text="The parent category, if nested.")
    color = models.CharField(max_length=20, null=True, blank=True, help_text="A display color (e.g. ``#4f46e5``).")
    kind = models.CharField(max_length=20, choices=CategoryKind.choices, default=CategoryKind.EXPENSE, help_text="Whether money in this category is an expense, income, or a transfer between own accounts. Always the root's kind.")
    key = models.CharField(max_length=100, null=True, blank=True, help_text="The base-taxonomy key (``food.groceries``) of a base category; null for the organization's own.")
    description = models.TextField(blank=True, default="", help_text="What belongs here, in words a bank line would use. Embedded with the name for suggestions.")
    hidden = models.BooleanField(default=False, help_text="Hidden from pickers, suggestions and automatic assignment; its transactions keep it.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the category was created.")
    # The vector rotates with every text edit and model change: keep it out of the history rows.
    provenance = ProvenanceField(excluded_fields=list(EMBEDDING_FIELDS))

    embedding_source_fields = ("name", "description")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "parent", "name"], name="bank_cat_org_parent_name", nulls_distinct=False),
            models.UniqueConstraint(fields=["organization", "key"], condition=models.Q(key__isnull=False), name="bank_cat_org_key"),
        ]
        indexes = [*embedding_indexes("bank_cat")]

    def __str__(self) -> str:
        return self.name

    def root_kind(self) -> str:
        """The kind of this category's root (which is the kind the whole subtree has)."""
        node = self
        while node.parent_id is not None:
            node = node.parent
        return node.kind

    def save(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        """Save, and rebuild the category's terms when its name or description changed."""
        from finance.textnorm import category_terms

        before = getattr(self, "_terms_source", None)
        super().save(*args, **kwargs)
        now = (self.name, self.description)
        if before != now:
            from finance.semantic import sync_terms

            sync_terms(self, category_terms(self.name, self.description))
        self._terms_source = now

    @classmethod
    def from_db(cls, db, field_names, values):  # noqa: ANN001, ANN206
        instance = super().from_db(db, field_names, values)
        deferred = instance.get_deferred_fields()
        if "name" not in deferred and "description" not in deferred:
            instance._terms_source = (instance.name, instance.description)
        return instance


class CategoryTerm(EmbeddedDescriptionMixin, models.Model):
    """One phrase a category is recognized by — its name, or one phrase of its description.

    Embedded one by one (normalized like a bank line), so "Hofer" in Groceries' description
    meets a "HOFER DANKT 0815" line exactly instead of being averaged away among twenty other
    words. Rebuilt whenever the category's name or description changes.
    """

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="terms", help_text="The category.")
    text = models.CharField(max_length=300, help_text="The phrase, as written in the name or description.")

    embedding_source_fields = ("text",)
    embedding_organization_path = "category__organization"

    class Meta:
        constraints = [models.UniqueConstraint(fields=["category", "text"], name="bank_term_cat_text")]
        indexes = [*embedding_indexes("bank_term")]

    def __str__(self) -> str:
        return self.text

    def embedding_source_text(self) -> str | None:
        from finance.textnorm import term_text

        return term_text(self.text)


class DismissedBaseCategory(models.Model):
    """A base category the organization deleted: seeding does not bring it back."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_dismissed_categories", help_text="The organization.")
    key = models.CharField(max_length=100, help_text="The dismissed base-taxonomy key.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When it was dismissed.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "key"], name="bank_dismissed_org_key")]

    def __str__(self) -> str:
        return self.key


class CategoryRule(models.Model):
    """Assigns a category to matching transactions on import (never to manually categorized ones)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_rules", help_text="The organization this rule belongs to.")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="rules", help_text="The category matching transactions get.")
    priority = models.IntegerField(default=100, help_text="Lower runs first; the first matching rule wins.")
    field = models.CharField(max_length=20, choices=RuleField.choices, help_text="The transaction field the pattern is matched against.")
    match = models.CharField(max_length=20, choices=RuleMatch.choices, default=RuleMatch.CONTAINS, help_text="How the pattern is compared (case-insensitive).")
    pattern = models.CharField(max_length=1000, help_text="The text or regular expression to match.")
    direction = models.CharField(max_length=10, choices=RuleDirection.choices, default=RuleDirection.ANY, help_text="Only match money in, money out, or both.")
    amount_min = money(null=True, blank=True, help_text="Only match when the absolute amount is at least this.")
    amount_max = money(null=True, blank=True, help_text="Only match when the absolute amount is at most this.")
    active = models.BooleanField(default=True, help_text="Inactive rules are skipped.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the rule was created.")
    provenance = ProvenanceField()

    def __str__(self) -> str:
        return f"{self.field} {self.match} {self.pattern!r} → {self.category_id}"


class StatementImport(models.Model):
    """One uploaded statement export (a Finanzguru file), previewed and then applied.

    Preview parses the file and matches every row against the accounts and their synced history
    without writing a transaction; ``report`` says what applying would do. Applying writes it — idempotently:
    a row already imported (by its ``import_ref``) is updated, never duplicated.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_imports", help_text="The organization this import belongs to.")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_imports", help_text="The user who uploaded the file.")
    source = models.CharField(max_length=20, choices=ImportSource.choices, default=ImportSource.FINANZGURU, help_text="Which app or format the file came from.")
    file = models.ForeignKey("datalayer.BigFileStore", on_delete=models.SET_NULL, null=True, blank=True, related_name="bank_imports", help_text="The uploaded file (null for an import of a local file by the management command).")
    file_name = models.CharField(max_length=1000, null=True, blank=True, help_text="The file's name as uploaded.")
    status = models.CharField(max_length=20, choices=ImportStatus.choices, default=ImportStatus.PREVIEWED, help_text="Previewed, applied, or failed.")
    report = models.JSONField(default=dict, blank=True, help_text="What the import found and did: per source account its target and counts, unmapped categories, warnings.")
    error = models.TextField(null=True, blank=True, help_text="Why the file could not be read, if it could not.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the file was previewed.")
    applied_at = models.DateTimeField(null=True, blank=True, help_text="When the import was (last) applied.")
    provenance = ProvenanceField()

    def __str__(self) -> str:
        return f"{self.source} import {self.pk} [{self.status}]"


class ImportCategoryMapping(models.Model):
    """Which of the organization's categories an imported (main, sub) category pair means.

    Proposed on first sight (from the base taxonomy, else by similarity), editable, and used
    on every later import: the same Finanzguru category always lands in the same place. A null
    ``category`` leaves such rows to rules and suggestions.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_import_mappings", help_text="The organization this mapping belongs to.")
    source = models.CharField(max_length=20, choices=ImportSource.choices, default=ImportSource.FINANZGURU, help_text="The app whose categories this maps.")
    main = models.CharField(max_length=200, help_text="The app's main category (Finanzguru: Analyse-Hauptkategorie).")
    sub = models.CharField(max_length=200, blank=True, default="", help_text="The app's subcategory (Finanzguru: Analyse-Unterkategorie); empty if none.")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="import_mappings", help_text="The category rows with this pair get; null leaves them to rules and suggestions.")
    proposed = models.BooleanField(default=True, help_text="Still the automatic proposal; false once a user set it.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the pair was first seen.")
    provenance = ProvenanceField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "source", "main", "sub"], name="bank_impmap_org_src_pair")]

    def __str__(self) -> str:
        return f"{self.main} / {self.sub} → {self.category_id}"


class Transaction(EmbeddedDescriptionMixin, models.Model):
    """A single booked or pending transaction on an account.

    Counterparty, remittance and provider kind are embedded: similar transactions (the same
    merchant, the same kind of payment) sit close together, which drives ``search``,
    ``similarTransactions`` and category suggestions (:mod:`finance.semantic`).

    Bank fields are overwritten by every sync; ``category``, ``note`` and ``is_transfer``
    belong to the users and survive re-syncs of booked rows (see :mod:`finance.sync`).

    A row is SYNC (a syncer wrote it) or IMPORT (a statement import did). A sync that finds the
    same booking as an IMPORT row takes that row over instead of adding a second one
    (:mod:`finance.matching`), keeping its category, note and ``import_raw``.
    """

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="transactions", help_text="The account the transaction is on.")
    fingerprint = models.CharField(max_length=200, help_text="Stable identity across syncs: the bank's entry reference, else a content hash plus an occurrence index.")
    booking_date = models.DateField(null=True, blank=True, help_text="When the bank booked it.")
    value_date = models.DateField(null=True, blank=True, help_text="When it took effect for interest.")
    transaction_date = models.DateField(null=True, blank=True, help_text="When it was made (e.g. the card payment).")
    amount = money(help_text="Signed amount: negative is money out.")
    currency = models.CharField(max_length=3, help_text="ISO currency of the amount.")
    status = models.CharField(max_length=4, choices=TransactionStatus.choices, default=TransactionStatus.BOOKED, help_text="Booked or pending.")
    counterparty = models.CharField(max_length=1000, null=True, blank=True, help_text="Who was paid, or who paid.")
    counterparty_iban = models.CharField(max_length=64, null=True, blank=True, help_text="The counterparty's IBAN, if reported.")
    remittance = models.TextField(null=True, blank=True, help_text="The remittance information (purpose line).")
    entry_reference = models.CharField(max_length=200, null=True, blank=True, help_text="The bank's own reference, when it sends one.")
    kind = models.CharField(max_length=40, choices=TransactionKind.choices, null=True, blank=True, help_text="The provider's transaction type (Scalable: BUY, SELL, DEPOSIT, DISTRIBUTION, …); null for bank transactions.")
    isin = models.CharField(max_length=12, null=True, blank=True, help_text="The security traded or paying out, if any.")
    quantity = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True, help_text="Units of the security, if any.")
    raw = models.JSONField(default=dict, blank=True, help_text="The transaction as the provider returned it.")
    origin = models.CharField(max_length=10, choices=TransactionOrigin.choices, default=TransactionOrigin.SYNC, help_text="Whether the bank fields were synced or imported.")
    syncer = models.ForeignKey(AccountSyncer, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", help_text="The syncer that last wrote the bank fields (null for imported rows).")
    statement_import = models.ForeignKey(StatementImport, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", help_text="The import that last brought this booking in (kept when a sync takes the row over).")
    import_ref = models.CharField(max_length=200, null=True, blank=True, help_text="The booking's id in the imported file (``fg:<Buchungs-ID>``): what makes re-importing the same file idempotent.")
    import_raw = models.JSONField(default=dict, blank=True, help_text="What the import knew beyond the bank fields (the app's analysis: type, contract, tags, split parts). Survives a sync taking the row over.")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", help_text="The transaction's category.")
    category_source = models.CharField(max_length=10, choices=CategorySource.choices, default=CategorySource.NONE, help_text="Who set the category; rules never override a manual one.")
    note = models.TextField(null=True, blank=True, help_text="A user's note.")
    is_transfer = models.BooleanField(default=False, help_text="Money moved between own accounts; excluded from stats.")
    is_transfer_manual = models.BooleanField(default=False, help_text="``is_transfer`` was set by a user and is not recomputed.")
    merchant = models.ForeignKey("Merchant", on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", help_text="Who the transaction was with.")
    merchant_location = models.ForeignKey("MerchantLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", help_text="Where: the merchant's store, when the line names one.")
    merchant_source = models.CharField(max_length=10, choices=MerchantSource.choices, default=MerchantSource.NONE, help_text="How the merchant was set; matching never overrides MANUAL.")
    merchant_context = models.TextField(blank=True, default="", help_text="What the merchant and place add to the embedding (name, short description, default category, place name, city); kept in step by finance.merchants.refresh_context.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When this service first saw the transaction.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When the row last changed.")

    embedding_source_fields = ("counterparty", "remittance", "kind", "merchant_context")
    embedding_organization_path = "account__organization"

    def embedding_source_text(self) -> str | None:
        """The bank line (normalized) plus its merchant context (see :mod:`finance.textnorm`)."""
        from finance.textnorm import transaction_text

        return transaction_text(self.counterparty, self.remittance, self.kind, self.merchant_context)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "fingerprint"], name="bank_tx_acct_fp_uniq"),
            models.UniqueConstraint(fields=["account", "import_ref"], condition=models.Q(import_ref__isnull=False), name="bank_tx_acct_import_ref"),
        ]
        indexes = [
            *embedding_indexes("bank_tx"),
            models.Index(fields=["account", "booking_date"], name="bank_tx_acct_date"),
            models.Index(fields=["category"], name="bank_tx_category"),
            models.Index(fields=["syncer", "booking_date"], name="bank_tx_syncer_date"),
        ]

    def __str__(self) -> str:
        return f"{self.booking_date} {self.amount} {self.currency} {self.counterparty or ''}".strip()


class BalanceSnapshot(models.Model):
    """An account balance as the bank reported it on a day."""

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="balances", help_text="The account.")
    date = models.DateField(help_text="The day the balance refers to.")
    balance_type = models.CharField(max_length=10, help_text="The ISO 20022 balance type (CLBD closing booked, ITAV interim available, …).")
    amount = money(help_text="The balance.")
    currency = models.CharField(max_length=3, help_text="ISO currency of the balance.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the snapshot was taken.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "date", "balance_type"], name="bank_bal_acct_date_type"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.balance_type} {self.amount} {self.currency}"


class HoldingSnapshot(models.Model):
    """One security position in a depot, as it stood on a day. One row per day and ISIN gives the history."""

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="holdings", help_text="The depot.")
    date = models.DateField(help_text="The day the position refers to.")
    isin = models.CharField(max_length=12, help_text="The security's ISIN.")
    name = models.CharField(max_length=1000, help_text="The security's name.")
    security_type = models.CharField(max_length=40, null=True, blank=True, help_text="ETF, STOCK, FUND, CRYPTO, …")
    quantity = models.DecimalField(max_digits=24, decimal_places=8, help_text="Units held (filled).")
    fifo_price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True, help_text="Average buy-in price per unit (FIFO).")
    price = models.DecimalField(max_digits=24, decimal_places=8, null=True, blank=True, help_text="The last quoted mid price per unit.")
    valuation = money(help_text="The position's market value.")
    currency = models.CharField(max_length=3, help_text="ISO currency of the valuation.")
    raw = models.JSONField(default=dict, blank=True, help_text="The position as the provider returned it.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the snapshot was first taken.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When the snapshot was last refreshed (a day can be synced several times).")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "date", "isin"], name="bank_hold_acct_date_isin"),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.isin} {self.quantity} = {self.valuation} {self.currency}"


class Budget(models.Model):
    """A monthly spending limit for a category (and its children), in one currency."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_budgets", help_text="The organization this budget belongs to.")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="budgets", help_text="The budgeted category; child categories count towards it.")
    amount = money(help_text="The monthly limit, as a positive amount.")
    currency = models.CharField(max_length=3, help_text="ISO currency of the limit; only transactions in it count.")
    start_month = models.DateField(help_text="First month the budget applies to (the first day of it).")
    end_month = models.DateField(null=True, blank=True, help_text="Last month the budget applies to, or open-ended.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the budget was created.")
    provenance = ProvenanceField()

    def __str__(self) -> str:
        return f"{self.category_id}: {self.amount} {self.currency}/month"


class RecurringPayment(models.Model):
    """A payment detected to repeat at a regular interval (rent, salary, a subscription)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_recurring", help_text="The organization this belongs to.")
    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="recurring_payments", help_text="The account it recurs on.")
    key = models.CharField(max_length=500, help_text="Normalized counterparty plus direction; identity across re-detection.")
    label = models.CharField(max_length=1000, help_text="A readable name (the counterparty as last seen).")
    amount = money(help_text="The typical (median) signed amount.")
    currency = models.CharField(max_length=3, help_text="ISO currency.")
    interval_days = models.PositiveIntegerField(help_text="Days between occurrences (7, 14, 30, 91 or 365).")
    occurrences = models.PositiveIntegerField(default=0, help_text="How many matching transactions were found.")
    last_seen = models.DateField(help_text="The date of the latest occurrence.")
    next_expected = models.DateField(help_text="When the next occurrence is expected.")
    status = models.CharField(max_length=20, choices=RecurringStatus.choices, default=RecurringStatus.DETECTED, help_text="Whether a user confirmed or ignored it.")
    transactions = models.ManyToManyField(Transaction, related_name="recurring_payments", blank=True, help_text="The transactions that make up the pattern.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When it was first detected.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When it was last re-detected or reviewed.")
    provenance = ProvenanceField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "key", "currency"], name="bank_rec_acct_key_cur"),
        ]

    def __str__(self) -> str:
        return f"{self.label} every {self.interval_days}d"


class Merchant(EmbeddedDescriptionMixin, models.Model):
    """Someone the organization pays or is paid by — "Spar", "Wiener Linien", the landlord.

    Recognized on bank lines by its :class:`MerchantAlias` rows (see :mod:`finance.merchants`).
    A merchant may carry a default ``category`` for its transactions (source MERCHANT: after
    rules, before semantic guesses). Name + description are embedded for semantic ``search``.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_merchants", help_text="The organization this merchant belongs to.")
    name = models.CharField(max_length=300, help_text="The merchant's display name.")
    key = models.CharField(max_length=300, help_text="The normalized name; unique per organization.")
    description = models.TextField(blank=True, default="", help_text="Notes about the merchant.")
    website = models.URLField(max_length=500, null=True, blank=True, help_text="The merchant's website.")
    logo_url = models.URLField(max_length=1000, null=True, blank=True, help_text="An image URL for the merchant's logo.")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="merchants", help_text="The default category of its transactions (source MERCHANT).")
    online = models.BooleanField(default=False, help_text="Online only: no physical stores.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the merchant was created.")
    provenance = ProvenanceField(excluded_fields=list(EMBEDDING_FIELDS))

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "key"], name="bank_merch_org_key")]
        indexes = [*embedding_indexes("bank_merch")]

    def __str__(self) -> str:
        return self.name


class MerchantAlias(models.Model):
    """A normalized counterparty prefix that means this merchant ("spar", "spar gourmet").

    Unique per organization: one text means one merchant. The longest alias whose words start a
    line's key wins.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_merchant_aliases", help_text="The organization.")
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="aliases", help_text="The merchant it means.")
    pattern = models.CharField(max_length=300, help_text="Normalized words (lower-case, no numbers or boilerplate) a line's key must start with.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the alias was added.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "pattern"], name="bank_malias_org_pattern")]

    def __str__(self) -> str:
        return self.pattern


class MerchantLocation(models.Model):
    """A place of a merchant: a store, a branch, a station.

    ``latitude``/``longitude`` are what the API reads and writes; ``point`` is the PostGIS
    geography Postgres generates from them (GiST-indexed) for ``near`` queries — see
    :mod:`finance.geo`. A location discovered from a store number has no address until a user
    fills it in or geocodes it.
    """

    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="locations", help_text="The merchant.")
    name = models.CharField(max_length=300, help_text="A display name (e.g. 'Spar 3418', 'Spar Mariahilfer Straße').")
    store_code = models.CharField(max_length=20, null=True, blank=True, help_text="The store number bank lines carry for this place; unique per merchant.")
    street = models.CharField(max_length=300, null=True, blank=True, help_text="Street and number.")
    postal_code = models.CharField(max_length=20, null=True, blank=True, help_text="Postal code.")
    city = models.CharField(max_length=200, null=True, blank=True, help_text="City.")
    region = models.CharField(max_length=200, null=True, blank=True, help_text="State or region.")
    country = models.CharField(max_length=2, null=True, blank=True, help_text="ISO 3166-1 alpha-2 country code.")
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, help_text="WGS84 latitude.")
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, help_text="WGS84 longitude.")
    point = models.GeneratedField(
        expression=MakePoint(models.F("longitude"), models.F("latitude")),
        output_field=GeographyPointField(),
        db_persist=True,
        help_text="The PostGIS point of latitude/longitude (NULL until both are known); what `near` queries use.",
    )
    source = models.CharField(max_length=12, choices=LocationSource.choices, default=LocationSource.MANUAL, help_text="Where the location came from.")
    osm_id = models.CharField(max_length=40, null=True, blank=True, help_text="The OpenStreetMap object it was geocoded to (N123, W456).")
    geocoded_at = models.DateTimeField(null=True, blank=True, help_text="When it was last geocoded.")
    notes = models.TextField(blank=True, default="", help_text="Notes.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the location was created.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["merchant", "store_code"], condition=models.Q(store_code__isnull=False), name="bank_loc_merch_code")]
        indexes = [GistIndex(fields=["point"], name="bank_loc_point_gist")]

    def __str__(self) -> str:
        return self.name


class MerchantRule(models.Model):
    """Maps matching transactions to a merchant — the explicit counterpart of aliases.

    Same matching as :class:`CategoryRule` (field, match, pattern, direction, amount range;
    :func:`finance.rules.matches`): e.g. the landlord's IBAN, or a remittance line that names a
    shop. The first active rule by priority wins, and rules win over aliases; a user's manual
    link wins over both. A rule may pin a place (``location``); otherwise a store number on the
    line picks or discovers one, as with aliases.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_merchant_rules", help_text="The organization this rule belongs to.")
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="rules", help_text="The merchant matching transactions get.")
    location = models.ForeignKey(MerchantLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="rules", help_text="Pin this place (else a store number on the line decides).")
    priority = models.IntegerField(default=100, help_text="Lower runs first; the first matching rule wins.")
    field = models.CharField(max_length=20, choices=RuleField.choices, help_text="The transaction field the pattern is matched against.")
    match = models.CharField(max_length=20, choices=RuleMatch.choices, default=RuleMatch.CONTAINS, help_text="How the pattern is compared (case-insensitive).")
    pattern = models.CharField(max_length=1000, help_text="The text or regular expression to match.")
    direction = models.CharField(max_length=10, choices=RuleDirection.choices, default=RuleDirection.ANY, help_text="Only match money in, money out, or both.")
    amount_min = money(null=True, blank=True, help_text="Only match when the absolute amount is at least this.")
    amount_max = money(null=True, blank=True, help_text="Only match when the absolute amount is at most this.")
    active = models.BooleanField(default=True, help_text="Inactive rules are skipped.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the rule was created.")
    provenance = ProvenanceField()

    def __str__(self) -> str:
        return f"{self.field} {self.match} {self.pattern!r} → merchant {self.merchant_id}"


class PriceSource(models.TextChoices):
    """Where security prices come from."""

    SCALABLE = "SCALABLE", "Scalable Capital's official API (the organization's own login)"
    TWELVEDATA = "TWELVEDATA", "Twelve Data (public REST API, API key)"
    YAHOO = "YAHOO", "Yahoo Finance (unofficial public endpoints, no key; may break or rate-limit)"


class SecurityListing(models.Model):
    """Which listing an organization prices an ISIN by, per source.

    One ISIN trades on many exchanges in several currencies (VWCE on Xetra in EUR, VWRA in London
    in USD), so prices belong to a listing — a source's ``symbol`` — not to the ISIN. Listings are
    resolved through OpenFIGI with a preferred exchange; ``pinned`` marks one a user chose, which
    resolution never overwrites.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bank_security_listings", help_text="The organization.")
    isin = models.CharField(max_length=12, help_text="The security.")
    source = models.CharField(max_length=12, choices=PriceSource.choices, help_text="The price source this listing is for.")
    symbol = models.CharField(max_length=40, help_text="The source's symbol (Scalable: the ISIN; Yahoo: VWCE.DE; Twelve Data: VWCE).")
    exchange = models.CharField(max_length=20, null=True, blank=True, help_text="The exchange (Twelve Data: the MIC, e.g. XETR; else the OpenFIGI exchange code).")
    name = models.CharField(max_length=300, null=True, blank=True, help_text="The security's name as the source knows it.")
    currency = models.CharField(max_length=3, null=True, blank=True, help_text="The listing's trading currency, once a price was fetched.")
    pinned = models.BooleanField(default=False, help_text="Chosen by a user: automatic resolution never changes it.")
    resolved_at = models.DateTimeField(null=True, blank=True, help_text="When it was last resolved.")
    fetched_at = models.DateTimeField(null=True, blank=True, help_text="When prices were last fetched for it.")
    last_error = models.TextField(null=True, blank=True, help_text="Why the last fetch failed, if it did.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "isin", "source"], name="bank_listing_org_isin_src")]

    def __str__(self) -> str:
        return f"{self.isin} → {self.source}:{self.symbol}"


class SecurityPrice(models.Model):
    """A listing's closing price on a day — public market data, shared by every organization.

    Keyed by (source, symbol, date), never by ISIN (see :class:`SecurityListing`). Registered in
    ``finance.scoping.UNSCOPED_MODELS``: it holds nothing of an organization's, and every read
    goes through an organization's listings.
    """

    source = models.CharField(max_length=12, choices=PriceSource.choices, help_text="Where the price came from.")
    symbol = models.CharField(max_length=40, help_text="The source's symbol.")
    date = models.DateField(help_text="The trading day.")
    close = models.DecimalField(max_digits=24, decimal_places=8, help_text="The day's closing (or last) price.")
    currency = models.CharField(max_length=3, help_text="ISO currency of the price.")
    fetched_at = models.DateTimeField(auto_now=True, help_text="When it was last fetched.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "symbol", "date"], name="bank_price_src_sym_date")]

    def __str__(self) -> str:
        return f"{self.source}:{self.symbol} {self.date} {self.close} {self.currency}"

