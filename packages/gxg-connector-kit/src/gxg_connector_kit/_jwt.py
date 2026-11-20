"""Shared EdDSA JWS verification for execution tokens, service JWTs and test identity.

Signature and `alg` handling come from PyJWT on `cryptography`; this module adds the
claim rules that are ours: exact `typ` and `aud`, issuer allowlist, 5 s clock skew
for `iat`/`exp` (AS-11), `exp - iat ≤ 60 s`, required claims and a local `jti` cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .errors import GxgError

Clock = Callable[[], float]
SKEW_S = 5  # AS-11
MAX_TTL_S = 60  # EFF execution token, AS-06
_MAX_TOKEN_LEN = 8192
_INT_CLAIMS = frozenset({"iat", "exp"})
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


class TokenError(GxgError):
    """Rejected token. `reason` is for tests and internal logs; the caller sees only `unauthenticated`."""

    def __init__(self, reason: str) -> None:
        super().__init__("unauthenticated", "token rejected")
        self.reason = reason


class JtiCache:
    """Seen `jti` values until their `exp` + skew. Thread-safe; expired entries are pruned on insert."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, key: str, until: float, now: float) -> bool:
        """Remember `key`; False if it was already seen and is not yet expired."""
        with self._lock:
            for k in [k for k, t in self._seen.items() if t < now]:
                del self._seen[k]
            if key in self._seen:
                return False
            self._seen[key] = until
            return True


def b64url_decode(data: str) -> bytes:
    """base64url without padding; any other character is an error (the stdlib would silently drop it)."""
    if not isinstance(data, str) or not _B64URL.fullmatch(data) or len(data) % 4 == 1:
        raise ValueError("not base64url without padding")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def raw_public_key(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def parse_public_key(value: str) -> Ed25519PublicKey:
    """`ed25519:<base64url raw 32 bytes>` (platform registry format)."""
    prefix = "ed25519:"
    if not value.startswith(prefix):
        raise ValueError("public key must start with 'ed25519:'")
    raw = b64url_decode(value[len(prefix) :])
    if len(raw) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def format_public_key(key: Ed25519PublicKey) -> str:
    return "ed25519:" + b64url_encode(raw_public_key(key))


def thumbprint(key: Ed25519PublicKey) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of an Ed25519 key."""
    jwk = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": b64url_encode(raw_public_key(key))}, separators=(",", ":"))
    return b64url_encode(hashlib.sha256(jwk.encode()).digest())


def sign(private_key: Ed25519PrivateKey, claims: Mapping[str, Any], *, typ: str, kid: str) -> str:
    return jwt.encode(dict(claims), private_key, algorithm="EdDSA", headers={"typ": typ, "kid": kid})


def verify(
    token: str,
    *,
    typ: str,
    key_for: Callable[[str, Mapping[str, Any]], Ed25519PublicKey],
    issuers: Collection[str],
    audience: str,
    required: Collection[str],
    jti_cache: JtiCache,
    now: Clock = time.time,
) -> dict[str, Any]:
    """Verify a compact EdDSA JWS and its claims; return the claims or raise `TokenError`.

    `key_for(kid, unverified_claims)` returns the verification key or raises `TokenError`.
    """
    if not isinstance(token, str) or len(token) > _MAX_TOKEN_LEN:
        raise TokenError("malformed")
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise TokenError("malformed") from None
    if header.get("alg") != "EdDSA":
        raise TokenError("alg")
    if header.get("typ") != typ:
        raise TokenError("typ")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TokenError("kid")
    key = key_for(kid, unverified)
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
                "verify_jti": False,
            },
        )
    except jwt.PyJWTError:
        raise TokenError("signature") from None
    _check_claims(claims, issuers=issuers, audience=audience, required=required, jti_cache=jti_cache, now=now())
    return claims


def _check_claims(
    claims: Mapping[str, Any],
    *,
    issuers: Collection[str],
    audience: str,
    required: Collection[str],
    jti_cache: JtiCache,
    now: float,
) -> None:
    for name in required:
        value = claims.get(name)
        if name in _INT_CLAIMS:
            if not isinstance(value, int) or isinstance(value, bool):
                raise TokenError(f"claim:{name}")
        elif not isinstance(value, str) or not value:
            raise TokenError(f"claim:{name}")
    if claims["iss"] not in issuers:
        raise TokenError("iss")
    if claims["aud"] != audience:
        raise TokenError("aud")
    iat, exp = claims["iat"], claims["exp"]
    if iat > now + SKEW_S:
        raise TokenError("iat")
    if now > exp + SKEW_S:
        raise TokenError("exp")
    if not 0 < exp - iat <= MAX_TTL_S:
        raise TokenError("ttl")
    if not jti_cache.add(f"{claims['iss']}\x00{claims['jti']}", exp + SKEW_S, now):
        raise TokenError("replay")
