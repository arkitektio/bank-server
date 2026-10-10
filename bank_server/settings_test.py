from .settings import *  # noqa
from .settings import AUTHENTIKATE, BANK_SYNC, DATABASES, DATALAYER
import logging
import os

# The test stack (tests/integration/docker-compose.yaml) publishes postgres on an *ephemeral*
# host port: `tests/conftest.py`'s `django_db_modify_db_settings` overwrites PORT with what
# docker picked, before pytest-django creates the test database. This value is only the
# fallback for running a `tmanage.py` command against a stack you started by hand -- set
# BANK_TEST_DB_PORT to whatever `docker compose port db 5432` reports for it.
DATABASES["default"] = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "testdb",
    "USER": "test",
    "PASSWORD": "test",
    "HOST": "localhost",
    "PORT": os.environ.get("BANK_TEST_DB_PORT", "5432"),
}

# Django forces DEBUG=False under the test runner, and authentikate 3.0 refuses static
# tokens when DEBUG is False. These are deliberate test fixtures, so opt in explicitly.
AUTHENTIKATE = {
    **AUTHENTIKATE,
    "allow_static_tokens_in_production": True,
    "static_tokens": {
        "test": {"sub": "1", "roles": ["editor"]},
        # An admin of the same organization (roles come from the membership, see conftest), for provider setup.
        "admin": {"sub": "3", "roles": ["admin"]},
        # Another member of the same organization, for creator-only checks.
        "colleague": {"sub": "2", "roles": ["editor"]},
        # A user in a different organization, for cross-tenant scoping tests.
        "othertest": {"sub": "9", "org": "other_org", "roles": ["editor"]},
    },
}

# The fake bank and the fake Scalable of the test stack, and a Fernet key generated for the run:
# `conftest.providers_endpoints` fills all three in. Nothing here is a real credential.
ENCRYPTION = {"key_path": "/nonexistent/set-by-conftest.fernet"}
ENABLEBANKING = {"api_url": "http://localhost:0", "timeout_seconds": 10}
SCALABLE = {
    "issuer": "http://localhost:0",
    "audience": "https://de.scalable.capital/api-gateway",
    "client_id": "test-cli-client",
    "graphql_url": "http://localhost:0/api/cli/graphql",
    "user_agent": "bank-tests",
    "timeout_seconds": 10,
}


# Disable logging during tests to reduce noise
logging.disable(logging.CRITICAL)

# Use in-memory channel layer for tests instead of Redis
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# S3 is the test stack's RustFS; `conftest.datalayer` fills in its port, creates the bucket and a
# non-root user the grants assume a role as. Nothing here is a real credential.
DATALAYER = {
    **DATALAYER,
    "access_key": "set-by-conftest",
    "secret_key": "set-by-conftest",
    "host": "localhost",
    "port": 0,
    "protocol": "http",
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::000000000000:role/datalayer",
    "bigfile": {"bucket": "bank-imports"},
    "upload_roles": ["admin", "editor", "bot"],
}
