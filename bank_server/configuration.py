"""Typed, fully-documented configuration schema for the **bank** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (the mount's ``config.yaml`` by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ByteSize, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from authentikate.base_models import AuthentikateSettings

_DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)


class AdminSettings(BaseModel):
    """Django superuser created on first boot."""

    username: str = Field(description="Superuser login name.")
    password: str = Field(description="Superuser password. Secret — must be set.")
    email: Optional[str] = Field(default=None, description="Superuser email address.")


class DjangoSettings(BaseModel):
    """Core Django framework settings."""

    secret_key: str = Field(description="Django SECRET_KEY for cryptographic signing. Secret — must be set.")
    debug: bool = Field(default=False, description="Enable Django debug mode (never in production).")
    log_level: str = Field(default="INFO", description="Root logger level (e.g. DEBUG, INFO, WARNING). The LOG_LEVEL env var overrides it.")
    enable_rich_logging: bool = Field(default=False, description="Render console logs with rich (colours, boxed tracebacks). A dev convenience; off by default, as plain one-line records suit container logs.")
    hosts: List[str] = Field(default_factory=lambda: ["*"], description="ALLOWED_HOSTS entries.")
    use_x_forwarded_host: bool = Field(default=True, description="Trust the X-Forwarded-Host header behind a reverse proxy.")
    admin: Optional[AdminSettings] = Field(default=None, description="Superuser provisioned on first boot.")
    csrf_trusted_origins: List[str] = Field(default_factory=lambda: ["http://localhost", "https://localhost"], description="CSRF_TRUSTED_ORIGINS for unsafe (POST) requests.")
    force_script_name: str = Field(default="", description="URL path prefix (FORCE_SCRIPT_NAME) this service is served under.")


class PostgresSettings(BaseModel):
    """PostgreSQL database connection (Django ``DATABASES['default']``)."""

    model_config = ConfigDict(extra="allow")

    engine: str = Field(default="django.db.backends.postgresql", description="Django database backend (PostgreSQL).")
    db_name: str = Field(description="Database name.")
    username: str = Field(description="Database user.")
    password: str = Field(description="Database password. Secret — must be set.")
    host: str = Field(description="Database host.")
    port: int = Field(default=5432, description="Database port.")


class RedisSettings(BaseModel):
    """Redis connection (channel layer / cache)."""

    model_config = ConfigDict(extra="allow")

    host: str = Field(description="Redis host.")
    port: int = Field(default=6379, description="Redis port.")


class EnableBankingSettings(BaseModel):
    """Enable Banking (PSD2 aggregator) application credentials.

    One Enable Banking application per deployment. Its private key is read from a
    mounted file at request time; it is never inlined in config nor committed.
    """

    app_id: str = Field(description="The Enable Banking application id. Also the ``kid`` of every JWT this service signs.")
    private_key_path: str = Field(description="Path to the application's RS256 private key (``<app-id>.pem``). Secret — mount it, never commit it.")
    api_url: str = Field(default="https://api.enablebanking.com", description="Base URL of the Enable Banking API.")
    redirect_urls: List[str] = Field(
        default_factory=lambda: ["https://johannesroos.de/callback"],
        description="Redirect URLs registered for the application. A client may pick one per link; the first is the default. Anything else is refused.",
    )
    consent_days: int = Field(default=90, description="How long a new bank consent is requested for, in days (banks may cap it).")
    psu_type: str = Field(default="personal", description="PSU type sent on authorization: ``personal`` or ``business``.")
    timeout_seconds: float = Field(default=60, description="Timeout for a single Enable Banking request.")
    daily_sync_limit: int = Field(default=4, description="Syncs per account per UTC day before the service stops asking the bank (banks cap PSD2 access at about 4 a day). Clients see the budget as `syncsRemainingToday`.")


class ScalableSettings(BaseModel):
    """Scalable Capital broker access through Scalable's official CLI API (OAuth device login + DPoP).

    The endpoint and client defaults are the official CLI's production channel. Linked
    accounts' credentials are stored encrypted with the Fernet key at ``secret_key_path``.
    """

    secret_key_path: str = Field(description="Path to a Fernet key (``Fernet.generate_key()``) encrypting stored Scalable credentials. Secret — mount it, never commit it. Losing it means relinking.")
    issuer: str = Field(default="https://secure.scalable.capital", description="The OAuth issuer (device code, token, revoke endpoints).")
    audience: str = Field(default="https://de.scalable.capital/api-gateway", description="The OAuth audience.")
    client_id: str = Field(default="yBM3BrpRgwSTJZRdJllvtD6jJEmyxWfE", description="The public OAuth client id of Scalable's CLI.")
    graphql_url: str = Field(default="https://de.scalable.capital/api/cli/graphql", description="Scalable's CLI GraphQL endpoint.")
    user_agent: str = Field(default="arkitekt-bank", description="User-Agent sent to Scalable.")
    daily_sync_limit: Optional[int] = Field(default=None, description="Syncs per account per UTC day; null is unlimited (Scalable is not a PSD2 consent).")
    timeout_seconds: float = Field(default=30, description="Timeout for a single Scalable request.")


class SyncSettings(BaseModel):
    """How a sync runs — requested by a client, or scheduled by the hub's rekuest (``rekuest_service``)."""

    lease_seconds: int = Field(default=600, description="How long a sync may hold an account before another request may take it over (a crashed sync frees it after this).")
    overlap_days: int = Field(default=7, description="An incremental sync re-fetches this many days before the newest booked transaction, so late-booked rows are caught.")
    scheduled_every_seconds: Optional[int] = Field(default=43200, description="No longer used: `sync_all_accounts` is only offered as an action, and scheduling it is the organization's own automation. Kept so existing configs load.")
    scheduled_reserve: int = Field(default=1, description="A scheduled sync skips an account with this many syncs (or fewer) left today, so a user can still sync by hand.")


class EmbeddingsSettings(BaseModel):
    """Semantic search and categorization: a model2vec static model embeds text into pgvector columns.

    Same block as rekuest/mikro/kabinet (the vendored ``embeddings`` package). Every value has a
    default, so the block may be omitted. The vector width is fixed by the model *and* by the
    database column; see CONFIG.md before changing ``model``.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    enabled: bool = Field(default=True, description="Embed transactions and categories and give `search` a semantic leg. Off: `search` is substring-only, suggestions come from rules only, and the embedding columns stay NULL.")
    model: str = Field(default="minishlab/potion-base-8M", description="model2vec model id. Recorded on every row; rows embedded by another model are re-embedded by the `reembed_stale` action and skipped by vector search until then.")
    model_path: Optional[str] = Field(default=None, description="Directory holding the weights of `model` (save_pretrained layout). The Docker image bakes them under /opt/models and sets EMBEDDINGS__MODEL_PATH; unset, model2vec downloads from Hugging Face on first use.")
    dimensions: int = Field(default=256, description="Vector width of `model`. Also the width of the database column, so changing it is a migration. Checked against both at startup.")
    distance_threshold: float = Field(default=0.55, description="Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic `search` hit.")
    sweep_interval: int = Field(default=300, description="No longer used: `reembed_stale` (which re-embeds rows whose `embedding_model` is not `model`) is only offered as an action, and scheduling it is the organization's own automation. Kept so existing configs load.")
    sweep_batch_size: int = Field(default=200, description="Rows re-embedded per batch.")


class CategorizationSettings(BaseModel):
    """How transactions get categories beyond rules: by similarity to what the organization already categorized."""

    auto_assign: bool = Field(default=True, description="During sync, give an uncategorized transaction its best suggestion (source SEMANTIC) when the suggestion is confident. Never overrides a manual category or a rule; rules override it.")
    auto_assign_threshold: float = Field(default=0.75, description="The share (0–1) of the similarity vote the top category needs for an automatic assignment.")
    min_evidence: int = Field(default=2, description="How many similar, already categorized transactions must back an automatic assignment (unless a category term matches near-exactly, see `term_assign_distance`).")
    neighbours: int = Field(default=25, description="How many similar categorized transactions vote on a suggestion.")
    vote_distance: float = Field(default=0.35, description="Cosine distance under which a categorized transaction counts as similar (and votes). Same merchant ≈ 0, same kind of payment ≈ 0.2–0.35.")
    term_distance: float = Field(default=0.35, description="Cosine distance under which a category term (its name or a description phrase) votes for its category.")
    term_weight: float = Field(default=1.0, description="A term's vote relative to one rule-categorized neighbour (a user-categorized one counts 2).")
    term_assign_distance: float = Field(default=0.15, description="A term this close (the merchant or keyword itself, e.g. 'hofer' → Groceries) is enough evidence for an automatic assignment on its own.")


class DatalayerBucket(BaseModel):
    """A single S3 bucket binding within the datalayer."""

    model_config = ConfigDict(extra="allow")

    bucket: str = Field(description="S3 bucket name.")
    default_max_bytes: Optional[ByteSize] = Field(default=None, description="Per-upload byte budget advertised on this bucket's grants when no quota sets `max_upload_bytes`. Accepts `500GiB`-style strings. Unset: 100 MiB.")


class QuotaLimits(BaseModel):
    """Byte limits at one level of the quota tree. Unset inherits from the level above; null at every level is unlimited.

    Byte values accept ints or strings such as ``500GiB`` / ``2TB``.
    """

    model_config = ConfigDict(extra="forbid")

    max_upload_bytes: Optional[ByteSize] = Field(default=None, description="Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused.")
    max_user_bytes: Optional[ByteSize] = Field(default=None, description="Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this.")


class OrganizationQuota(QuotaLimits):
    """Quota for one organization, plus per-user overrides inside it."""

    max_org_bytes: Optional[ByteSize] = Field(default=None, description="Total bytes the whole organization may hold.")
    users: Dict[str, QuotaLimits] = Field(default_factory=dict, description="Per-user overrides in this organization, keyed by the user's token `sub`.")


class QuotaSettings(BaseModel):
    """Upload quotas, set by the hub owner. Resolved most specific first: user in org, then org, then `default`."""

    model_config = ConfigDict(extra="forbid")

    default: OrganizationQuota = Field(default_factory=OrganizationQuota, description="Limits for every organization without its own entry (its `users` map is ignored).")
    organizations: Dict[str, OrganizationQuota] = Field(default_factory=dict, description="Per-organization quotas, keyed by the token `org` claim -- since authentikate 4.0 the lok organization *id* as a string (e.g. `3`), not a readable name.")


class DatalayerSettings(BaseModel):
    """S3 storage for uploaded files (the vendored ``datalayer`` app, as in mikro/elektro): statement exports to import.

    A client asks for a scoped upload grant (STS ``AssumeRole`` with a one-key session policy),
    writes the file straight to the bucket, and hands the store to an import mutation.
    """

    model_config = ConfigDict(extra="allow")

    access_key: str = Field(description="S3 access key. Secret — must be set.")
    secret_key: str = Field(description="S3 secret key. Secret — must be set.")
    host: Optional[str] = Field(default=None, description="S3 endpoint host.")
    port: Optional[int] = Field(default=None, description="S3 endpoint port.")
    protocol: str = Field(default="http", description="S3 endpoint protocol (http or https).")
    region: str = Field(default="us-east-1", description="S3 region name.")
    role_arn: Optional[str] = Field(default=None, description="The role upload grants assume. RustFS/MinIO ignore its value and scope the session by the inline policy alone, but STS needs one.")
    session_duration_seconds: int = Field(default=3600, description="How long an upload or read grant lasts (clamped to 900–43200).")
    bigfile: DatalayerBucket = Field(description="Bucket for uploaded files (statement exports).")
    upload_roles: List[str] = Field(default_factory=lambda: ["admin", "editor", "bot"], description="Organization roles allowed to request upload grants. Holding any one of them is enough.")
    quotas: QuotaSettings = Field(default_factory=QuotaSettings, description="Per-organization, per-user and per-upload byte quotas.")


class ImportSettings(BaseModel):
    """How imported statements (a Finanzguru export) merge with synced history."""

    match_window_days: int = Field(default=3, description="An imported and a synced row are the same booking when amount and currency agree and their booking days are at most this far apart (and their counterparty IBANs agree when both have one).")


class GeocodingSettings(BaseModel):
    """Address lookups for merchant locations, through a Nominatim (OpenStreetMap) server.

    Only ever called inside a request that asks (``geocodeSearch``, ``geocodeMerchantLocation``).
    The public server's usage policy wants an identifying User-Agent and at most one request a
    second; set ``url`` to a self-hosted Nominatim for anything heavier.
    """

    enabled: bool = Field(default=True, description="Allow address lookups. Off: the geocode mutations fail with NOT_CONFIGURED; coordinates can still be entered by hand.")
    url: str = Field(default="https://nominatim.openstreetmap.org", description="The Nominatim base URL.")
    user_agent: str = Field(default="arkitekt-bank (https://arkitekt.live)", description="The identifying User-Agent Nominatim's usage policy requires.")
    country_codes: Optional[str] = Field(default="at,de,it", description="Comma-separated ISO countries searches are limited to (null: worldwide).")
    language: str = Field(default="de,en", description="Accept-Language for place names.")
    timeout_seconds: float = Field(default=10, description="Timeout for one lookup.")


class PricesSettings(BaseModel):
    """Security prices for the depot, from Scalable and public APIs behind one interface (``finance.prices``).

    Fetched only inside a request that asks (a depot sync fetches Scalable's recent prices of the
    held ISINs; ``refreshSecurityPrices`` backfills from any source).
    """

    sources: List[str] = Field(default_factory=lambda: ["SCALABLE", "TWELVEDATA", "YAHOO"], description="Price sources in order of preference: SCALABLE, TWELVEDATA, YAHOO. A read uses the first with prices.")
    twelvedata_api_key: Optional[str] = Field(default=None, description="A Twelve Data API key (free at twelvedata.com). Without it the source is skipped. Secret.")
    twelvedata_url: str = Field(default="https://api.twelvedata.com", description="Twelve Data base URL.")
    yahoo_enabled: bool = Field(default=True, description="Use Yahoo Finance's unofficial chart endpoints (no key; may break or rate-limit, and scraping is against Yahoo's terms).")
    yahoo_url: str = Field(default="https://query1.finance.yahoo.com", description="Yahoo Finance base URL.")
    openfigi_url: str = Field(default="https://api.openfigi.com", description="OpenFIGI (ISIN → listings) base URL.")
    openfigi_api_key: Optional[str] = Field(default=None, description="An OpenFIGI API key (optional; raises its rate limit).")
    preferred_exchanges: List[str] = Field(
        default_factory=lambda: ["GY", "GR", "AV", "IM", "NA", "FP", "SW", "LN", "US", "UN", "UW"],
        description="OpenFIGI exchange codes to pick a listing from, most preferred first (GY Xetra, AV Vienna, IM Milan, LN London, US/UN/UW United States …).",
    )
    user_agent: str = Field(default="Mozilla/5.0 (compatible; arkitekt-bank)", description="User-Agent for the public APIs.")
    timeout_seconds: float = Field(default=15, description="Timeout of one request.")


class RekuestHookSettings(BaseModel):
    """How this process reaches the hub's rekuest: as a service (``rekuest_service``) and as a hook agent (``rekuest_hook``)."""

    rekuest_url: str = Field(default="http://rekuest:80/rekuest", description="rekuest's base URL on the internal network; runs are reported to its `agi/http/<agent>` intake.")
    service: str = Field(default="bank", description="The name rekuest knows this process by: its `rekuest.services[].name` (signals are sent as it) and its `rekuest.hook_agents[].name`.")
    max_skew: int = Field(default=30, description="Clock skew (seconds) tolerated on a signed request; tokens themselves live 60 s.")


class InstanceTrustSettings(BaseModel):
    """Where the hub's instance public keys come from: the coord's bundle, or inline."""

    jwks_uri: Optional[str] = Field(default=None, description="The coord's hub-keys URL (the fakts `self.hub_keys_url`).")
    jwks: Optional[Dict[str, Any]] = Field(default=None, description="The bundle inline (a JWKS whose keys carry `service`), for a hub not enrolled yet.")


class InstanceSettings(BaseModel):
    """This instance's key — its only secret towards the hub's other services — and whom it trusts."""

    private_key: str = Field(description="Ed25519 private key (PKCS#8 PEM). Signs this service's requests to rekuest. Secret — must be set.")
    trust: InstanceTrustSettings = Field(default_factory=InstanceTrustSettings, description="The hub's trust bundle.")


class Settings(BaseSettings):
    """Top-level, validated configuration for the bank service."""

    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
    enablebanking: Optional[EnableBankingSettings] = Field(default=None, description="Enable Banking credentials. Without them the service still serves stats, categories and budgets, but cannot link or sync banks.")
    scalable: Optional[ScalableSettings] = Field(default=None, description="Scalable Capital access. Without it Scalable cannot be linked.")
    sync: SyncSettings = Field(default_factory=SyncSettings, description="How syncs run.")
    embeddings: EmbeddingsSettings = Field(default_factory=EmbeddingsSettings, description="Semantic search model and thresholds.")
    categorization: CategorizationSettings = Field(default_factory=CategorizationSettings, description="Semantic categorization (suggestions and automatic assignment).")
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings, description="Address lookups for merchant locations.")
    prices: PricesSettings = Field(default_factory=PricesSettings, description="Security prices (Scalable, Twelve Data, Yahoo).")
    datalayer: Optional[DatalayerSettings] = Field(default=None, description="S3 storage for uploaded statement exports. Without it files cannot be uploaded (`import_finanzguru` still imports a local file).")
    imports: ImportSettings = Field(default_factory=ImportSettings, description="How imported statements merge with synced history.")
    rekuest_hook: Optional[RekuestHookSettings] = Field(default=None, description="Expose `sync_all_accounts` to the hub's rekuest, which schedules it. Without it nothing syncs unless a client asks.")
    instance: Optional[InstanceSettings] = Field(default=None, description="This instance's key and the hub trust bundle (signed requests to and from rekuest, no shared secrets).")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit init kwargs > environment variables > YAML file.
        path = os.environ.get("ARKITEKT_CONFIG_FILE", _DEFAULT_CONFIG)
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=path),
            file_secret_settings,
        )
