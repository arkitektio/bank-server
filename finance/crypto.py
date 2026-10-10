"""Encryption at rest for provider credentials.

A provider's credentials (an Enable Banking application key) and a connection's (Scalable's
refresh token and the DPoP private key it is bound to) are full read access to bank accounts.
They are stored Fernet-encrypted with a key read from a mounted file (``encryption.key_path``),
so a database dump alone is not enough.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings

from finance.providers.errors import NotConfigured


@lru_cache(maxsize=4)
def _fernet(path: str) -> Fernet:
    return Fernet(Path(path).read_bytes().strip())


def _current() -> Fernet:
    conf: dict[str, str] | None = getattr(settings, "ENCRYPTION", None)
    if not conf:
        raise NotConfigured("This server has no encryption key for provider credentials (no `encryption` block in its config).")
    return _fernet(conf["key_path"])


def encrypt(value: str) -> str:
    """``value``, encrypted with the service's key."""
    return _current().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """What :func:`encrypt` was given."""
    return _current().decrypt(value.encode()).decode()
