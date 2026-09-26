"""Encryption at rest for Scalable credentials.

A Scalable connection holds a refresh token and the DPoP private key it is bound to — together
they are full read access to a brokerage account, for as long as the refresh token lives. Both
are stored Fernet-encrypted with a key read from a mounted file (``scalable.secret_key_path``),
so a database dump alone is not enough.
"""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet

from finance.scalable.client import ScalableConfig


@lru_cache(maxsize=4)
def _fernet(path: str) -> Fernet:
    return Fernet(Path(path).read_bytes().strip())


def _current() -> Fernet:
    return _fernet(ScalableConfig.from_settings().secret_key_path)


def encrypt(value: str) -> str:
    return _current().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _current().decrypt(value.encode()).decode()
