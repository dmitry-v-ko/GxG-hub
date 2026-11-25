"""ContextForge `tool_pre_invoke` plugin: adds `X-GxG-Identity` (ADR-0001 variant C). Logic: core.py.

Config (plugins.yaml `config:`): private_key_file, kid, tenants {team_id: {tenant, agent_domain}},
connectors {gateway_id: aud}. A missing key or bad config fails the plugin's start; with
PLUGINS_FAIL_ON_PLUGIN_ERROR=true ContextForge then does not start (fail closed).
shortcut: key from a file and maps from a generated YAML; production takes the key from a secret store
(rotation by kid) and the maps from the platform registry.
"""

from __future__ import annotations

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

from .core import Config, Denied, claims_for, sign, with_identity, without_identity


class GxgIdentityPlugin(Plugin):
    def __init__(self, config: PluginConfig) -> None:
        super().__init__(config)
        raw = config.config or {}
        with open(raw["private_key_file"], "rb") as f:
            self._key = load_pem_private_key(f.read(), password=None)
        self._cfg = Config.parse(raw)

    async def tool_pre_invoke(self, payload: ToolPreInvokePayload, context: PluginContext) -> ToolPreInvokeResult:
        gc = context.global_context
        uc = gc.user_context
        tool = gc.metadata.get("tool")  # the tool record ContextForge resolved, not the request
        try:
            claims = claims_for(
                self._cfg,
                email=(uc.email or uc.user_id) if uc else None,
                teams=uc.teams if uc else None,
                tool_team=getattr(tool, "team_id", None),
                # ContextForge calls upstream with original_name whatever the alias
                tool_name=getattr(tool, "original_name", None),
                context_tenant=gc.tenant_id,
                gateway_id=gc.server_id,
                arguments=payload.args,
                request_id=gc.request_id,
            )
        except Denied as exc:
            return ToolPreInvokeResult(
                continue_processing=False,
                violation=PluginViolation(
                    reason="identity_unavailable", description=str(exc), code="GXG_IDENTITY_DENIED", details={}
                ),
            )
        if claims is None:  # external gateway: the caller was checked, no identity leaves ContextForge
            headers = without_identity(payload.headers.model_dump() if payload.headers else {})
            return ToolPreInvokeResult(
                modified_payload=payload.model_copy(update={"headers": HttpHeaderPayload(root=headers)}),
                metadata={"gxg_identity": "external"},
            )
        token = sign(claims, self._key, self._cfg.kid)
        # shortcut: payload.headers is deprecated in cpex 0.1.4, v1.1.1 applies only it upstream (ADR-0001)
        headers = with_identity(payload.headers.model_dump() if payload.headers else {}, token)
        new = payload.model_copy(update={"headers": HttpHeaderPayload(root=headers)})
        return ToolPreInvokeResult(modified_payload=new, metadata={"gxg_identity": claims["sub"]})
