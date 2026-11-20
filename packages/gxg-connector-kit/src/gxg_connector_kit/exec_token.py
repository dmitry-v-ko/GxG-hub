"""Execution token verification in the connector (EFF-18 п.1, AS-09…AS-11).

Token: compact JWS, `alg: EdDSA`, `typ: gxg-exec+jwt`, claims per
schemas/execution-token.schema.json. Keys come only from the executor JWKS
(`GET /.well-known/gxg-exec-jwks.json`), cached for at most 10 minutes. The HTTP
transport is injected as `fetch()`.

Hash, config and grant checks of EFF-18 п.2-3 and `dispatch` (п.4) are the caller's job.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, TypedDict, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import _jwt
from ._jwt import Clock, JtiCache, TokenError
from .readiness import Readiness

TYP = "gxg-exec+jwt"
ISSUER = "gxg-executor"
JWKS_MAX_AGE_S = 600  # AS-09
REQUIRED_CLAIMS = ("iss", "aud", "sub", "jti", "ten", "req", "tool", "con", "res", "ahs", "idk", "iat", "exp")


class ExecutionClaims(TypedDict):
    iss: str
    aud: str
    sub: str  # intent_id
    jti: str  # execution_id
    ten: str
    req: str
    tool: str  # tool_key@tool_version
    con: str
    res: str
    ahs: str  # canonical_args_hash
    idk: str
    iat: int
    exp: int


def parse_jwks(data: bytes) -> dict[str, Ed25519PublicKey]:
    """Ed25519 keys of a JWKS by `kid`. Keys of other types are ignored; a malformed Ed25519 key is an error."""
    doc = json.loads(data)
    keys: dict[str, Ed25519PublicKey] = {}
    for jwk in doc["keys"]:
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            continue
        raw = _jwt.b64url_decode(jwk["x"])
        if len(raw) != 32 or not isinstance(jwk["kid"], str) or not jwk["kid"]:
            raise ValueError("malformed Ed25519 JWK")
        keys[jwk["kid"]] = Ed25519PublicKey.from_public_bytes(raw)
    return keys


class ExecutorJwks:
    """JWKS cache: refetched when older than `max_age_s`. If the refetch fails, no key is trusted (fail closed)."""

    def __init__(
        self, fetch: Callable[[], bytes], *, max_age_s: float = JWKS_MAX_AGE_S, now: Clock = time.time
    ) -> None:
        self._fetch = fetch
        self._max_age_s = max_age_s
        self._now = now
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _current(self) -> dict[str, Ed25519PublicKey]:
        with self._lock:
            t = self._now()
            if self._fetched_at is None or t - self._fetched_at >= self._max_age_s:
                try:
                    self._keys = parse_jwks(self._fetch())
                    self._fetched_at = t
                except Exception:  # transport or format error: drop the stale keys
                    self._keys = {}
                    self._fetched_at = None
            return self._keys

    def key(self, kid: str) -> Ed25519PublicKey:
        found = self._current().get(kid)
        if found is None:
            raise TokenError("kid")
        return found

    def readiness(self) -> Readiness:
        """MCC-09: not ready while no executor key can be loaded. Refetches only when the cache is stale."""
        if not self._current():
            return Readiness(False, ("executor JWKS is unreachable or has no Ed25519 key",))
        return Readiness(True)


class ExecTokenVerifier:
    """Verifies execution tokens addressed to this connector (`aud` = `connector_id`)."""

    def __init__(self, connector_id: str, jwks: ExecutorJwks, *, now: Clock = time.time) -> None:
        self._connector_id = connector_id
        self._jwks = jwks
        self._now = now
        self._jti = JtiCache()

    def verify(self, token: str) -> ExecutionClaims:
        def key_for(kid: str, _claims: Mapping[str, Any]) -> Ed25519PublicKey:
            return self._jwks.key(kid)

        claims = _jwt.verify(
            token,
            typ=TYP,
            key_for=key_for,
            issuers=(ISSUER,),
            audience=self._connector_id,
            required=REQUIRED_CLAIMS,
            jti_cache=self._jti,
            now=self._now,
        )
        return cast(ExecutionClaims, claims)
