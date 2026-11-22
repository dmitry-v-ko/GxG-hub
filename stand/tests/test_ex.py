"""EX-01 (conformance matrix, stage C) through ContextForge on the dev stand: AC-M8-13, EFF-02.

`execute` is never registered in ContextForge, so no agent sees it on any path, and a call by name is refused. The
`unified_pdp` rule `deny:agents-never-execute` is the second layer: it is proven on a probe tool registered for the
duration of one test (the rules even allow it to t-alpha:reader by mistake) and deleted afterwards.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from standlib import AGENTS, ALPHA_READER, Stand, denied

EXECUTE = "demo.message.execute"
INTERNAL = re.compile(r"execute|reconcile")


def names(stand: Stand, tenant: str) -> list[str]:
    """Every name an agent might try: upstream, ContextForge's gateway-prefixed and slug forms."""
    slug = EXECUTE.replace(".", "-")
    return [EXECUTE, f"demo-{tenant}-{slug}", slug, "demo.message.reconcile", f"demo-{tenant}-demo-message-reconcile"]


def test_ex01_no_agent_lists_execute_on_any_path(stand: Stand) -> None:
    for agent in AGENTS:
        for name, path in stand.paths(agent).items():
            if name == "mcp":
                continue  # global /mcp refuses team-scoped agent tokens (test_sc01_global_mcp_refuses_agents)
            listed = [t["name"] for t in stand.tools(agent, path)]
            assert listed and not [n for n in listed if INTERNAL.search(n)], (agent, path, listed)


def test_ex01_calls_by_name_are_refused_on_every_path(stand: Stand) -> None:
    before = len(stand.audit())
    for agent in AGENTS:
        tenant = stand.state["agents"][agent]["team"]
        for path in stand.paths(agent).values():
            for name in names(stand, tenant):
                status, resp = stand.rpc(
                    path,
                    "tools/call",
                    {"name": name, "arguments": {"recipient": "partner_1", "text": "x"}},
                    token=stand.token(agent),
                )
                assert status in (200, 401, 403, 404) and (denied(resp) or resp.get("_raw")), (agent, path, name)
    assert stand.audit()[before:] == []  # nothing reached the connector


@pytest.fixture
def probe(stand: Stand) -> Iterator[dict[str, Any]]:
    """A REST tool named demo.message.execute in team t-alpha, only for this test (EFF-02 forbids it otherwise)."""
    admin = stand.admin()
    team = stand.state["teams"]["t-alpha"]["id"]
    body = {
        "tool": {
            "name": EXECUTE,
            "url": "http://demo:8080/healthz",
            "integration_type": "REST",
            "request_type": "GET",
            "description": "EX-01 probe: unified_pdp must refuse it to every agent",
            "visibility": "team",
        },
        "team_id": team,
        "visibility": "team",
    }
    r = admin.post("/v1/tools", json=body)
    assert r.status_code in (200, 201), r.text
    tool = r.json()
    try:
        yield tool
    finally:
        assert admin.delete(f"/v1/tools/{tool['id']}").status_code in (200, 204)


def test_ex01_unified_pdp_denies_a_registered_execute_tool(stand: Stand, probe: dict[str, Any]) -> None:
    """The rules allow `demo-message-execute` to t-alpha:reader by mistake; `deny:agents-never-execute` wins."""
    resp = stand.call(ALPHA_READER, probe["name"])
    assert "PDP_DENY" in denied(resp) and "EFF-02" in denied(resp), resp
    assert denied(stand.call(ALPHA_READER, probe["name"], path="/sse"))
    # not on the agent's virtual server either (the server's tool set is fixed at bootstrap)
    vs = stand.paths(ALPHA_READER)["virtual_server"]
    assert EXECUTE not in {t["name"] for t in stand.tools(ALPHA_READER, vs)}


def test_ex01_probe_is_gone_after_the_test(stand: Stand) -> None:
    listed = [t["name"] for t in stand.tools(ALPHA_READER, "/rpc")]
    assert not [n for n in listed if INTERNAL.search(n)]
