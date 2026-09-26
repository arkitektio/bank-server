"""DPoP (RFC 9449) proofs for Scalable's OAuth and GraphQL endpoints.

A port of the official CLI's software signer (``scalable-cli/src/dpop``): an ES256 (P-256) key
whose public JWK rides in every proof's header. Scalable binds the access *and* refresh tokens to
the key's thumbprint, so the key is part of the connection's credentials and is stored with them.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def canonical_htu(url: str) -> str:
    """The proof's ``htu``: the target URL without query and fragment."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def access_token_hash(token: str) -> str:
    """The proof's ``ath`` claim, binding it to the access token it accompanies."""
    return _b64(hashlib.sha256(token.encode()).digest())


class DpopKey:
    """One P-256 signing key."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._key = private_key
        numbers = private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64(numbers.x.to_bytes(32, "big")),
            "y": _b64(numbers.y.to_bytes(32, "big")),
        }

    @classmethod
    def generate(cls) -> "DpopKey":
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> "DpopKey":
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("DPoP key is not an EC key")
        return cls(key)

    @classmethod
    def from_private_scalar(cls, scalar: bytes) -> "DpopKey":
        return cls(ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1()))

    def to_pem(self) -> str:
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    @property
    def thumbprint(self) -> str:
        """RFC 7638 thumbprint (canonical member order for EC keys)."""
        jwk = self.jwk
        canonical = f'{{"crv":"{jwk["crv"]}","kty":"{jwk["kty"]}","x":"{jwk["x"]}","y":"{jwk["y"]}"}}'
        return _b64(hashlib.sha256(canonical.encode()).digest())

    def proof(
        self,
        method: str,
        url: str,
        *,
        nonce: str | None = None,
        access_token: str | None = None,
        iat: int | None = None,
        jti: str | None = None,
    ) -> str:
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}
        claims: dict[str, object] = {
            "htm": method.upper(),
            "htu": canonical_htu(url),
            "iat": int(time.time()) if iat is None else iat,
            "jti": jti or _b64(secrets.token_bytes(16)),
        }
        if nonce:
            claims["nonce"] = nonce
        if access_token:
            claims["ath"] = access_token_hash(access_token)
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(claims, separators=(",", ":")).encode())
        )
        r, s = decode_dss_signature(self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        return signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
