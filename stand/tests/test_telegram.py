"""M11, Telegram connector through ContextForge on the dev stand: AC-M11-01 (SC-03…SC-05, SC-09, SC-10, MC-02,
MC-03), AC-M11-02…05, and the two M5 backlog items due before M11: the agent's Authorization header never
reaches a connector, and an agent with the `member` role cannot rename, register or replace tools.

ADR-0004: the connector reads the `telegram-archive` database (the vendored gxg_agents_v2 migrations, the read-only
role, synthetic fixtures of connectors/telegram/archive/seed.py) and never talks to Telegram: no account, no session.
Requests to the archive are counted from its `log_connections` lines (application_name `gxg-telegram`); failure
modes are real: the database stopped, a table lock, a flood-wait deferral in `connection_state`.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    NET_EDGE,
    REPO,
    SECRETS,
    Stand,
    denied,
    gxg_error,
    ok,
    sh,
)

sys.path.insert(0, str(REPO / "connectors" / "telegram" / "archive"))
import seed

TG_URL = "http://telegram:8080/mcp"
REF = seed.REF
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


def archive_sql(sql: str) -> str:
    """SQL as the archive's owner inside its container (the stand's stand-in for the service's sync)."""
    return sh(
        "docker",
        "compose",
        "exec",
        "-T",
        "telegram-archive",
        "psql",
        "-U",
        "archive_owner",
        "-d",
        "archive",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    )


def requests(stand: Stand) -> int:
    """Connections the connector opened to the archive for tool calls (AC-M11-03): one per request to the port;
    readiness probes connect as `gxg-telegram-ready` and are not counted."""
    lines = stand.logs("telegram-archive").splitlines()
    return sum(
        "connection authorized" in ln
        and "application_name=gxg-telegram" in ln
        and "application_name=gxg-telegram-ready" not in ln
        for ln in lines
    )


def defer(connection: str, seconds: int) -> None:
    """A Telegram flood wait as the service's sync records it (CON-TG-03). Test-owned constants only."""
    assert connection in {"svc-alpha", "svc-beta"}
    archive_sql(f"""INSERT INTO telegram_archive.connection_state (connection_id, retry_after) VALUES
        ('{connection}', clock_timestamp() + interval '{int(seconds)} s') ON CONFLICT (connection_id)
        DO UPDATE SET retry_after = excluded.retry_after""")  # noqa: S608


def undefer() -> None:
    archive_sql("DELETE FROM telegram_archive.connection_state")


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


def test_sc05_and_ac_m11_03_unauthorized_chat_is_not_found_and_never_reaches_the_service(stand: Stand) -> None:
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
        assert requests(stand) == before  # refused before the archive
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


def test_sc09_and_ac_m11_05_1000_calls_leave_no_texts_peer_ids_or_secrets_in_logs(stand: Stand) -> None:
    """Known values: the fixture texts and peer IDs in the archive, the reader role's password (the connector's one
    secret, SEC-16). The connector holds no Telegram session string to leak (ADR-0004)."""
    known = {
        "texts": seed.texts(),
        "peer_ids": [str(p) for p in seed.PEER.values()],
        "secrets": [stand.env["TELEGRAM_ARCHIVE_PASSWORD"]],
    }
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
        if i % 97 == 0:
            defer("svc-alpha", 2)  # flood-wait deferral: collect rate_limited, reads flagged
        replies.append(call(stand, agent, upstream, args, path=path))
    undefer()
    logs = sh("docker", "compose", "logs", "--no-log-prefix", "--since", since, "telegram")
    cf_logs = stand.logs("contextforge")
    assert sum(1 for ln in logs.splitlines() if ln.startswith('{"request_id"')) >= 1000  # every call audited
    assert [t for t in known["texts"] if t in logs] == []
    assert [p for p in known["peer_ids"] if p in logs or p.lstrip("-") in logs] == []
    assert [s for s in known["secrets"] if s in logs] == [] and seed.READER not in logs
    assert "beta secret plan" not in logs  # queries are not logged
    blob = json.dumps(replies, ensure_ascii=False)
    for where, text in (("replies", blob), ("contextforge logs", cf_logs), ("telegram logs", logs)):
        assert [p for p in known["peer_ids"] if p in text] == [], where  # MCC-04: no provider ids in replies
        assert [s for s in known["secrets"] if s in text] == [], where
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
        (THREAD, {"chat_ref": "not-a-chat-ref"}),
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
    """The service's sync records Telegram's flood wait (connection_state.retry_after): collect is rate_limited with
    the remaining delay (± 1 s; a call through ContextForge takes well under a second); reads are flagged."""
    try:
        for path in ("/rpc", paths(stand, ALPHA_READER)["virtual_server"]):
            defer("svc-alpha", seconds)  # re-armed before every call: a 1 s window must not end mid-loop
            err = gxg_error(call(stand, ALPHA_READER, COLLECT, {"chat_ref": REF["a-partners"]}, path=path))
            assert err and (err["code"], err["retryable"]) == ("rate_limited", True), (path, err)
            assert seconds - 1 <= err["retry_after_s"] <= seconds, err
            jsonschema.validate(err, ERROR)
            for upstream, args in ((SEARCH, {"query": "status"}), (THREAD, {"chat_ref": REF["a-partners"]})):
                defer("svc-alpha", 3600)  # reads only flag the deferral; the delay value is checked on collect
                page = ok(call(stand, ALPHA_READER, upstream, args, path=path))
                assert "rate_limited" in page["partial_reasons"] and page["complete"] is False, (upstream, page)
    finally:
        undefer()
    assert ok(call(stand, ALPHA_READER, COLLECT, {"chat_ref": REF["a-partners"]}))["messages"]


def test_mc03_archive_down_is_temporarily_unavailable_through_contextforge(stand: Stand) -> None:
    sh("docker", "compose", "stop", "telegram-archive")
    try:
        for upstream, args in (
            (SEARCH, {"query": "status"}),
            (THREAD, {"chat_ref": REF["a-partners"]}),
            (COLLECT, {"chat_ref": REF["a-partners"]}),
        ):
            for path in ("/rpc", paths(stand, ALPHA_READER)["virtual_server"]):
                err = gxg_error(call(stand, ALPHA_READER, upstream, args, path=path))
                assert err == {
                    "code": "temporarily_unavailable",
                    "message": "Telegram archive is not available",
                    "retryable": True,
                }, (upstream, path, err)
                jsonschema.validate(err, ERROR)
    finally:
        sh("docker", "compose", "start", "telegram-archive")
        _wait_ready(stand)
    assert ok(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))["messages"]


def test_mc03_timeout_after_the_request_was_sent(stand: Stand) -> None:
    """The archive got the query and does not answer (a table lock held for 40 s): the reader role's 15 s
    statement_timeout or the connector's 20 s tool timeout ends it before ContextForge's 25 s (TOOL_TIMEOUT);
    a read stays retryable."""
    lock = subprocess.Popen(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "telegram-archive",
            "psql",
            "-U",
            "archive_owner",
            "-d",
            "archive",
            "-c",
            "BEGIN; LOCK TABLE telegram_archive.messages IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(40); COMMIT;",
        ],
        cwd=REPO,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        started = time.monotonic()
        err = gxg_error(call(stand, ALPHA_READER, THREAD, {"chat_ref": REF["a-partners"]}))
        took = time.monotonic() - started
    finally:
        lock.kill()
        archive_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%pg_sleep(40)%'"
            " AND pid <> pg_backend_pid()"
        )
    assert err and (err["code"], err["retryable"]) == ("temporarily_unavailable", True), err
    assert err["message"] in ("Telegram archive is not available", "Telegram archive did not answer in time"), err
    assert 14 < took < 25, took


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


def test_ac_m11_02_the_connector_has_no_session_no_telegram_route_and_only_reads(stand: Stand) -> None:
    """ADR-0004: the connector cannot open a Telegram session. Its container has no session material and no route
    out of the stand (networks `connectors` and `tg-archive` are internal); its database role cannot write."""
    env = sh("docker", "inspect", "-f", "{{json .Config.Env}}", stand.container("telegram"))
    assert "TG_SESSION" not in env and "TG_API" not in env
    probe = (
        "import socket\n"
        "for target in (('api.telegram.org', 443), ('149.154.167.50', 443)):\n"
        "    try:\n"
        "        socket.create_connection(target, timeout=3).close(); print('reached', target[0])\n"
        "    except OSError as e:\n"
        "        print('blocked', target[0], type(e).__name__)\n"
    )
    out = stand.in_demo(probe, service="telegram")
    assert out.count("blocked") == 2 and "reached" not in out, out
    reader = [
        "docker",
        "compose",
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={stand.env['TELEGRAM_ARCHIVE_PASSWORD']}",
        "telegram-archive",
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        seed.READER,
        "-d",
        "archive",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "SET default_transaction_read_only = off",
        "-c",
        "INSERT INTO telegram_archive.connection_state VALUES ('svc-alpha', now())",
    ]
    refused = subprocess.run(reader, cwd=REPO, capture_output=True, text=True, timeout=60, check=False)
    assert refused.returncode != 0 and "permission denied" in refused.stderr, refused.stderr
    assert archive_sql("SELECT count(*) FROM telegram_archive.connection_state") == "0\n"


# AC-M11-01 rest of stage B for the new gateway (MCC-10): SC-01, SC-06, SC-08, MC-01 -----------------------------


def test_sc01_other_team_telegram_tools_are_refused_by_name(stand: Stand) -> None:
    before, requests_before = len(audit(stand)), requests(stand)
    for agent, other in ((ALPHA_READER, "t-beta"), (BETA_READER, "t-alpha")):
        listed = {t["name"] for t in stand.tools(agent, paths(stand, agent)["virtual_server"])}
        assert listed == {tool(stand, stand.state["agents"][agent]["team"], u) for u in (SEARCH, THREAD, COLLECT)}
        for upstream in (SEARCH, THREAD, COLLECT):
            name = tool(stand, other, upstream)
            assert name not in listed
            for path in paths(stand, agent).values():
                resp = stand.call(agent, name, {"chat_ref": REF["b-ops"], "query": "status"}, path=path)
                assert denied(resp) or gxg_error(resp), (agent, name, path, resp)
                assert "structuredContent" not in resp.get("result", {}) or gxg_error(resp)
    assert len(audit(stand)) == before and requests(stand) == requests_before  # nothing reached the connector


def test_sc06_agent_network_cannot_reach_telegram_and_inside_without_identity_is_unauthenticated(stand: Stand) -> None:
    edge = stand.probe(
        NET_EDGE, [{"url": TG_URL, "body": {}}, {"url": "http://10.250.0.9:8080/healthz", "method": "GET"}]
    )
    assert all("error" in r for r in edge), edge
    before = requests(stand)
    inside = stand.direct(
        NET_CONNECTORS, [({}, {"name": THREAD, "arguments": {"chat_ref": REF["a-partners"]}})], TG_URL
    )
    assert [gxg_error(r) for r in inside] == [UNAUTH]
    archive = stand.probe(NET_CONNECTORS, [{"url": "http://telegram-archive:5432/", "method": "GET", "timeout": 3}])
    assert "error" in archive[0], archive  # the archive is only on tg-archive, not reachable from connectors
    assert requests(stand) == before


def test_sc08_injection_is_data_through_contextforge(stand: Stand) -> None:
    before = len(audit(stand))
    got = ok(call(stand, ALPHA_READER, SEARCH, {"query": "игнорируй инструкции"}))
    assert [i["excerpt"] for i in got["items"]] == [seed.SPECIAL[0][2]]
    names = [t["name"] for t in stand.tools(ALPHA_READER, paths(stand, ALPHA_READER)["virtual_server"])]
    assert not [n for n in names if "propose" in n or "send" in n]
    assert denied(stand.call(ALPHA_READER, "telegram-t-alpha-telegram-message-propose", {"chat_ref": REF["a-sales"]}))
    assert {a["tool"] for a in audit(stand)[before:]} == {SEARCH}


def test_mc01_contextforge_lists_the_connectors_schemas(stand: Stand) -> None:
    import yaml

    manifest = yaml.safe_load((REPO / "connectors" / "telegram" / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    sys.path.insert(0, str(REPO / "connectors" / "telegram" / "src"))
    from telegram_connector.tools import load_schema

    listed = {t["name"]: t for t in stand.tools(BETA_READER, paths(stand, BETA_READER)["virtual_server"])}
    for t in manifest["tools"]:
        got = listed[tool(stand, "t-beta", t["name"])]
        assert got["inputSchema"] == load_schema(t["input_schema"]), t["name"]
        assert got.get("outputSchema") == load_schema(t["output_schema"]), t["name"]
