"""Service JWT between internal services (AS-06, AS-25).

`Authorization: Bearer <jwt>`, `alg: EdDSA`, `typ: gxg-svc+jwt`, claims `iss` (service ID),
`aud` (receiver ID), `iat`, `exp ≤ iat + 60 s`, `jti`. The verification key is the
issuer's `public_key` in the platform registry (part of the signed bundle).
`kid` = RFC 7638 thumbprint of that key (interpretation recorded in
reports/plans/M1-cycle-1.md: the spec has one key per service and no `kid`).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import _jwt
from ._jwt import Clock, JtiCache, TokenError

TYP = "gxg-svc+jwt"
REQUIRED_CLAIMS = ("iss", "aud", "iat", "exp", "jti")


@dataclass(frozen=True)
class Service:
    id: str
    kind: str
    public_key: Ed25519PublicKey
    tenants: frozenset[str]
    internal_url: str | None = None

    def serves(self, tenant: str) -> bool:
        """AS-25: the service is registered for this tenant (`*` = all, never allowed for connectors by schema)."""
        return tenant in self.tenants or "*" in self.tenants


def parse_registry(doc: Mapping[str, Any]) -> dict[str, Service]:
    """Platform registry (schemas/platform-registry.schema.json) → services by ID."""
    if doc.get("schema_version") != 1:
        raise ValueError("platform registry: schema_version must be 1")
    services: dict[str, Service] = {}
    for s in doc["services"]:
        if s["id"] in services:
            raise ValueError(f"platform registry: duplicate service {s['id']!r}")
        services[s["id"]] = Service(
            id=s["id"],
            kind=s["kind"],
            public_key=_jwt.parse_public_key(s["public_key"]),
            tenants=frozenset(s["tenants"]),
            internal_url=s.get("internal_url"),
        )
    return services


def sign(private_key: Ed25519PrivateKey, *, iss: str, aud: str, ttl_s: int = 60, now: Clock = time.time) -> str:
    """Service JWT for one call from `iss` to `aud`."""
    if not 0 < ttl_s <= _jwt.MAX_TTL_S:
        raise ValueError("ttl_s must be in (0, 60]")
    iat = int(now())
    claims = {"iss": iss, "aud": aud, "iat": iat, "exp": iat + ttl_s, "jti": str(uuid.uuid4())}
    return _jwt.sign(private_key, claims, typ=TYP, kid=_jwt.thumbprint(private_key.public_key()))


class SvcJwtVerifier:
    """Verifies service JWTs addressed to `service_id` against the current platform registry."""

    def __init__(
        self, service_id: str, registry: Callable[[], Mapping[str, Service]], *, now: Clock = time.time
    ) -> None:
        self._service_id = service_id
        self._registry = registry
        self._now = now
        self._jti = JtiCache()

    def verify(self, token: str, *, allowed_issuers: Collection[str]) -> Service:
        """Return the calling service. `allowed_issuers` is this endpoint's allowlist (AS-06)."""
        services = self._registry()

        def key_for(kid: str, claims: Mapping[str, Any]) -> Ed25519PublicKey:
            iss = claims.get("iss")
            service = services.get(iss) if isinstance(iss, str) else None
            if service is None:
                raise TokenError("iss_unregistered")
            if kid != _jwt.thumbprint(service.public_key):
                raise TokenError("kid")
            return service.public_key

        claims = _jwt.verify(
            token,
            typ=TYP,
            key_for=key_for,
            issuers=allowed_issuers,
            audience=self._service_id,
            required=REQUIRED_CLAIMS,
            jti_cache=self._jti,
            now=self._now,
        )
        return services[claims["iss"]]
