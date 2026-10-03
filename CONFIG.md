# Bank — Configuration Reference

This document explains how the **bank** service is configured, then lists every
configuration value, its environment-variable name, its default, and what it does.

The single source of truth for the schema is
[`bank_server/configuration.py`](bank_server/configuration.py); this file
documents it for humans. If the two ever disagree, the code wins — and you can always
print the live, resolved configuration with `python manage.py validate_settings` (see
below).

---

## How configuration works

Configuration is a typed [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
schema. Values are resolved from several sources, **highest precedence first**:

1. **Init kwargs** — values passed directly in code (rarely used; tests).
2. **Environment variables** — override anything in the YAML file.
3. **The YAML file** — [`config.yaml`](config.yaml) by default.
4. **File secrets** — Docker/systemd secret files, if used.

So an environment variable always beats the YAML file, which makes containerized
overrides easy without editing the mounted config.

### The YAML file

By default the service reads `config.yaml` next to the project. Point it elsewhere with
the `ARKITEKT_CONFIG_FILE` environment variable:

```bash
ARKITEKT_CONFIG_FILE=/etc/bank/config.yaml python manage.py runserver
```

The file is a nested mapping, one top-level key per configuration *block*:

```yaml
django:
  secret_key: "change-me"
  debug: false
postgres:
  db_name: bank
  username: bank
  password: "change-me"
  host: db
  port: 5432
redis:
  host: redis
  port: 6379
```

### Environment variables (the `__` rule)

Every value is also settable from the environment. The nesting is expressed with a
**double-underscore** (`__`) delimiter, and names are case-insensitive:

| YAML path | Environment variable |
|---|---|
| `postgres.password` | `POSTGRES__PASSWORD` |
| `postgres.port` | `POSTGRES__PORT` |
| `django.debug` | `DJANGO__DEBUG` |
| `redis.host` | `REDIS__HOST` |

Lists and nested objects (e.g. `authentikate.issuers`) are awkward
to express as environment variables — prefer the YAML file for those and use env vars
for the flat scalars (hosts, ports, passwords, toggles).

### Secrets fail fast

Fields marked **secret / required** below have **no default**. If they are missing from
both the YAML file and the environment, the service refuses to start and raises a
`pydantic.ValidationError` naming the missing field. The same error blocks
`manage.py` entirely, so a broken config cannot be deployed silently.

### Validating a configuration

Run the bundled command to load the config exactly as the app would, validate it, and
print the fully-resolved result as a tree with **secrets redacted**:

```bash
python manage.py validate_settings
```

- Valid config → prints a green `Configuration valid` tree and exits `0`.
- Invalid config → prints each offending field and its error, and exits `1`.

It honors `ARKITEKT_CONFIG_FILE`, so you can validate an alternate file the same way.
(Note: because Django loads settings on startup, a fundamentally invalid config also
surfaces the same validation errors when running *any* `manage.py` command.)

---

## Configuration reference

Secret fields are flagged with 🔒. "Required" means there is no default.

### `django` — core Django framework settings

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `secret_key` 🔒 | `DJANGO__SECRET_KEY` | str | **required** | Django `SECRET_KEY` for cryptographic signing. |
| `debug` | `DJANGO__DEBUG` | bool | `false` | Enable Django debug mode. Never enable in production. |
| `hosts` | `DJANGO__HOSTS` | list[str] | `["*"]` | `ALLOWED_HOSTS` entries. |
| `use_x_forwarded_host` | `DJANGO__USE_X_FORWARDED_HOST` | bool | `true` | Trust the `X-Forwarded-Host` header behind a reverse proxy. |
| `admin` | `DJANGO__ADMIN__*` | object | `null` | Superuser provisioned on first boot (see below). |
| `csrf_trusted_origins` | `DJANGO__CSRF_TRUSTED_ORIGINS` | list[str] | `["http://localhost", "https://localhost"]` | `CSRF_TRUSTED_ORIGINS` for unsafe (POST) requests. |
| `force_script_name` | `DJANGO__FORCE_SCRIPT_NAME` | str | `""` | URL path prefix this service is served under (`FORCE_SCRIPT_NAME`). |

#### `django.admin` — superuser created on first boot

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `username` | `DJANGO__ADMIN__USERNAME` | str | **required** | Superuser login name. |
| `password` 🔒 | `DJANGO__ADMIN__PASSWORD` | str | **required** | Superuser password. |
| `email` | `DJANGO__ADMIN__EMAIL` | str | `null` | Superuser email address. |

### `postgres` — PostgreSQL database (Django `DATABASES['default']`)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `engine` | `POSTGRES__ENGINE` | str | `django.db.backends.postgresql` | Django database backend. |
| `db_name` | `POSTGRES__DB_NAME` | str | **required** | Database name. |
| `username` | `POSTGRES__USERNAME` | str | **required** | Database user. |
| `password` 🔒 | `POSTGRES__PASSWORD` | str | **required** | Database password. |
| `host` | `POSTGRES__HOST` | str | **required** | Database host. |
| `port` | `POSTGRES__PORT` | int | `5432` | Database port. |

### `redis` — Redis connection (channel layer / cache)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `host` | `REDIS__HOST` | str | **required** | Redis host. |
| `port` | `REDIS__PORT` | int | `6379` | Redis port. |

### `authentikate` — inbound token verification

Configures how incoming JWT access tokens are verified (the shared `authentikate`
library). At least one issuer is required.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `issuers` | — (use YAML) | list[issuer] | **required** | Trusted token issuers whose keys verify incoming tokens (see issuer kinds below). |
| `authorization_headers` | `AUTHENTIKATE__AUTHORIZATION_HEADERS` | list[str] | `["Authorization", "X-Authorization", "AUTHORIZATION", "authorization"]` | Request headers searched (in order) for a Bearer token. |
| `static_tokens` | — (use YAML) | map | `{}` | Pre-defined tokens that bypass signature verification. **Tests only.** |

Each entry in `issuers` is discriminated by its `kind`:

- `kind: rsa` — inline PEM RSA public key. Fields: `iss`, `kid` (default `1`), `public_key`.
- `kind: rsa_file` — RSA public key read from a PEM file. Fields: `iss`, `kid`, `public_key_pem_file`.
- `kind: jwks_dict` — inline JWKS document. Fields: `iss`, `jwks` (a dict with a `keys` list).
- `kind: jwks_uri` — JWKS fetched from a remote endpoint. Fields: `iss`, `jwks_uri`.

```yaml
authentikate:
  issuers:
    - kind: rsa
      iss: lok
      kid: lok-key-1
      public_key: "ssh-rsa AAAA..."
  static_tokens: {}
```

### `enablebanking` — Enable Banking application (optional)

The PSD2 aggregator the service reaches banks through. One application per deployment. Without
this block the service still serves stats, categories and budgets, but `bankInstitutions`,
`startBankLink`, `completeBankLink` and syncs answer `NOT_CONFIGURED`.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `app_id` | `ENABLEBANKING__APP_ID` | str | **required** | The application id; also the `kid` of every JWT the service signs. |
| `private_key_path` 🔒 | `ENABLEBANKING__PRIVATE_KEY_PATH` | str | **required** | Path of the application's RS256 private key (`<app-id>.pem`). Mount it read-only; never commit it. |
| `api_url` | `ENABLEBANKING__API_URL` | str | `https://api.enablebanking.com` | Base URL of the API. |
| `redirect_urls` | — (use YAML) | list[str] | `["https://johannesroos.de/callback"]` | Redirect URLs registered for the application. A client may pick one per link (the first is the default); anything else is refused. |
| `consent_days` | `ENABLEBANKING__CONSENT_DAYS` | int | `90` | How long a new consent is requested for (banks may cap it). |
| `psu_type` | `ENABLEBANKING__PSU_TYPE` | str | `personal` | `personal` or `business`. |
| `timeout_seconds` | `ENABLEBANKING__TIMEOUT_SECONDS` | float | `60` | Timeout of one API request. |
| `daily_sync_limit` | `ENABLEBANKING__DAILY_SYNC_LIMIT` | int | `4` | Syncs per account per UTC day (PSD2); clients see it as `syncsRemainingToday`. |

### `sync` — how a sync runs

An account syncs when a client asks, inside that request — or when the hub's rekuest runs the
scheduled `sync_all_accounts` (needs `rekuest_hook`). This service itself never loops.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `lease_seconds` | `SYNC__LEASE_SECONDS` | int | `600` | How long a sync holds an account; a crashed sync frees it after this. |
| `overlap_days` | `SYNC__OVERLAP_DAYS` | int | `7` | An incremental sync re-fetches this many days before the newest booked transaction. |
| `scheduled_every_seconds` | `SYNC__SCHEDULED_EVERY_SECONDS` | int? | `43200` | No longer used: the action is only offered, and scheduling it is the organization's own automation in rekuest. Kept so existing configs load. |
| `scheduled_reserve` | `SYNC__SCHEDULED_RESERVE` | int | `1` | A scheduled sync skips an account with this many syncs (or fewer) left today, so a user can still sync by hand. |

### `rekuest_hook` — how this process reaches the hub's rekuest (optional)

Two separate things use it. The **service** (vendored `rekuest_service`, mounted at
`_rekuest/service`) says what exists here: rekuest lists it under `rekuest.services` and
catalogues its structures and signals. The **hook agent** (vendored `rekuest_hook`, mounted at
`_rekuest/hook`) offers this process's actions: rekuest lists it under `rekuest.hook_agents` and
gives every organization the agent. Nothing is scheduled by itself; when an action runs is the
organization's own automation. Keep `_rekuest/` off the public edge.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `rekuest_url` | `REKUEST_HOOK__REKUEST_URL` | str | `http://rekuest:80/rekuest` | rekuest on the internal network; runs are reported to its intake. |
| `service` | `REKUEST_HOOK__SERVICE` | str | `bank` | The name rekuest knows this process by: its `rekuest.services[].name` (signals are sent as it) and its `rekuest.hook_agents[].name`. |
| `max_skew` | `REKUEST_HOOK__MAX_SKEW` | int | `30` | Clock skew (seconds) tolerated on a signed request; tokens live 60 s. |

### `instance` — this instance's key and the hub trust bundle

No shared secrets: requests between this service and rekuest carry short-lived JWTs signed with
each side's instance key, checked against the hub's trust bundle (the coord-vouched public keys
of every instance). Konstruktor mints the key and enrolls its public half with the coord.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `private_key` | `INSTANCE__PRIVATE_KEY` | str | — | Ed25519 private key (PKCS#8 PEM). Secret — must be set. |
| `trust.jwks_uri` | `INSTANCE__TRUST__JWKS_URI` | str? | `null` | The coord's hub-keys URL (fakts `self.hub_keys_url`). |
| `trust.jwks` | — | object? | `null` | Or the bundle inline, for a hub not enrolled yet. |

### `scalable` — Scalable Capital (official CLI login)

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `secret_key_path` | `SCALABLE__SECRET_KEY_PATH` | str | — | Fernet key encrypting stored Scalable credentials. Secret — mount it. |
| `daily_sync_limit` | `SCALABLE__DAILY_SYNC_LIMIT` | int? | `null` | Syncs per account per UTC day; null is unlimited. |
| `issuer`, `audience`, `client_id`, `graphql_url` | `SCALABLE__…` | str | CLI prod | Scalable's OAuth issuer and CLI API. |
| `user_agent`, `timeout_seconds` | `SCALABLE__…` | | | Sent User-Agent; per-request timeout. |

---

### `datalayer` — S3 for uploaded statement exports (optional)

The vendored `datalayer` app (as in mikro/elektro), cut down to single files. A client imports a
Finanzguru export by `requestBigfileUpload` (an STS `AssumeRole` session whose inline policy
allows writing exactly one key) → S3 PUT → `finishBigfileUpload` → `createFinanzguruImport`.
Without this block uploads answer `NOT_CONFIGURED`; `python manage.py import_finanzguru <file>`
still imports a local file.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `access_key` / `secret_key` | `DATALAYER__ACCESS_KEY` / `DATALAYER__SECRET_KEY` | str | — | The RustFS user the service (and its grants) act as. Secret — must be set. |
| `host` / `port` / `protocol` | `DATALAYER__HOST` … | str / int / str | — / — / `http` | The S3 endpoint (`rustfs:9000` in the deployment). |
| `role_arn` | `DATALAYER__ROLE_ARN` | str? | — | Any ARN-shaped string: RustFS ignores it and scopes the session by the inline policy, but `AssumeRole` needs one. Unset, no grant can be issued. |
| `session_duration_seconds` | `DATALAYER__SESSION_DURATION_SECONDS` | int | `3600` | How long a grant lasts (clamped to 900–43200). |
| `bigfile.bucket` | `DATALAYER__BIGFILE__BUCKET` | str | — | The bucket uploads land in (`bank-imports`; created by `rustfs_init` from `configs/rustfs.yaml`). |
| `bigfile.default_max_bytes` | — | size? | 100 MiB | Per-upload byte budget advertised on grants when no quota sets one. |
| `upload_roles` | — | list | `[admin, editor, bot]` | Organization roles allowed to request upload grants. |
| `quotas` | — | object | unlimited | Per-organization / per-user byte quotas (same shape as mikro's). |

### `imports` — merging imported statements with synced history

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `match_window_days` | `IMPORTS__MATCH_WINDOW_DAYS` | int | `3` | An imported and a synced row are the same booking when amount and currency agree, their booking days are at most this far apart, and their counterparty IBANs agree when both have one. |

### `embeddings` — semantic search and categorization

Transactions (counterparty + remittance + kind, normalized: store numbers, dates and "DANKT"
dropped), categories (name + description) and category terms are embedded into pgvector
columns by a [model2vec](https://github.com/MinishLab/model2vec) static model running inside
the service (CPU, no extra service) — the vendored `embeddings` package, the same block as
rekuest, mikro and kabinet. Every key has a default; the block may be omitted.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `enabled` | `EMBEDDINGS__ENABLED` | bool | `true` | Off: `search` is substring-only, suggestions are empty, only rules categorize. |
| `model` | `EMBEDDINGS__MODEL` | str | `minishlab/potion-base-8M` | model2vec model id, recorded per row; rows of another model are re-embedded by `reembed_stale` and skipped by vector search until then. |
| `model_path` | `EMBEDDINGS__MODEL_PATH` | str | `null` | Weights directory. The image bakes them under `/opt/models/embeddings` and sets this (with `HF_HUB_OFFLINE=1`). |
| `dimensions` | `EMBEDDINGS__DIMENSIONS` | int | `256` | Vector width — also the column width (checked at startup, `embeddings.E001`/`E002`). |
| `distance_threshold` | `EMBEDDINGS__DISTANCE_THRESHOLD` | float | `0.55` | Cosine distance under which a row is a semantic `search` hit / "near" a category. |
| `sweep_interval` | `EMBEDDINGS__SWEEP_INTERVAL` | int | `300` | Default schedule of the `reembed_stale` rekuest action. |
| `sweep_batch_size` | `EMBEDDINGS__SWEEP_BATCH_SIZE` | int | `200` | Rows re-embedded per batch. |

### `categorization` — suggestions and automatic assignment

A transaction's likely categories come from two votes: similar transactions the organization
categorized (MANUAL counts double, SEMANTIC never votes) and category *terms* (the name and each
comma-separated phrase of the description, embedded one by one). Sync assigns the top one
(source `SEMANTIC`) when it is confident; rules override SEMANTIC, nothing overrides MANUAL.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `auto_assign` | `CATEGORIZATION__AUTO_ASSIGN` | bool | `true` | Assign confident suggestions during sync. Off: suggestions only. |
| `auto_assign_threshold` | `CATEGORIZATION__AUTO_ASSIGN_THRESHOLD` | float | `0.75` | Share of the vote the top category needs. |
| `min_evidence` | `CATEGORIZATION__MIN_EVIDENCE` | int | `2` | Agreeing categorized neighbours needed (or a near-exact term, below). |
| `neighbours` | `CATEGORIZATION__NEIGHBOURS` | int | `25` | Neighbours that vote. |
| `vote_distance` | `CATEGORIZATION__VOTE_DISTANCE` | float | `0.35` | Cosine distance under which a categorized transaction votes. |
| `term_distance` | `CATEGORIZATION__TERM_DISTANCE` | float | `0.35` | Cosine distance under which a term votes. |
| `term_weight` | `CATEGORIZATION__TERM_WEIGHT` | float | `1.0` | A term's vote relative to one rule-categorized neighbour. |
| `term_assign_distance` | `CATEGORIZATION__TERM_ASSIGN_DISTANCE` | float | `0.15` | A term this close (the merchant itself: "hofer" → Groceries) is enough evidence alone. |

### `geocoding` — address lookups for merchant locations

Only ever used inside a request that asks (`geocodeSearch`, `geocodeMerchantLocation`); nothing
geocodes on its own. The public Nominatim's usage policy wants an identifying User-Agent and at
most one request a second — point `url` at a self-hosted Nominatim for anything heavier.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `enabled` | `GEOCODING__ENABLED` | bool | `true` | Off: the geocode operations fail with `NOT_CONFIGURED`; coordinates can still be entered by hand. |
| `url` | `GEOCODING__URL` | str | `https://nominatim.openstreetmap.org` | Nominatim base URL. |
| `user_agent` | `GEOCODING__USER_AGENT` | str | `arkitekt-bank (https://arkitekt.live)` | Identifying User-Agent (required by the usage policy). |
| `country_codes` | `GEOCODING__COUNTRY_CODES` | str? | `at,de,it` | Limit searches to these ISO countries; null is worldwide. |
| `language` | `GEOCODING__LANGUAGE` | str | `de,en` | Accept-Language for place names. |
| `timeout_seconds` | `GEOCODING__TIMEOUT_SECONDS` | float | `10` | Timeout of one lookup. |

### `prices` — security prices (Scalable, Twelve Data, Yahoo)

One interface over three sources (`finance/prices`); listings are found per ISIN through
OpenFIGI (every exchange it trades on) and the first of `preferred_exchanges`. A depot sync stores
Scalable's last month for the held ISINs; `refreshSecurityPrices` backfills from every source.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `sources` | `PRICES__SOURCES` | list | `[SCALABLE, TWELVEDATA, YAHOO]` | Preference order; a read uses the first with prices. |
| `twelvedata_api_key` | `PRICES__TWELVEDATA_API_KEY` | str? | `null` | Free key from twelvedata.com; without it Twelve Data is skipped. Secret. |
| `yahoo_enabled` | `PRICES__YAHOO_ENABLED` | bool | `true` | Yahoo's unofficial chart endpoint (no key; may break; against Yahoo's terms). |
| `preferred_exchanges` | `PRICES__PREFERRED_EXCHANGES` | list | `[GY, GR, AV, IM, NA, FP, SW, LN, US, UN, UW]` | OpenFIGI exchange codes, most preferred first. |
| `openfigi_api_key` | `PRICES__OPENFIGI_API_KEY` | str? | `null` | Optional; raises OpenFIGI's rate limit. |
| `openfigi_url`, `yahoo_url`, `twelvedata_url`, `user_agent`, `timeout_seconds` | `PRICES__…` | | | Endpoints and client settings. |

## Minimal example

```yaml
django:
  secret_key: "REPLACE_ME"
  debug: false
  admin:
    username: admin
    password: "REPLACE_ME"
    email: admin@bank.test
postgres:
  db_name: bank
  username: bank
  password: "REPLACE_ME"
  host: db
  port: 5432
redis:
  host: redis
  port: 6379
authentikate:
  issuers:
    - kind: rsa
      iss: lok
      kid: lok-key-1
      public_key: "ssh-rsa AAAA..."
  static_tokens: {}
enablebanking:
  app_id: "REPLACE_ME"
  private_key_path: /secrets/enablebanking.pem
```

Validate it with `python manage.py validate_settings`.
