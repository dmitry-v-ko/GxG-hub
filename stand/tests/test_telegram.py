"""M11, Telegram connector through ContextForge on the dev stand: AC-M11-01 (SC-03…SC-05, SC-09, SC-10, MC-02,
MC-03), AC-M11-02…05, and the two M5 backlog items due before M11: the agent's Authorization header never
reaches a connector, and an agent with the `member` role cannot rename, register or replace tools.

The connector runs over the fake of the existing Telegram service (test profile, synthetic fixtures): no real
Telegram account. The fake's controls (counters, failure modes, the values that must not leak) are on
127.0.0.1:8090 inside the `telegram` container.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

import httpx2
import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from gxg_identity import core
from standlib import (
    ALPHA_NOBODY,
    ALPHA_READER,
    BETA_READER,
    CF,
    NET_CONNECTORS,
    REPO,
    SECRETS,
    Stand,
    denied,
    gxg_error,
    ok,
    sh,
)

TG_URL = "http://telegram:8080/mcp"
FIXTURES = json.loads((REPO / "connectors" / "telegram" / "fixtures" / "service.json").read_text(encoding="utf-8"))
REF = {ch["name"]: ch["chat_ref"] for c in FIXTURES["connections"].values() for ch in c["chats"]}
FORGED = "tgc1_" + "A" * 32
NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}
UNAUTH = {"code": "unauthenticated", "message": "token rejected", "retryable": False}
SEARCH_RESULT = json.loads((REPO / "schemas" / "search-result.schema.json").read_text())
ERROR = json.loads((REPO / "schemas" / "error.schema.json").read_text())
MESSAGES = json.loads((REPO / "connectors" / "telegram" / "schemas" / "messages.output.json").read_text())
SEARCH, THREAD, COLLECT = "telegram.messages.search", "telegram.thread.read", "telegram.messages.collect"


def tool(stand: Stand, tenant: str, upstream: str) -> str:
    return str(stand.state["teams"][tenant]["tg_tools"][upstream]["name"])


def paths(stand: Stand, agent: str) -> dict[str, str]:
    """/rpc and the agent's Telegram virtual server (MCP and SSE)."""
    server = f"/servers/{stand.team(agent)['tg_server_id']}"
    return {"rpc": "/rpc", "virtual_server": f"{server}/mcp/", "virtual_server_sse": f"{server}/sse"}


def call(stand: Stand, agent: str, upstream: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return stand.call(agent, tool(stand, stand.state["agents"][agent]["team"], upstream), args, **kw)


def stats(stand: Stand) -> dict[str, Any]:
    return stand.ctl("GET", "/_test/service/stats", service="telegram")


def requests(stand: Stand) -> int:
    """Requests that reached the service port (AC-M11-03)."""
    return sum(stats(stand)["calls"].values())


def fail(stand: Stand, mode: str | None) -> None:
    stand.ctl("POST", "/_test/service/fail", {"mode": mode}, service="telegram")


def audit(stand: Stand) -> list[dict[str, Any]]:
    return stand.audit("telegram")


def sign(stand: Stand, upstream: str, args: dict[str, Any], **overrides: Any) -> str:
    return stand.sign(**({"aud": "telegram", "tool": upstream, "ahs": core.args_hash(args)} | overrides))


# AC-M11-01: SC-03 -------------------------------------------------------------------------------


def test_sc03_identity_in_arguments_headers_and_meta_changes_nothing(stand: Stand) -> None:
    forged = {
        "X-Forwarded-User-Email": "reader@t-beta.agents.gxg.example.com",
        "X-Forwarded-User-Teams": stand.state["teams"]["t-beta"]["id"],
        "X-Tenant-Id": "t-beta",
        "X-GxG-Identity": stand.sign(aud="telegram", sub=BETA_READER, tenant_id="t-beta"),
        "X-GxG-Test-Identity": "W10.e30.AA",
    }
    meta = {"tenant_id": "t-beta", "agent_id": BETA_READER}
    spoof = {"tenant_id": "t-beta", "agent_id": BETA_READER, "approved": True, "user": BETA_READER}
    calls: list[tuple[str, dict[str, Any]]] = [
        (SEARCH, {"query": "status", "limit": 5}),
        (THREAD, {"chat_ref": REF["a-partners"], "limit": 3}),
        (COLLECT, {"chat_ref": REF["a-partners"], "limit": 3}),
        (THREAD, {"chat_ref": REF["b-public"]}),  # t-beta's chat stays not_found for t-alpha
    ]
    for path in paths(stand, ALPHA_READER).values():
        for upstream, args in calls:
            before = len(audit(stand))
            plain = call(stand, ALPHA_READER, upstream, args, path=path)
            spoofed = call(stand, ALPHA_READER, upstream, args | spoof, path=path, meta=meta, extra=forged)
            assert plain["result"] == spoofed["result"], (path, upstream)
            assert {(a["tenant"], a["principal"]) for a in audit(stand)[before:]} == {("t-alpha", ALPHA_READER)}


# AC-M11-01: SC-04 -------------------------------------------------------------------------------


def test_sc04_bad_identity_is_refused_by_the_connector_before_the_service(stand: Stand) -> None:
    """From inside the connectors network, bypassing ContextForge."""
    args = {"chat_ref": REF["a-partners"], "limit": 2}
    params = {"name": THREAD, "arguments": args}
    attacker = Ed25519PrivateKey.generate()
    replayed = sign(stand, THREAD, args)
    now = int(time.time())
    cases: dict[str, dict[str, str]] = {
        "valid (control)": {"X-GxG-Identity": replayed},
        "replayed": {"X-GxG-Identity": replayed},
        "forged signature": {"X-GxG-Identity": sign(stand, THREAD, args, key=attacker)},
        "unknown kid": {"X-GxG-Identity": sign(stand, THREAD, args, key=attacker, kid="attacker-1")},
        "expired": {"X-GxG-Identity": sign(stand, THREAD, args, iat=now - 120, exp=now - 60)},
        "other audience": {"X-GxG-Identity": sign(stand, THREAD, args, aud="demo")},
        "other tool": {"X-GxG-Identity": sign(stand, SEARCH, args)},
        "other arguments": {"X-GxG-Identity": sign(stand, THREAD, {"chat_ref": REF["a-sales"]})},
        "sub of another tenant": {"X-GxG-Identity": sign(stand, THREAD, args, sub=BETA_READER)},
        "garbage": {"X-GxG-Identity": "W10.e30.AA"},
        "no identity": {},
    }
    before = requests(stand)
    replies = dict(zip(cases, stand.direct(NET_CONNECTORS, [(h, params) for h in cases.values()], TG_URL), strict=True))
    assert ok(replies.pop("valid (control)"))["messages"]
    for name, resp in replies.items():
        assert gxg_error(resp) == UNAUTH, (name, resp)
    assert requests(stand) == before + 1  # only the control reached the service


# AC-M11-01: SC-05, SC-10; AC-M11-03 -------------------------------------------------------------


def test_ac_m11_03_unauthorized_chat_is_not_found_and_never_reaches_the_service(stand: Stand) -> None:
    unreadable = {
        "other tenant's chat": REF["b-public"],
        "no grant": REF["a-hidden"],
        "expired grant": REF["a-secret"],
        "forged chat_ref": FORGED,
    }
    before = requests(stand)
    for path in paths(stand, ALPHA_READER).values():
        replies = []
        for ref in unreadable.values():
            for upstream, args in (
                (THREAD, {"chat_ref": ref}),
                (COLLECT, {"chat_ref": ref}),
                (SEARCH, {"query": "status", "chat_refs": [REF["a-partners"], ref]}),
            ):
                replies.append(call(stand, ALPHA_READER, upstream, args, path=path)["result"])
        replies.append(call(stand, ALPHA_READER, COLLECT, {"chat_ref": REF["a-sales"]}, path=path)["result"])
        replies.append(call(stand, ALPHA_NOBODY, THREAD, {"chat_ref": REF["a-partners"]}, path=path)["result"])
        # byte-identical on each path (ContextForge's own envelope differs between paths, not between chats)
        assert len({json.dumps(r, sort_keys=True) for r in replies}) == 1, (path, replies[:2])
        assert replies[0]["structuredContent"] == NOT_FOUND
    assert requests(stand) == before  # the counter of requests to the service port stays at 0


def test_sc10_search_returns_only_granted_chats_without_hidden_counts(stand: Stand) -> None:
    got = ok(call(stand, ALPHA_READER, SEARCH, {"query": "budget", "limit": 50}))
    want = {REF["a-partners"], REF["a-sales"], REF["a-gap"]}
    assert {i["id"].split(":")[0] for i in got["items"]} == want
    assert set(got["source_refs"]) == {f"telegram:tg-main/chat/{r}" for r in want}
    assert set(got) == {"items", "next_cursor", "complete", "partial_reasons", "source_refs"}
    jsonschema.validate(got, SEARCH_RESULT)
    beta = ok(call(stand, BETA_READER, SEARCH, {"query": "status", "limit": 50}))  # a mask: the service scope
    assert {i["id"].split(":")[0] for i in beta["items"]} == {REF["b-public"], REF["b-ops"]}


def test_sc10_nothing_granted_and_bad_bundle_are_refused_before_the_service(stand: Stand) -> None:
    before = requests(stand)
    assert gxg_error(call(stand, ALPHA_NOBODY, THREAD, {"chat_ref": REF["a-partners"]})) == NOT_FOUND
    assert requests(stand) == before
    sig = SECRETS / "bundle" / "grants-bundle.json.sig"
    good = sig.read_text()
    try:
        sig.write_text(good[::-1])  # a signature that does not verify: GR-12
        sh("docker", "compose", "restart", "telegram")
        deadline, err = time.monotonic() + 60, None
        while err is None and time.monotonic() < deadline:  # until the restarted connector answers itself
            err = gxg_error(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))
            time.sleep(0 if err else 1)
        assert err and (err["code"], err["retryable"]) == ("temporarily_unavailable", True), err
        assert requests(stand) == 0  # a fresh fake after the restart: nothing reached it
    finally:
        sig.write_text(good)
        sh("docker", "compose", "restart", "telegram")
        _wait_ready(stand)
    assert ok(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))["messages"]


def _wait_ready(stand: Stand, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "healthy" in sh("docker", "inspect", "-f", "{{.State.Health.Status}}", stand.container("telegram")):
            return
        time.sleep(1)
    raise AssertionError("telegram did not become healthy")


# AC-M11-01: SC-09; AC-M11-05 --------------------------------------------------------------------


def test_ac_m11_05_1000_calls_leave_no_texts_peer_ids_or_sessions_in_logs(stand: Stand) -> None:
    known = stand.ctl("GET", "/_test/service/secrets", service="telegram")
    plan: list[tuple[str, str, dict[str, Any]]] = [
        (ALPHA_READER, SEARCH, {"query": "status", "limit": 20}),
        (ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"], "limit": 50}),
        (ALPHA_READER, COLLECT, {"chat_ref": REF["a-partners"], "limit": 100}),
        (ALPHA_READER, THREAD, {"chat_ref": REF["a-hidden"]}),
        (BETA_READER, SEARCH, {"query": "beta secret plan"}),
        (BETA_READER, COLLECT, {"chat_ref": REF["b-ops"]}),
        (ALPHA_NOBODY, THREAD, {"chat_ref": REF["a-sales"]}),
        (BETA_READER, THREAD, {"chat_ref": FORGED}),
    ]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1))
    replies = []
    for i in range(1000):
        agent, upstream, args = plan[i % len(plan)]
        path = ("/rpc", paths(stand, agent)["virtual_server"])[i % 2]
        replies.append(call(stand, agent, upstream, args, path=path))
    logs = sh("docker", "compose", "logs", "--no-log-prefix", "--since", since, "telegram")
    cf_logs = stand.logs("contextforge")
    assert sum(1 for ln in logs.splitlines() if ln.startswith('{"request_id"')) >= 1000  # every call audited
    assert [t for t in known["texts"] if t in logs] == []
    assert [p for p in known["peer_ids"] if p in logs or p.lstrip("-") in logs] == []
    assert [s for s in known["sessions"] if s in logs] == []
    assert "beta secret plan" not in logs  # queries are not logged
    blob = json.dumps(replies, ensure_ascii=False)
    for where, text in (("replies", blob), ("contextforge logs", cf_logs), ("telegram logs", logs)):
        assert [p for p in known["peer_ids"] if p in text] == [], where  # MCC-04: no provider ids in replies
        assert [s for s in known["sessions"] if s in text] == [], where
        assert stand.sign().split(".")[0] not in text, where  # no identity assertion
        for agent in stand.state["agents"].values():
            assert agent["token"] not in text, where
    assert any(t in blob for t in known["texts"])  # the replies carry texts: the log scan is meaningful


# backlog M5 (2): Authorization does not reach the connector --------------------------------------


def test_agent_authorization_header_never_reaches_the_connector(stand: Stand) -> None:
    before = len(audit(stand))
    for agent in (ALPHA_READER, BETA_READER):
        for path in paths(stand, agent).values():
            ok(
                call(
                    stand, agent, THREAD, {"chat_ref": REF["a-sales" if agent == ALPHA_READER else "b-ops"]}, path=path
                )
            )
    lines = audit(stand)[before:]
    assert len(lines) == 6 and all(a["authorization_header"] is False for a in lines), lines


# backlog M5 (3): a member cannot rename, register or replace tools ---------------------------------


def test_member_role_cannot_rename_register_or_replace_tools(stand: Stand) -> None:
    """unified_pdp allows by exact tool name: an agent that could rename a tool or register one under an allowed
    name would widen its own rights. Every agent is `member` of its team; all such writes must be refused."""
    team = stand.team(ALPHA_NOBODY)
    thread = team["tg_tools"][THREAD]["id"]
    search_name = tool(stand, "t-alpha", SEARCH)
    attempts: list[tuple[str, str, dict[str, Any] | None]] = [
        ("PUT", f"/v1/tools/{thread}", {"customName": search_name}),
        (
            "POST",
            "/v1/tools",
            {
                "tool": {"name": search_name, "url": TG_URL, "integration_type": "REST", "request_type": "POST"},
                "team_id": team["id"],
                "visibility": "team",
            },
        ),
        ("DELETE", f"/v1/tools/{thread}", None),
        ("POST", f"/v1/tools/{thread}/state?activate=false", None),
        (
            "POST",
            "/v1/gateways",
            {"name": "evil", "url": TG_URL, "transport": "STREAMABLEHTTP", "teamId": team["id"], "visibility": "team"},
        ),
        ("PUT", f"/v1/gateways/{team['tg_gateway_id']}", {"name": "evil"}),
        ("PUT", f"/v1/servers/{team['tg_server_id']}", {"associated_tools": []}),
        ("POST", "/v1/servers", {"server": {"name": "vs-evil"}, "team_id": team["id"], "visibility": "team"}),
        ("POST", f"/admin/tools/{thread}/edit", {"customName": search_name}),
        ("POST", "/admin/gateways", {"name": "evil", "url": TG_URL}),
        ("POST", "/admin/import/configuration", {}),
        ("POST", f"/v1/teams/{team['id']}/members", {"email": "x@t-alpha.agents.gxg.example.com", "role": "owner"}),
    ]
    with httpx2.Client(base_url=CF, timeout=30) as http:
        for agent in (ALPHA_NOBODY, ALPHA_READER):
            headers = {"Authorization": f"Bearer {stand.token(agent)}"}
            for method, path, body in attempts:
                r = http.request(method, path, json=body, headers=headers)
                assert r.status_code in (401, 403), (agent, method, path, r.status_code, r.text[:200])
    names = {x["name"] for x in stand.tools(ALPHA_NOBODY)}
    assert tool(stand, "t-alpha", THREAD) in names and search_name in names
    assert denied(call(stand, ALPHA_NOBODY, SEARCH, {"query": "status"}))  # still default deny for nobody


# AC-M11-01: MC-02 -------------------------------------------------------------------------------


def test_mc02_search_schema_pagination_cursor_and_limits(stand: Stand) -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(30):
        page = ok(
            call(stand, ALPHA_READER, SEARCH, {"query": "status", "limit": 9} | ({"cursor": cursor} if cursor else {}))
        )
        jsonschema.validate(page, SEARCH_RESULT)
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) > 9
    first = ok(call(stand, ALPHA_READER, SEARCH, {"query": "status", "limit": 9}))
    other = gxg_error(call(stand, ALPHA_READER, SEARCH, {"query": "open", "limit": 9, "cursor": first["next_cursor"]}))
    assert other and other["code"] == "invalid_input"
    empty = ok(call(stand, ALPHA_READER, SEARCH, {"query": "zzz-nothing", "chat_refs": [REF["a-sales"]]}))
    assert empty == {"items": [], "next_cursor": None, "complete": True, "partial_reasons": [], "source_refs": []}
    bad_args: list[tuple[str, dict[str, Any]]] = [
        (SEARCH, {"query": "status", "limit": 51}),
        (SEARCH, {"query": ""}),
        (THREAD, {"chat_ref": "-1001000000101"}),
        (COLLECT, {"chat_ref": REF["a-partners"], "limit": 101}),
        (COLLECT, {"chat_ref": REF["a-partners"], "cursor": "x"}),
    ]
    before = requests(stand)
    for upstream, bad in bad_args:  # ContextForge validates against the inputSchema first
        assert "Internal error" in denied(call(stand, ALPHA_READER, upstream, bad)), (upstream, bad)
    replies = stand.direct(  # the connector enforces the same schema itself (MCC-05)
        NET_CONNECTORS,
        [({"X-GxG-Identity": sign(stand, u, b)}, {"name": u, "arguments": b}) for u, b in bad_args],
        TG_URL,
    )
    for (upstream, bad), resp in zip(bad_args, replies, strict=True):
        err = gxg_error(resp)
        assert err and (err["code"], err["retryable"]) == ("invalid_input", False), (upstream, bad, resp)
        jsonschema.validate(err, ERROR)
    assert requests(stand) == before


def test_mc02_thread_and_collect_page_to_the_end(stand: Stand) -> None:
    ids, before = [], None
    for _ in range(10):
        page = ok(
            call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-sales"], "limit": 5, "before_message_id": before})
        )
        jsonschema.validate(page, MESSAGES)
        ids += [m["message_id"] for m in page["messages"]]
        before = page["next_cursor"]
        if before is None:
            break
    assert ids == sorted(ids, reverse=True) and ids[-1] == 1 and len(ids) == len(set(ids))
    got, cursor = [], None
    for _ in range(30):
        page = ok(
            call(
                stand,
                BETA_READER,
                COLLECT,
                {"chat_ref": REF["b-ops"], "limit": 3} | ({"cursor": cursor} if cursor else {}),
            )
        )
        jsonschema.validate(page, MESSAGES)
        got += [m["message_id"] for m in page["messages"]]
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    assert got == list(range(1, len(got) + 1)) and len(got) == 8
    gap = ok(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-gap"]}))
    assert (gap["complete"], gap["partial_reasons"], gap["untrusted"]) == (False, ["other"], True)


# AC-M11-01: MC-03; AC-M11-04 --------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [1, 7, 3600])
def test_ac_m11_04_flood_wait_is_rate_limited_with_telegrams_delay(stand: Stand, seconds: int) -> None:
    fail(stand, f"flood_wait:{seconds}")
    try:
        for upstream, args in (
            (SEARCH, {"query": "status"}),
            (THREAD, {"chat_ref": REF["a-partners"]}),
            (COLLECT, {"chat_ref": REF["a-partners"]}),
        ):
            for path in ("/rpc", paths(stand, ALPHA_READER)["virtual_server"]):
                err = gxg_error(call(stand, ALPHA_READER, upstream, args, path=path))
                assert err and (err["code"], err["retryable"]) == ("rate_limited", True), (upstream, path, err)
                assert seconds <= err["retry_after_s"] <= seconds + 1, err
                jsonschema.validate(err, ERROR)
    finally:
        fail(stand, None)


def test_mc03_service_failures_keep_codes_and_retryable_through_contextforge(stand: Stand) -> None:
    want = {
        "busy": ("temporarily_unavailable", True),
        "auth_required": ("temporarily_unavailable", True),
        "provider_error": ("temporarily_unavailable", True),
        "peer_inaccessible": ("not_found", False),
    }
    try:
        for mode, expected in want.items():
            fail(stand, mode)
            for upstream, args in ((SEARCH, {"query": "status"}), (THREAD, {"chat_ref": REF["a-partners"]})):
                for path in ("/rpc", paths(stand, ALPHA_READER)["virtual_server"]):
                    err = gxg_error(call(stand, ALPHA_READER, upstream, args, path=path))
                    assert err and (err["code"], err["retryable"]) == expected, (mode, upstream, path, err)
                    jsonschema.validate(err, ERROR)
    finally:
        fail(stand, None)


def test_mc03_timeout_after_the_request_was_sent(stand: Stand) -> None:
    """The service got the request and does not answer: the connector's 20 s tool timeout answers before
    ContextForge's 25 s (TOOL_TIMEOUT)."""
    fail(stand, "slow:40000")
    try:
        started = time.monotonic()
        err = gxg_error(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))
        took = time.monotonic() - started
    finally:
        fail(stand, None)
    assert err == {
        "code": "temporarily_unavailable",
        "message": "Telegram service did not answer in time",
        "retryable": True,
    }, err
    assert 19 < took < 25, took


def test_mc03_connector_down_is_an_error_not_a_result(stand: Stand) -> None:
    sh("docker", "compose", "stop", "telegram")
    try:
        resp = call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]})
        assert denied(resp) or gxg_error(resp), resp
    finally:
        sh("docker", "compose", "start", "telegram")
        _wait_ready(stand)
    assert ok(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))["messages"]


# AC-M11-02 --------------------------------------------------------------------------------------


def test_ac_m11_02_the_connector_opens_no_telegram_session(stand: Stand) -> None:
    before = stats(stand)["open_sessions"]
    for i in range(20):
        fail(stand, ("busy", "flood_wait:5", None, "auth_required")[i % 4])
        call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]})
        call(stand, BETA_READER, SEARCH, {"query": secrets.token_hex(4)})
    fail(stand, None)
    assert stats(stand)["open_sessions"] == before == 2
    env = sh("docker", "inspect", "-f", "{{json .Config.Env}}", stand.container("telegram"))
    assert "TG_SESSION" not in env and "TG_API" not in env  # no session material is ever given to the connector
