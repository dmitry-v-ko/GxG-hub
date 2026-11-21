"""MC-01…MC-04 (conformance-matrix-v1, contract, stage B) through ContextForge on the dev stand. AC-M5-01."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx2
import jsonschema
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from gxg_identity import core
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import LATEST_PROTOCOL_VERSION
from standlib import (
    ALPHA_NOBODY,
    ALPHA_READER,
    BETA_READER,
    CF,
    NET_CONNECTORS,
    REPO,
    Stand,
    denied,
    gxg_error,
    ok,
    sh,
)

from demo_connector.tools import load_schema

MANIFEST = yaml.safe_load((REPO / "connectors" / "demo" / "gxg-manifest.yaml").read_text(encoding="utf-8"))
SEARCH_RESULT = json.loads((REPO / "schemas" / "search-result.schema.json").read_text())
ERROR = json.loads((REPO / "schemas" / "error.schema.json").read_text())


# MC-01 ------------------------------------------------------------------------------------------


async def _sdk_session(stand: Stand, agent: str) -> dict[str, Any]:
    """The official MCP SDK against the agent's virtual server: negotiate, discover, invoke."""
    url = CF + stand.paths(agent)["virtual_server"]
    headers = {"Authorization": f"Bearer {stand.token(agent)}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=60) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        tools = (await session.list_tools()).tools
        boards = next(t for t in tools if t.name == stand.tool("t-alpha", "demo.boards.list"))
        result = await session.call_tool(boards.name, {})
        return {"init": init, "tools": tools, "result": result}


def test_mc01_negotiate_discover_invoke_with_the_official_sdk(stand: Stand) -> None:
    got = asyncio.run(_sdk_session(stand, ALPHA_READER))
    assert got["init"].protocolVersion == LATEST_PROTOCOL_VERSION or got["init"].protocolVersion
    assert got["init"].capabilities.tools is not None
    by_upstream = {t["name"]: t for t in MANIFEST["tools"]}
    listed = {t.name: t for t in got["tools"]}
    assert len(listed) == 3
    for upstream, spec in by_upstream.items():
        tool = listed[stand.tool("t-alpha", upstream)]
        assert tool.inputSchema == load_schema(spec["input_schema"]), upstream
        assert tool.outputSchema == load_schema(spec["output_schema"]), upstream
        assert tool.description == load_schema(spec["input_schema"])["description"]
    result = got["result"]
    assert not result.isError
    jsonschema.validate(result.structuredContent, load_schema(by_upstream["demo.boards.list"]["output_schema"]))
    assert json.loads(result.content[0].text) == result.structuredContent  # MCC-04: duplicated in content


def test_mc01_rpc_lists_the_same_schemas(stand: Stand) -> None:
    for agent, tenant in ((ALPHA_READER, "t-alpha"), (BETA_READER, "t-beta")):
        listed = {t["name"]: t for t in stand.tools(agent)}
        for spec in MANIFEST["tools"]:
            tool = listed[stand.tool(tenant, spec["name"])]
            assert tool["inputSchema"] == load_schema(spec["input_schema"])
            assert tool["outputSchema"] == load_schema(spec["output_schema"])


# MC-02 ------------------------------------------------------------------------------------------


def _search(stand: Stand, args: dict[str, Any], agent: str = ALPHA_READER, path: str = "/rpc") -> dict[str, Any]:
    return stand.call(agent, stand.tool(stand.state["agents"][agent]["team"], "demo.notes.search"), args, path=path)


def test_mc02_search_schema_pagination_and_limits(stand: Stand) -> None:
    out_schema = load_schema(next(t for t in MANIFEST["tools"] if t["name"] == "demo.notes.search")["output_schema"])
    seen: list[str] = []
    cursor = None
    for _ in range(50):
        page = ok(_search(stand, {"query": "status", "limit": 7} | ({"cursor": cursor} if cursor else {})))
        jsonschema.validate(page, SEARCH_RESULT)
        jsonschema.validate(page, out_schema)
        assert len(page["items"]) <= 7 and all(len(i["excerpt"]) <= 500 for i in page["items"])
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    full = ok(_search(stand, {"query": "status", "limit": 50}))
    assert len(seen) == len(set(seen)) > 7 and seen[:50] == [i["id"] for i in full["items"]]
    # the cursor is bound to the caller and the query
    first = ok(_search(stand, {"query": "status", "limit": 7}))
    assert first["next_cursor"]
    other_query = gxg_error(_search(stand, {"query": "open", "limit": 7, "cursor": first["next_cursor"]}))
    assert other_query and other_query["code"] == "invalid_input"
    # size limits: ContextForge validates the arguments against the inputSchema first (-32603, the connector is not
    # reached); the connector enforces the same schema itself with invalid_input (MCC-05) when called directly
    bad_args: list[dict[str, Any]] = [
        {"query": "status", "limit": 51},
        {"query": ""},
        {"query": "x" * 201},
        {"query": "status", "board_ids": [f"b{i}" for i in range(11)]},
    ]
    before = len(stand.audit())
    for bad in bad_args:
        resp = _search(stand, bad)
        assert "Internal error" in denied(resp), (bad, resp)
    assert stand.audit()[before:] == []
    jws = [stand.sign(tool="demo.notes.search", ahs=core.args_hash(bad)) for bad in bad_args]
    replies = stand.direct(
        NET_CONNECTORS,
        [
            ({"X-GxG-Identity": j}, {"name": "demo.notes.search", "arguments": b})
            for j, b in zip(jws, bad_args, strict=True)
        ],
    )
    for bad, resp in zip(bad_args, replies, strict=True):
        err = gxg_error(resp)
        assert err and err["code"] == "invalid_input" and err["retryable"] is False, (bad, resp)
        jsonschema.validate(err, ERROR)


def test_mc02_valid_empty_result_does_not_fall_back(stand: Stand) -> None:
    before = stand.ctl("GET", "/_test/backend/calls")
    empty = ok(_search(stand, {"query": "zzz-nothing-matches"}))
    assert empty == {"items": [], "next_cursor": None, "complete": True, "partial_reasons": [], "source_refs": []}
    after = stand.ctl("GET", "/_test/backend/calls")
    assert after.get("index", 0) == before.get("index", 0) + 1 and after.get("scan", 0) == before.get("scan", 0)


def test_mc02_index_failure_falls_back_to_scan(stand: Stand) -> None:
    plain = ok(_search(stand, {"query": "status", "limit": 10}))
    before = stand.ctl("GET", "/_test/backend/calls")
    stand.ctl("POST", "/_test/index/fail", {"error": "timeout_before_start"})
    try:
        fallback = ok(_search(stand, {"query": "status", "limit": 10}))
    finally:
        stand.ctl("POST", "/_test/index/fail", {"error": None})
    after = stand.ctl("GET", "/_test/backend/calls")
    assert after.get("scan", 0) == before.get("scan", 0) + 1
    assert [i["id"] for i in fallback["items"]] == [i["id"] for i in plain["items"]]
    assert fallback["complete"] is True and fallback["partial_reasons"] == []


# MC-03 ------------------------------------------------------------------------------------------


def test_mc03_backend_errors_keep_their_codes_and_retryable_through_contextforge(stand: Stand) -> None:
    want = {
        "rate_limited": {"code": "rate_limited", "retryable": True, "retry_after_s": 7},
        "temporarily_unavailable": {"code": "temporarily_unavailable", "retryable": True},
        "timeout_before_start": {"code": "timeout_before_start", "retryable": True},
    }
    note = {"note_id": "00000000-0000-4000-8000-000000000000"}
    try:
        for mode, expected in want.items():
            stand.ctl("POST", "/_test/backend/fail", {"mode": mode})
            for upstream, args in (
                ("demo.boards.list", {}),
                ("demo.notes.search", {"query": "x"}),
                ("demo.note.get", note),
            ):
                for path in ("/rpc", stand.paths(ALPHA_READER)["virtual_server"]):
                    err = gxg_error(stand.call(ALPHA_READER, stand.tool("t-alpha", upstream), args, path=path))
                    assert err and {k: err.get(k) for k in expected} == expected, (mode, upstream, path, err)
                    jsonschema.validate(err, ERROR)
    finally:
        stand.ctl("POST", "/_test/backend/fail", {"mode": None})
    assert ok(stand.call(ALPHA_READER, stand.tool("t-alpha", "demo.boards.list")))["boards"]


def test_mc03_timeout_after_the_request_was_sent(stand: Stand) -> None:
    """The backend got the search and does not answer: the connector's tool timeout (20 s, gxg-manifest.yaml)
    answers temporarily_unavailable, retryable for a read, before ContextForge's own 25 s (TOOL_TIMEOUT)."""
    stand.ctl("POST", "/_test/backend/fail", {"mode": "slow:40000"})
    try:
        for path in ("/rpc", stand.paths(ALPHA_READER)["virtual_server"]):
            started = time.monotonic()
            err = gxg_error(_search(stand, {"query": "status"} | ({} if path == "/rpc" else {"limit": 3}), path=path))
            took = time.monotonic() - started
            assert err == {
                "code": "temporarily_unavailable",
                "message": "backend did not answer in time",
                "retryable": True,
            }, (path, err)
            assert 19 < took < 25, (path, took)
    finally:
        stand.ctl("POST", "/_test/backend/fail", {"mode": None})


def test_mc03_connector_down_is_an_error_not_a_result(stand: Stand) -> None:
    sh("docker", "compose", "stop", "demo")
    try:
        for path in ("/rpc", stand.paths(ALPHA_READER)["virtual_server"]):
            resp = stand.call(ALPHA_READER, stand.tool("t-alpha", "demo.boards.list"), path=path)
            assert denied(resp) or gxg_error(resp), (path, resp)
            assert "structuredContent" not in resp.get("result", {}) or gxg_error(resp), resp
    finally:
        sh("docker", "compose", "start", "demo")
        stand.wait_demo_ready()
    assert ok(stand.call(ALPHA_READER, stand.tool("t-alpha", "demo.boards.list")))["boards"]


# MC-04 ------------------------------------------------------------------------------------------


def test_mc04_revoked_agent_token_stops_working_on_every_path(stand: Stand) -> None:
    admin = stand.admin()
    created = admin.post(
        "/v1/tokens",
        json={
            "name": f"m5-revoke-{time.time_ns()}",
            "team_id": stand.state["teams"]["t-alpha"]["id"],
            "user_email": stand.state["agents"][ALPHA_READER]["email"],
            "expires_in_days": 1,
        },
    ).json()
    token, tool = created["access_token"], stand.tool("t-alpha", "demo.boards.list")
    paths = ("/rpc", stand.paths(ALPHA_READER)["virtual_server"])
    for path in paths:
        assert ok(stand.call(ALPHA_READER, tool, path=path, token=token))
    assert admin.delete(f"/v1/tokens/{created['token']['id']}").status_code == 204
    revoked = time.monotonic()
    # ContextForge caches revocations per worker for AUTH_CACHE_REVOCATION_TTL (5 s on the stand, 30 s default)
    while True:
        statuses = [stand.rpc(p, "tools/call", {"name": tool, "arguments": {}}, token=token)[0] for p in paths]
        if statuses == [401, 401] or time.monotonic() - revoked > 15:
            break
        time.sleep(1)
    assert statuses == [401, 401], statuses
    for _ in range(10):  # every worker now refuses it
        assert [stand.rpc(p, "tools/call", {"name": tool, "arguments": {}}, token=token)[0] for p in paths] == [401] * 2
    assert ok(stand.call(ALPHA_READER, tool))  # the agent's other token is untouched


def _recreate_demo(identity_keys: str) -> None:
    sh("docker", "compose", "up", "-d", "--wait", "demo", env_override={"DEMO_IDENTITY_KEYS": identity_keys})


def test_mc04_adapter_key_rotation_and_revocation_at_the_connector(stand: Stand) -> None:
    """Rotation by kid: both keys during the overlap; after the old kid is removed its assertions are refused."""
    old = stand.env["DEMO_IDENTITY_KEYS"]
    new_key = Ed25519PrivateKey.generate()
    new = (
        "stand-adapter-2=ed25519:"
        + base64.urlsafe_b64encode(new_key.public_key().public_bytes_raw()).rstrip(b"=").decode()
    )
    params = {"name": "demo.boards.list", "arguments": {}}
    tool = stand.tool("t-alpha", "demo.boards.list")
    try:
        _recreate_demo(f"{old},{new}")
        replies = stand.direct(
            NET_CONNECTORS,
            [
                ({"X-GxG-Identity": stand.sign()}, params),
                ({"X-GxG-Identity": stand.sign(key=new_key, kid="stand-adapter-2")}, params),
            ],
        )
        assert all(ok(r)["boards"] for r in replies)
        assert ok(stand.call(ALPHA_READER, tool))["boards"]  # ContextForge still signs with the old kid
        _recreate_demo(new)
        old_jws, new_jws = stand.sign(), stand.sign(key=new_key, kid="stand-adapter-2")
        replies = stand.direct(
            NET_CONNECTORS, [({"X-GxG-Identity": old_jws}, params), ({"X-GxG-Identity": new_jws}, params)]
        )
        assert gxg_error(replies[0]) == {"code": "unauthenticated", "message": "token rejected", "retryable": False}
        assert ok(replies[1])["boards"]
        err = gxg_error(stand.call(ALPHA_READER, tool))
        assert err and err["code"] == "unauthenticated"  # fail closed, not open
    finally:
        _recreate_demo(old)
    assert ok(stand.call(ALPHA_READER, tool))["boards"]
    assert ok(stand.call(ALPHA_NOBODY, tool)) == {"boards": []}
