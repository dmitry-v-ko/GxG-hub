"""Decision and claims of the identity adapter, without the ContextForge plugin framework (unit-tested, mutmut).

Claims (SEC-03): iss, aud (= connector id of the gateway), sub (= agent:<tenant>:<name>), tenant_id, team_id,
tool (= upstream name the connector receives), iat, exp (iat + 60), jti, request_id, ahs (sha256 of the
canonical JSON arguments). Every check failing is a `Denied`: the call never leaves ContextForge.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jwt  # PyJWT, ships in the ContextForge image

HEADER = "X-GxG-Identity"
ISSUER = "gxg-identity-adapter"
TYP = "gxg-identity+jwt"  # gxg_connector_kit.cf_identity.TYP
TTL_S = 60


class Denied(Exception):
    """The call has no verifiable identity; the reason goes to ContextForge's violation, never to the connector."""


@dataclass(frozen=True)
class Tenant:
    tenant: str  # t-alpha (team slug)
    agent_domain: str  # agents are <name>@<agent_domain>


@dataclass(frozen=True)
class Config:
    kid: str
    tenants: Mapping[str, Tenant]  # ContextForge team_id -> tenant
    connectors: Mapping[str, str]  # ContextForge gateway_id -> aud (connector id)
    # PILOT EXCEPTION to MCC-01 (owner decision 2026-11-25): third-party MCP gateways that get NO identity header at all
    external: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> Config:
        return cls(
            kid=str(raw["kid"]),
            tenants={str(k): Tenant(str(v["tenant"]), str(v["agent_domain"])) for k, v in raw["tenants"].items()},
            connectors={str(k): str(v) for k, v in raw["connectors"].items()},
            external=frozenset(str(g) for g in raw.get("external", [])),
        )


def args_hash(arguments: Mapping[str, Any]) -> str:
    """SEC-03 canonical JSON: UTF-8, keys sorted, no spaces, non-ASCII as is; = gxg_connector_kit.cf_identity."""
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def claims_for(
    cfg: Config,
    *,
    email: str | None,
    teams: Sequence[str] | None,
    tool_team: str | None,
    tool_name: str | None,
    context_tenant: str | None,
    gateway_id: str | None,
    arguments: Mapping[str, Any] | None,
    request_id: str,
    now: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """Claims for the caller of this tool, or `Denied`; None for an `external` gateway (all the caller checks below
    still apply, but nothing is signed or sent). Inputs are what ContextForge resolved, never the request."""
    if not email:
        raise Denied("no authenticated user")
    if not tool_name:
        raise Denied("tool record has no upstream name")
    if not tool_team or context_tenant != tool_team:
        raise Denied("tool has no team or context tenant disagrees with the tool record")
    if list(teams or []) != [tool_team]:
        raise Denied("caller token must be scoped to exactly the tool's team")
    tenant = cfg.tenants.get(tool_team)
    if tenant is None:
        raise Denied("team is not a registered tenant")
    local, at, domain = email.partition("@")
    if not at or not local or domain != tenant.agent_domain:
        raise Denied("caller is not an agent of this tenant")
    if gateway_id and gateway_id in cfg.external and gateway_id not in cfg.connectors:
        return None
    aud = cfg.connectors.get(gateway_id) if gateway_id else None
    if aud is None:
        raise Denied("gateway is not a registered connector")
    iat = int(now())
    return {
        "iss": ISSUER,
        "aud": aud,
        "sub": f"agent:{tenant.tenant}:{local}",
        "tenant_id": tenant.tenant,
        "team_id": tool_team,
        "tool": tool_name,
        "iat": iat,
        "exp": iat + TTL_S,
        "jti": uuid.uuid4().hex,
        "request_id": request_id,
        "ahs": args_hash(arguments or {}),
    }


def sign(claims: Mapping[str, Any], key: Any, kid: str) -> str:
    return jwt.encode(dict(claims), key, algorithm="EdDSA", headers={"typ": TYP, "kid": kid})


def without_identity(headers: Mapping[str, str]) -> dict[str, str]:
    """Outgoing headers for an external gateway: any incoming X-GxG-Identity dropped, nothing of ours added."""
    return {k: v for k, v in headers.items() if k.lower() != HEADER.lower()}


def with_identity(headers: Mapping[str, str], token: str) -> dict[str, str]:
    """Outgoing headers: any incoming X-GxG-Identity (any case) dropped, ours added."""
    out = {k: v for k, v in headers.items() if k.lower() != HEADER.lower()}
    out[HEADER] = token
    return out
