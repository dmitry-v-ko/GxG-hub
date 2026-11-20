"""GxG identity adapter (ADR-0001 option C prototype) - a ContextForge tool_pre_invoke plugin.

For every MCP tool call it adds `X-GXG-Identity: <compact JWS, EdDSA/Ed25519>` to the request
ContextForge sends upstream. Claims: iss, aud (= upstream gateway URL), sub (= authenticated
ContextForge user), tenant_id (= team that owns the gateway, must be the caller's token team),
iat, exp (iat + ttl), jti, ahs (sha256 of canonical JSON arguments), tool.

Fail closed: no user, no team match or no gateway -> PluginViolation, the call is not sent.
Any caller-supplied X-GXG-Identity is dropped before signing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

import jwt
from cpex.framework import HttpHeaderPayload, Plugin, PluginConfig, PluginContext, PluginViolation
from cpex.framework.constants import GATEWAY_METADATA
from cpex.framework.hooks.tools import ToolPreInvokePayload, ToolPreInvokeResult
from cryptography.hazmat.primitives.serialization import load_pem_private_key

HEADER = "X-GXG-Identity"
log = logging.getLogger("gxg_identity_adapter")


def args_hash(args: dict | None) -> str:
    """sha256 over canonical JSON (sorted keys, no spaces, UTF-8) of tool arguments."""
    raw = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _deny(code: str, why: str) -> ToolPreInvokeResult:
    return ToolPreInvokeResult(
        continue_processing=False,
        violation=PluginViolation(reason="identity adapter", description=why, code=code, details={}),
    )


class GxgIdentityAdapter(Plugin):
    def __init__(self, config: PluginConfig) -> None:
        super().__init__(config)
        cfg = config.config or {}
        with open(cfg["private_key_path"], "rb") as f:
            self._key = load_pem_private_key(f.read(), password=None)
        self._kid = cfg.get("kid", "gxg-adapter-1")
        self._iss = cfg.get("issuer", "gxg-identity-adapter")
        self._ttl = int(cfg.get("ttl_seconds", 60))

    async def tool_pre_invoke(self, payload: ToolPreInvokePayload, context: PluginContext) -> ToolPreInvokeResult:
        gc = context.global_context
        uc = gc.user_context
        gw = gc.metadata.get(GATEWAY_METADATA)
        if uc is None or not uc.user_id:
            return _deny("GXG_NO_USER", "no authenticated user in context")
        if gw is None or not getattr(gw, "url", None) or not getattr(gw, "team_id", None):
            return _deny("GXG_NO_GATEWAY", "tool has no team-owned upstream gateway")
        teams = list(uc.teams or [])
        if teams != [gw.team_id]:
            return _deny("GXG_TEAM_MISMATCH", f"caller teams {teams} != gateway team {gw.team_id}")
        now = int(time.time())
        claims = {
            "iss": self._iss,
            "aud": str(gw.url),
            "sub": uc.user_id,
            "tenant_id": gw.team_id,
            "iat": now,
            "exp": now + self._ttl,
            "jti": uuid.uuid4().hex,
            "ahs": args_hash(payload.args),
            "tool": payload.name,
        }
        token = jwt.encode(claims, self._key, algorithm="EdDSA", headers={"kid": self._kid, "typ": "gxg-id+jwt"})
        incoming = payload.headers.model_dump() if payload.headers is not None else {}
        headers = {k: v for k, v in incoming.items() if k.lower() != HEADER.lower()}
        headers[HEADER] = token
        log.info(
            "gxg identity issued sub=%s tenant=%s aud=%s jti=%s", uc.user_id, gw.team_id, str(gw.url), claims["jti"]
        )
        return ToolPreInvokeResult(
            modified_payload=payload.model_copy(update={"headers": HttpHeaderPayload(root=headers)})
        )
