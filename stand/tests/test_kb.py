"""M6 (AC-M6-02; SC-05, SC-10, SC-01, SC-06 on the KB) through ContextForge on the dev stand.

The KB behind the wrapper is the synthetic fake-KB (connectors/kb/fixtures/kb.json) until the gap audit
(docs/audits/kb-gap-audit.md). `kb_calls` in the wrapper's audit line counts KB calls of that tools/call.
"""

from __future__ import annotations

from typing import Any

from standlib import (
    ALPHA_NOBODY,
    ALPHA_READER,
    BETA_READER,
    NET_CONNECTORS,
    NET_EDGE,
    Stand,
    denied,
    gxg_error,
    mcp_body,
    ok,
)

SEARCH, GET, CONTEXT = "kb.documents.search", "kb.documents.get", "kb.context.get"
ALPHA_SALES = {"d-1f6c0a", "d-82b4e1", "d-3c9d27"}
HR, PUBLIC, BETA_SALES, MISSING = "d-a07e55", "d-e4419b", "d-5b2f90", "d-000000"
NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}


def kb_paths(stand: Stand, agent: str) -> dict[str, str]:
    """Where the agent's team tools answer: /rpc, /sse, the team's KB virtual server (MCP, SSE). Global /mcp
    refuses team-scoped agent tokens on v1.1.1 (M5, test_sc01_global_mcp_refuses_agents)."""
    server = f"/servers/{stand.team(agent)['kb_server_id']}"
    return {"rpc": "/rpc", "sse": "/sse", "vs": f"{server}/mcp/", "vs_sse": f"{server}/sse"}


def call(stand: Stand, agent: str, tool: str, args: dict[str, Any], path: str = "/rpc") -> dict[str, Any]:
    return stand.call(agent, stand.tool(stand.state["agents"][agent]["team"], tool), args, path=path)


def reached(stand: Stand, before: int) -> list[dict[str, Any]]:
    return stand.audit("kb")[before:]


def test_ac_m6_02_search_and_context_return_only_the_granted_collection(stand: Stand) -> None:
    # "contract" matches t-alpha sales, hr (expired grant), public (no grant) and t-beta's own sales.
    for path in kb_paths(stand, ALPHA_READER).values():
        got = ok(call(stand, ALPHA_READER, SEARCH, {"query": "contract"}, path))
        assert {i["id"] for i in got["items"]} == {"d-1f6c0a", "d-82b4e1"}, path
        assert got["source_refs"] == ["kb:kb-main/collection/sales"]
        everything = ok(call(stand, ALPHA_READER, SEARCH, {"query": "e", "limit": 50}, path))
        assert {i["id"] for i in everything["items"]} == ALPHA_SALES  # no count of hidden matches (SC-10)
        ctx = ok(call(stand, ALPHA_READER, CONTEXT, {"query": "contract"}, path))
        assert {d["id"] for d in ctx["documents"]} == {"d-1f6c0a", "d-82b4e1"}
        assert "Salary" not in ctx["context"] and "Beta" not in ctx["context"] and "Public" not in ctx["context"]


def test_ac_m6_02_document_of_another_collection_by_id_is_not_found(stand: Stand) -> None:
    before = len(stand.audit("kb"))
    for path in kb_paths(stand, ALPHA_READER).values():
        assert ok(call(stand, ALPHA_READER, GET, {"document_id": "d-1f6c0a"}, path))["collection"] == "sales"
        replies = [call(stand, ALPHA_READER, GET, {"document_id": d}, path)["result"] for d in (HR, PUBLIC, MISSING)]
        assert all(r == replies[-1] for r in replies), (path, replies)  # SC-05: as for a missing document
        assert gxg_error({"result": replies[-1]}) == NOT_FOUND
    lines = reached(stand, before)
    assert {a["outcome"] for a in lines} == {"ok", "not_found"} and all(a["tenant"] == "t-alpha" for a in lines)


def test_sc05_document_of_another_tenant_is_not_found(stand: Stand) -> None:
    for agent, foreign in ((ALPHA_READER, BETA_SALES), (BETA_READER, "d-1f6c0a")):
        own_missing = call(stand, agent, GET, {"document_id": MISSING})["result"]
        assert call(stand, agent, GET, {"document_id": foreign})["result"] == own_missing
    beta = ok(call(stand, BETA_READER, SEARCH, {"query": "contract"}))
    assert [i["id"] for i in beta["items"]] == [BETA_SALES]  # its own `sales`, never t-alpha's


def test_sc10_named_collection_without_grant_is_not_found_before_the_kb(stand: Stand) -> None:
    before = len(stand.audit("kb"))
    for collections in (["hr"], ["public"], ["sales", "hr"]):  # expired grant, no grant, mixed
        for tool in (SEARCH, CONTEXT):
            resp = call(stand, ALPHA_READER, tool, {"query": "contract", "collections": collections})
            assert gxg_error(resp) == NOT_FOUND, (tool, collections, resp)
    lines = reached(stand, before)
    assert len(lines) == 6 and all(a["outcome"] == "not_found" and a["kb_calls"] == 0 for a in lines), lines


def test_sc01_kb_tools_of_another_team_and_without_policy_are_refused(stand: Stand) -> None:
    before = len(stand.audit("kb"))
    for agent, other in ((ALPHA_READER, "t-beta"), (BETA_READER, "t-alpha")):
        for tool in (SEARCH, GET, CONTEXT):
            for path in (*kb_paths(stand, agent).values(), "/mcp/"):
                status, resp = stand.rpc(
                    path,
                    "tools/call",
                    {"name": stand.tool(other, tool), "arguments": {"query": "contract", "document_id": BETA_SALES}},
                    token=stand.token(agent),
                )
                assert status in (200, 401, 403, 404) and (denied(resp) or resp.get("_raw")), (agent, tool, path)
    for tool, args in ((SEARCH, {"query": "contract"}), (GET, {"document_id": "d-1f6c0a"})):
        assert "PDP_DENY" in denied(call(stand, ALPHA_NOBODY, tool, args))  # no unified_pdp allow for nobody
    assert reached(stand, before) == []


def test_sc06_kb_is_not_reachable_from_the_agent_host_and_needs_identity_inside(stand: Stand) -> None:
    body = mcp_body("tools/call", {"name": SEARCH, "arguments": {"query": "contract"}})
    targets = [{"url": u, "body": body, "timeout": 3} for u in ("http://kb:8080/mcp", "http://10.250.0.12:8080/mcp")]
    assert all("error" in r for r in stand.probe(NET_EDGE, targets))
    assert all("error" in r for r in stand.probe("host", targets[1:]))
    before = len(stand.audit("kb"))
    for headers in ({}, {"X-Forwarded-User-Email": "reader@t-alpha.agents.gxg.example.com"}):
        reqs = [{"url": "http://kb:8080/mcp", "headers": stand.headers(None, headers), "body": body}]
        [r] = stand.probe(NET_CONNECTORS, reqs)
        assert r["status"] == 200 and '"unauthenticated"' in r["body"], r
    lines = reached(stand, before)
    assert len(lines) == 2 and all(a["outcome"] == "unauthenticated" and a["kb_calls"] == 0 for a in lines)
