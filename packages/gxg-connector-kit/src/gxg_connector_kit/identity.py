"""Caller identity in the connector (ADR-0001, SEC-03, MCC-02).

`IdentityVerifier` is the interface every connector depends on. The production
implementation is `cf_identity.ContextForgeVerifier` (ADR-0001 variant C); the stub
issuer/verifier below is for tests without ContextForge and refuses to start outside `GXG_PROFILE=test`.
Without a verifier a connector has no identity and must refuse calls (fail closed).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import _jwt
from ._jwt import Clock, JtiCache, TokenError

PROFILE_ENV = "GXG_PROFILE"
STUB_HEADER = "x-gxg-test-identity"
STUB_ISSUER = "gxg-test-issuer"
STUB_TYP = "gxg-test-identity+jwt"
REQUIRED_CLAIMS = ("iss", "aud", "sub", "tenant_id", "iat", "exp", "jti")  # SEC-03 minimum


@dataclass(frozen=True)
class VerifiedIdentity:
    tenant_id: str  # = ContextForge team
    principal: str  # agent:<tenant>:<name>, immutable
    issued_at: datetime
    expires_at: datetime
    request_id: str
    assertion: str | None  # the verifiable assertion, if any (AS-08)
    mode: Literal["signed_assertion", "per_tenant_instance"]


class IdentityVerifier(Protocol):
    def verify(
        self, headers: Mapping[str, str], *, tool: str | None = None, arguments: Mapping[str, Any] | None = None
    ) -> VerifiedIdentity:
        """Identity from the trusted channel only, or raise `GxgError("unauthenticated")`.

        `tool` and `arguments` are the call the assertion must be bound to (SEC-03: `tool`, `ahs`);
        a verifier that binds refuses without them. They never carry identity, `_meta` is never passed (MCC-02).
        """
        ...


def _require_test_profile() -> None:
    if os.environ.get(PROFILE_ENV) != "test":
        raise RuntimeError(f"the stub identity issuer works only with {PROFILE_ENV}=test")


class StubIssuer:
    """Test issuer: signs identity assertions with a key generated in memory. Test profile only."""

    def __init__(self) -> None:
        _require_test_profile()
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def headers(
        self, *, tenant_id: str, principal: str, audience: str, ttl_s: int = 60, now: Clock = time.time
    ) -> dict[str, str]:
        iat = int(now())
        claims: dict[str, Any] = {
            "iss": STUB_ISSUER,
            "aud": audience,
            "sub": principal,
            "tenant_id": tenant_id,
            "iat": iat,
            "exp": iat + ttl_s,
            "jti": str(uuid.uuid4()),
        }
        token = _jwt.sign(self._key, claims, typ=STUB_TYP, kid=_jwt.thumbprint(self.public_key))
        return {STUB_HEADER: token}


class StubVerifier:
    """`IdentityVerifier` for `StubIssuer` assertions addressed to `connector_id`. Test profile only."""

    def __init__(self, connector_id: str, issuer_key: Ed25519PublicKey, *, now: Clock = time.time) -> None:
        _require_test_profile()
        self._connector_id = connector_id
        self._key = issuer_key
        self._kid = _jwt.thumbprint(issuer_key)
        self._now = now
        self._jti = JtiCache()

    def verify(
        self, headers: Mapping[str, str], *, tool: str | None = None, arguments: Mapping[str, Any] | None = None
    ) -> VerifiedIdentity:
        """The test issuer's assertions are not bound to a call: `tool` and `arguments` are ignored."""
        token = {k.lower(): v for k, v in headers.items()}.get(STUB_HEADER)
        if token is None:
            raise TokenError("missing")

        def key_for(kid: str, _claims: Mapping[str, Any]) -> Ed25519PublicKey:
            if kid != self._kid:
                raise TokenError("kid")
            return self._key

        claims = _jwt.verify(
            token,
            typ=STUB_TYP,
            key_for=key_for,
            issuers=(STUB_ISSUER,),
            audience=self._connector_id,
            required=REQUIRED_CLAIMS,
            jti_cache=self._jti,
            now=self._now,
        )
        if not claims["sub"].startswith(f"agent:{claims['tenant_id']}:"):
            raise TokenError("principal")
        return VerifiedIdentity(
            tenant_id=claims["tenant_id"],
            principal=claims["sub"],
            issued_at=datetime.fromtimestamp(claims["iat"], UTC),
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            request_id=claims["jti"],
            assertion=token,
            mode="signed_assertion",
        )
