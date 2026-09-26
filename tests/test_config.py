"""Validate the bank service's config.yaml against its bespoke schema.

Standalone — needs no database; run with ``uv run pytest tests/test_config.py``.
"""

from bank_server.configuration import Settings


def test_config_yaml_validates():
    """The service's own config.yaml parses into the typed schema."""
    s = Settings()
    assert s.postgres.db_name
    assert s.redis.host


def test_env_override(monkeypatch):
    """Env vars override the YAML file (nested via ``__``)."""
    monkeypatch.setenv("POSTGRES__PASSWORD", "from-env-test")
    assert Settings().postgres.password == "from-env-test"
