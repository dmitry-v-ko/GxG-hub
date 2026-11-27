"""Which telegram tools each agent sees in ContextForge, derived from a tenant policy file (M11.3/M11.4, GR-18).

ContextForge v1.1.1 does not look inside `telegram.ext.call`: it knows three ext tools and `participants.collect` by
name, so the virtual server (what `tools/list` shows) and the `unified_pdp` allow rules (what may be invoked) are
computed here from the SAME tenant file the bundle carries, so the two cannot drift. Which registry *entry* a caller
may run or even see is decided by the connector (GR-22/GR-23/GR-25): `ext.search` lists an entry only if it is enabled
and the caller's grant covers its group; the `pii` entries are never visible through an `ext:read` grant.

    python gateway/contextforge/ext_roles.py policies/tenants/example-ext-test.yaml > servers.json

NOT verified on a live ContextForge (no stand without secrets): the gateway tool names below follow the stand's rule
`<gateway>-<tool with dots as dashes>` (stand/unified_pdp.rules.json); the real names appear after registration.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

# message.get: read, one chat, live (plan M11.1 «Объём v1», deploy/railway/manifests/telegram/gxg-manifest.yaml)
CORE = ("telegram.messages.search", "telegram.thread.read", "telegram.message.get", "telegram.messages.collect")
PARTICIPANTS = "telegram.participants.collect"
EXT = ("telegram.ext.search", "telegram.ext.describe", "telegram.ext.call")
EXECUTE_FRAGMENTS = ("execute", "reconcile", "propose")  # agents never get these through ContextForge (EFF-02)


def agent_email(principal: str) -> str:
    _, tenant, name = principal.split(":", 2)
    return f"{name}@{tenant}.agents.gxg.example.com"


def _on_telegram(rid: str, connections: set[str]) -> bool:
    domain, _, rest = rid.partition(":")
    return domain == "telegram" and rest.split("/")[0] in connections


def agent_tools(doc: dict[str, Any], principal: str) -> list[str]:
    """Tools of the telegram domain visible to `principal`, from the actions of its live grants and groups."""
    groups = {f"group:{g}": m for g, m in doc.get("groups", {}).items()}
    tg = {c["id"] for c in doc["connections"] if c["domain"] == "telegram"}
    actions: set[str] = set()
    for g in doc["grants"]:
        subjects = {m for s in g["subjects"] for m in (groups.get(s, [s]))}
        if principal in subjects and any(_on_telegram(r, tg) for r in g["resources"]):
            actions |= set(g["actions"])
    tools: list[str] = []
    if actions & {"read", "collect"}:
        tools += [t for t in CORE if t != "telegram.messages.collect" or "collect" in actions]
    if "collect" in actions:
        tools.append(PARTICIPANTS)
    if any(a.startswith("ext:") for a in actions):
        tools += EXT
    return tools


def server_name(tenant: str, tools: list[str]) -> str | None:
    if not tools:
        return None
    suffix = ("-ext" if EXT[0] in tools else "") + ("-people" if PARTICIPANTS in tools else "")
    return f"vs-tg-{tenant}{suffix}"


def cf_tool(tenant: str, tool: str) -> str:
    return f"telegram-{tenant}-" + tool.replace(".", "-")


def build(doc: dict[str, Any]) -> dict[str, Any]:
    """{servers: {name: tools}, agents: {principal: server}, pdp_rules: [...]} for one tenant file."""
    tenant = doc["tenant"]
    servers: dict[str, list[str]] = {}
    agents: dict[str, str] = {}
    rules: list[dict[str, Any]] = []
    for a in doc["principals"].get("agents", []):
        tools = agent_tools(doc, a["id"])
        name = server_name(tenant, tools)
        if name is None:
            continue
        servers[name] = sorted(set(servers.get(name, [])) | set(tools))
        agents[a["id"]] = name
        rules.append(
            {
                "id": f"{tenant}.{a['id'].rsplit(':', 1)[1]}.telegram",
                "roles": ["*"],
                "conditions": {"subject.email": agent_email(a["id"])},
                "actions": [f"tools.invoke.{cf_tool(tenant, t)}" for t in tools],
                "resource_types": ["tool"],
                "resource_ids": ["*"],
                "reason": f"derived from policies/tenants/{tenant} (GR-18); the entry is checked by the connector",
            }
        )
    return {"tenant": tenant, "servers": dict(sorted(servers.items())), "agents": agents, "pdp_rules": rules}


def main(argv: list[str]) -> int:
    json.dump(build(yaml.safe_load(Path(argv[0]).read_text("utf-8"))), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
