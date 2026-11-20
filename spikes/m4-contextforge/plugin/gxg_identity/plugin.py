"""ADR-0001 variant C prototype: ContextForge tool_pre_invoke plugin that adds a JWS identity assertion.

X-GxG-Identity: compact JWS, alg EdDSA (Ed25519), signed with a key only this adapter holds.
Claims: iss, aud (= connector id of the gateway), sub (= agent:<tenant>:<name>), tenant_id, team_id,
tool (= upstream tool name the connector receives), iat, exp (iat+60), jti, ahs (sha256 of canonical JSON arguments).
Fail closed: no authenticated user, caller not in exactly the tool's team, unknown team or gateway,
or an email outside the tenant's agent domain -> PluginViolation, the call never leaves ContextForge.
shortcut: key read from a file and config in YAML; production needs a secret store and key rotation (kid).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

from cpex.framework import (
    HttpHeaderPayload,
    Plugin,
    PluginConfig,
    PluginContext,
    PluginViolation,
    ToolPreInvokePayload,
    ToolPreInvokeResult,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key

HEADER = "X-GxG-Identity"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class GxgIdentityPlugin(Plugin):
    def __init__(self, config: PluginConfig) -> None:
        super().__init__(config)
        cfg = config.config or {}
        with open(cfg["private_key_file"], "rb") as f:
            self._key = load_pem_private_key(f.read(), password=None)
        self._kid = cfg["kid"]
        self._iss = cfg.get("issuer", "gxg-identity-adapter")
        self._ttl = int(cfg.get("ttl_seconds", 60))
        self._tenants = cfg["tenants"]  # team_id -> {"tenant": "t-alpha", "agent_domain": "t-alpha.spike.example.com"}
        self._connectors = cfg["connectors"]  # gateway_id -> aud

    def _deny(self, why: str) -> ToolPreInvokeResult:
        return ToolPreInvokeResult(
            continue_processing=False,
            violation=PluginViolation(
                reason="identity_unavailable", description=why, code="GXG_IDENTITY_DENIED", details={}
            ),
        )

    async def tool_pre_invoke(  # noqa: PLR0911 — one early deny per fail-closed check
        self, payload: ToolPreInvokePayload, context: PluginContext
    ) -> ToolPreInvokeResult:
        gc = context.global_context
        uc = gc.user_context
        if uc is None or not uc.user_id:
            return self._deny("no authenticated user")
        # team and upstream name of the tool, from the tool record ContextForge resolved (not from the request);
        # ContextForge calls upstream with original_name whatever the alias, so that is what we sign
        tool = gc.metadata.get("tool")
        tool_team, tool_name = getattr(tool, "team_id", None), getattr(tool, "original_name", None)
        if not tool_name:
            return self._deny("tool record has no upstream name")
        if not tool_team or gc.tenant_id != tool_team:
            return self._deny("tool has no team or context tenant disagrees with the tool record")
        teams = list(uc.teams or [])
        if len(teams) != 1 or teams[0] != tool_team:
            return self._deny("caller token must be scoped to exactly the tool's team")
        tenant = self._tenants.get(tool_team)
        if tenant is None:
            return self._deny("team is not a registered tenant")
        local, _, domain = (uc.email or uc.user_id).partition("@")
        if domain != tenant["agent_domain"] or not local:
            return self._deny("caller is not an agent of this tenant")
        aud = self._connectors.get(gc.server_id)
        if aud is None:
            return self._deny("gateway is not a registered connector")
        now = int(time.time())
        claims = {
            "iss": self._iss,
            "aud": aud,
            "sub": f"agent:{tenant['tenant']}:{local}",
            "tenant_id": tenant["tenant"],
            "team_id": tool_team,
            "tool": tool_name,
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
            "request_id": gc.request_id,
            "ahs": hashlib.sha256(canonical(payload.args or {})).hexdigest(),
        }
        head = {"alg": "EdDSA", "typ": "JWT", "kid": self._kid}
        signing_input = b64(canonical(head)) + "." + b64(canonical(claims))
        token = signing_input + "." + b64(self._key.sign(signing_input.encode()))
        headers = {
            k: v
            for k, v in (payload.headers.model_dump() if payload.headers else {}).items()
            if k.lower() != HEADER.lower()
        }
        headers[HEADER] = token
        new = payload.model_copy(update={"headers": HttpHeaderPayload(root=headers)})
        return ToolPreInvokeResult(modified_payload=new, metadata={"gxg_identity": claims["sub"]})
