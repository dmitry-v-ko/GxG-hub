"""`IdentityVerifier` for the ContextForge identity adapter (ADR-0001 variant C, SEC-03).

The adapter plugin `gxg_identity` (gateway/contextforge/) adds `X-GxG-Identity`: a compact JWS,
`alg` EdDSA, `typ` `gxg-identity+jwt`, `kid` of the adapter key. `_jwt.verify` checks the signature,
`kid`, `iss`, `aud` = this connector, `iat`/`exp` with 5 s skew, `exp - iat <= 60 s`, required claims
and a one-time `jti`. Here: `tool` = the called tool, `ahs` = hash of the call's arguments,
`sub` inside `tenant_id`. The arguments are only compared with the signed hash; identity never comes
from them (MCC-02).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import _jwt
from ._jwt import Clock, JtiCache, TokenError
from .identity import REQUIRED_CLAIMS, VerifiedIdentity

HEADER = "x-gxg-identity"
ISSUER = "gxg-identity-adapter"
TYP = "gxg-identity+jwt"


def args_hash(arguments: Mapping[str, Any]) -> str:
    """`ahs` (SEC-03): sha256 of UTF-8 JSON, keys sorted, no spaces, non-ASCII as is. Not EFF-05 (JCS)."""
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ContextForgeVerifier:
    def __init__(self, connector_id: str, keys: Mapping[str, Ed25519PublicKey], *, now: Clock = time.time) -> None:
        if not keys:
            raise ValueError("no identity adapter keys")
        self._connector_id = connector_id
        self._keys = dict(keys)
        self._now = now
        # shortcut: jti cache in this process; with a second replica it moves to a shared store (SEC-03).
        self._jti = JtiCache()

    def verify(
        self, headers: Mapping[str, str], *, tool: str | None = None, arguments: Mapping[str, Any] | None = None
    ) -> VerifiedIdentity:
        if tool is None or arguments is None:
            raise TokenError("call binding missing")  # a caller that forgets the binding gets nothing
        token = {k.lower(): v for k, v in headers.items()}.get(HEADER)
        if token is None:
            raise TokenError("missing")

        def key_for(kid: str, _claims: Mapping[str, Any]) -> Ed25519PublicKey:
            key = self._keys.get(kid)
            if key is None:
                raise TokenError("kid")
            return key

        claims = _jwt.verify(
            token,
            typ=TYP,
            key_for=key_for,
            issuers=(ISSUER,),
            audience=self._connector_id,
            required=(*REQUIRED_CLAIMS, "tool", "ahs"),
            jti_cache=self._jti,
            now=self._now,
        )
        if claims["tool"] != tool:
            raise TokenError("tool")
        if claims["ahs"] != args_hash(arguments):
            raise TokenError("ahs")
        if not claims["sub"].startswith(f"agent:{claims['tenant_id']}:"):
            raise TokenError("principal")
        return VerifiedIdentity(
            tenant_id=claims["tenant_id"],
            principal=claims["sub"],
            issued_at=datetime.fromtimestamp(claims["iat"], UTC),
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            request_id=claims.get("request_id") or claims["jti"],
            assertion=token,
            mode="signed_assertion",
        )
