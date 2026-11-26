"""Checks 1-6 of reports/M11-cf-stand.md on the running gxg-m11cf stand (stand/m11cf/run.sh, KEEP=1).

  uv run python stand/m11cf/checks.py all|register|schema|limits|servers|hubexec

Evidence (replies cut, never a token) -> stand/secrets/m11cf/evidence.json and stdout; exit 1 if a check failed.
`limits` and `hubexec` recreate/restart ContextForge with other settings and restore the stand ones afterwards.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO / "stand" / "tests"), str(REPO / "stand"), str(REPO / "gateway" / "contextforge")]
import bootstrap as base  # noqa: E402
from init import SECRETS, env_values, plugins_yaml  # noqa: E402
from standlib import NET_CONNECTORS, PROTO, Stand, mcp_body, sh  # noqa: E402

OUT = SECRETS / "m11cf"
S = json.loads((OUT / "state.json").read_text())
ROLES = json.loads((OUT / "roles.json").read_text())
REF = (OUT / "chat_ref").read_text().strip()
R, C = "agent:t-ext-test:reader", "agent:t-ext-test:collector"
_state = json.loads((SECRETS / "state.json").read_text())
_state["agents"].update({k: v | {"team": "t-ext-test"} for k, v in S["agents"].items()})
st = Stand(_state, env_values())
EVIDENCE: dict[str, Any] = {}
FAILED: list[str] = []


def T(upstream: str) -> str:
    return str(S["tools"][upstream]["name"])


def vs(agent: str) -> str:
    return f"/servers/{S['servers'][S['agents'][agent]['server']]['id']}/mcp/"


def cut(x: Any, n: int = 400) -> str:
    return json.dumps(x, ensure_ascii=False)[:n]


def verdict(check: str, ok: bool, **evidence: Any) -> None:
    EVIDENCE[check] = {"verdict": "passed" if ok else "failed", **evidence}
    if not ok:
        FAILED.append(check)
    print(check, EVIDENCE[check]["verdict"], cut(evidence, 1500))


def code(resp: dict[str, Any]) -> str:
    """-32xxx for a JSON-RPC error, the MCC-05 code of an isError result, `ok`, or ContextForge's error text."""
    if "error" in resp:
        return f"jsonrpc {resp['error'].get('code')}"
    res = resp.get("result", {})
    if res.get("isError"):
        sc = res.get("structuredContent")
        return sc["code"] if sc else "text: " + " ".join(c.get("text", "")[:60] for c in res.get("content", []))
    return "ok"


def register() -> None:
    want = ["telegram.ext.call", "telegram.ext.describe", "telegram.ext.search", "telegram.participants.collect"]
    names = {u: S["tools"].get(u, {}).get("name") for u in want}
    rule = {u: f"telegram-t-ext-test-{u.replace('.', '-')}" for u in want}
    verdict("1_register", names == rule, tools=names, all_gateway_tools=sorted(S["tools"]))


def schema() -> None:
    srv = S["servers"][S["agents"][R]["server"]]["id"]
    paths = {"rpc": "/rpc", "virtual_server_mcp": vs(R), "virtual_server_sse": f"/servers/{srv}/sse"}
    calls = {
        "entry with arguments": {"name": "chats.get_info", "arguments": {"chat_ref": REF}},
        "nested generic arguments": {"name": "chats.history", "arguments": {"chat_ref": REF, "x": {"y": [1, None]}}},
        "no arguments key": {"name": "chats.get_info"},
        "unknown entry": {"name": "no.such_entry", "arguments": {}},
    }
    out = {
        f"{c} via {p}": code(st.call(R, T("telegram.ext.call"), a, path=path))
        for c, a in calls.items()
        for p, path in paths.items()
    }
    bad = {p: code(st.call(R, T("telegram.ext.call"), {"name": "NOT A NAME"}, path=path)) for p, path in paths.items()}
    ok = not any(v.startswith("jsonrpc -32603") for v in out.values())
    verdict("2_schema", ok, valid_calls=out, invalid_name_rejected_by_contextforge=bad)


def servers() -> None:
    seen = {a: sorted(t["name"] for t in st.tools(a, vs(a))) for a in (R, C)}
    want = {a: sorted(T(t) for t in ROLES["servers"][ROLES["agents"][a]]) for a in (R, C)}
    rpc = {a: sorted(t["name"] for t in st.tools(a)) for a in (R, C)}
    entries = {}
    for a in (R, C):
        r = st.call(a, T("telegram.ext.search"), {"limit": 20}, path=vs(a))
        entries[a] = [e["name"] for e in r["result"]["structuredContent"]["entries"]]
    pii = {"users.profile_photos", "users.profile_photo_get"}
    photo = {"name": "users.profile_photos", "arguments": {"chat_ref": REF, "user_ref": "tgu1_" + "A" * 32}}
    denied = {
        "reader participants.collect (/rpc)": code(
            st.call(R, T("telegram.participants.collect"), {"chat_refs": [REF]})
        ),
        "reader ext.call users.profile_photos": code(st.call(R, T("telegram.ext.call"), photo, path=vs(R))),
        "collector ext.call users.profile_photos": code(st.call(C, T("telegram.ext.call"), photo, path=vs(C))),
        "collector participants.collect": code(
            st.call(C, T("telegram.participants.collect"), {"chat_refs": [REF], "include_pii": True}, path=vs(C))
        ),
    }
    ok = (
        seen == want
        and seen[R] != seen[C]
        and not pii & set(entries[R])
        and pii <= set(entries[C])
        and denied["reader participants.collect (/rpc)"] == "jsonrpc -32602"
        and denied["reader ext.call users.profile_photos"] == "permission_denied"
        and denied["collector ext.call users.profile_photos"] != "permission_denied"
    )
    verdict(
        "5_servers",
        ok,
        virtual_server_tools=seen,
        expected=want,
        rpc_tools_of_the_team=rpc,
        ext_search=entries,
        calls=denied,
    )


def _cf(**env: str) -> None:
    sh("docker", "compose", "up", "-d", "--wait", "contextforge", env_override=env, timeout=400)
    base.wait_healthy()


def limits() -> None:
    _cf(M11CF_TOOL_TIMEOUT="5", M11CF_MEDIUM_RPM="6", M11CF_MEDIUM_BURST="0", M11CF_LOCKOUT="false")
    try:
        args = {"name": "chats.get_info", "arguments": {"chat_ref": REF}}
        h = st.headers(st.token(R))
        r = st.http.post(
            vs(R),
            headers=h,
            json=mcp_body(
                "initialize",
                {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "m11cf", "version": "0"}},
            ),
        )
        h |= {"Mcp-Session-Id": r.headers.get("mcp-session-id", ""), "MCP-Protocol-Version": PROTO}
        burst = []
        for i in range(8):
            r = st.http.post(
                vs(R),
                headers=h,
                json=mcp_body("tools/call", {"name": T("telegram.ext.call"), "arguments": args}, i + 10),
            )
            burst.append({"status": r.status_code, "retry-after": r.headers.get("retry-after"), "body": r.text[:150]})
        rpc = st.http.post(
            "/rpc",
            headers=st.headers(st.token(C)),
            json=mcp_body("tools/call", {"name": T("telegram.ext.call"), "arguments": args}),
        )
        rpc_tier = {k: rpc.headers[k] for k in rpc.headers if k.lower().startswith("x-ratelimit-limit")}
        cid = sh("docker", "compose", "ps", "-q", "tgsvc").strip()
        sh("docker", "pause", cid)
        try:
            time.sleep(61)  # the MEDIUM window of the burst above is over
            slow, took = {}, {}
            for p, path in (("virtual_server", vs(C)), ("rpc", "/rpc")):
                t0 = time.monotonic()
                slow[p] = cut(st.call(C, T("telegram.ext.call"), args, path=path), 200)
                took[p] = round(time.monotonic() - t0, 1)
        finally:
            sh("docker", "unpause", cid)
        a = st.admin()
        tid = S["tools"]["telegram.ext.call"]["id"]
        put = a.put(f"/v1/tools/{tid}", json={"timeout_ms": 45000}).status_code
        after = a.get(f"/v1/tools/{tid}").json().get("timeoutMs")
    finally:
        _cf()
    limited = [b for b in burst if b["status"] == 429]
    ok = (
        bool(limited)
        and burst[0]["status"] == 200
        and all(4.5 <= t <= 9 for t in took.values())
        and all("MCP server error" in v for v in slow.values())
    )
    verdict(
        "4_limits",
        ok,
        settings="TOOL_TIMEOUT=5 RATE_LIMIT_MEDIUM_RPM=6 BURST=0 LOCKOUT=false (stand values restored afterwards)",
        burst_virtual_server=burst,
        rpc_tier_limit=rpc_tier,
        paused_service={"seconds": took, "reply": slow},
        per_tool_timeout_ms={"PUT status": put, "timeoutMs after": after},
    )


HUBEXEC_IP, HUBEXEC = "10.250.0.11", "gxg-m11cf-hubexec"  # .11 is SC-07's redirector only while that test runs


def hubexec() -> None:
    sh("docker", "rm", "-f", HUBEXEC, check=False)
    sh(
        "docker",
        "run",
        "-d",
        "--name",
        HUBEXEC,
        "--label",
        "gxg.owner=gxg-m11cf",
        "--network",
        NET_CONNECTORS,
        "--ip",
        HUBEXEC_IP,
        "--entrypoint",
        "python",
        "-v",
        f"{HERE / 'hubexec_server.py'}:/srv/hubexec.py:ro",
        "gxg-telegram-service:m11-4-local",
        "-I",
        "/srv/hubexec.py",
    )
    a = st.admin()
    rules_file = OUT / "unified_pdp.rules.json"
    rules0, plugins0 = rules_file.read_text(), (SECRETS / "plugins.yaml").read_text()
    gw_id = None
    out: dict[str, Any] = {}
    try:
        for _ in range(30):
            r = a.post(
                "/v1/gateways",
                json={
                    "name": "hubexec-t-ext-test",
                    "url": f"http://{HUBEXEC_IP}:8000/mcp",
                    "transport": "STREAMABLEHTTP",
                    "teamId": S["team_id"],
                    "visibility": "team",
                },
            )
            if r.status_code in (200, 201):
                break
            time.sleep(1)
        out["register_gateway"] = {"status": r.status_code, "body": r.text[:200] if r.status_code >= 300 else ""}
        gw_id = r.json()["id"]
        tool = next(x for x in a.get("/v1/tools?include_inactive=true&limit=0").json() if x.get("gatewayId") == gw_id)
        out["tool"] = {k: tool.get(k) for k in ("name", "originalName", "integrationType", "enabled")}
        out["input_schema"] = tool.get("inputSchema")
        allow = {
            "id": "m11cf.hubexec",
            "roles": ["*"],
            "conditions": {"subject.email": S["agents"][C]["email"]},
            "actions": [f"tools.invoke.{tool['name']}"],
            "resource_types": ["tool"],
            "resource_ids": ["*"],
            "reason": "check 6",
        }
        stand = json.loads((SECRETS / "state.json").read_text())
        plugins = plugins_yaml(
            {t["id"]: n for n, t in stand["teams"].items()} | {S["team_id"]: "t-ext-test"},
            {t["gateway_id"]: "demo" for t in stand["teams"].values()}
            | {t["tg_gateway_id"]: "telegram" for t in stand["teams"].values()}
            | {S["gateway_id"]: "telegram", gw_id: "hub-executor"},
        )
        (SECRETS / "plugins.yaml").write_text(plugins)
        for label, rules in (
            ("stand rules + allow", [*json.loads(rules0), allow]),
            (
                "allow, without deny:agents-never-execute",
                [x for x in json.loads(rules0) if x["id"] != "deny:agents-never-execute"] + [allow],
            ),
        ):
            rules_file.write_text(json.dumps(rules, indent=1))
            sh("docker", "compose", "restart", "contextforge")
            base.wait_healthy()
            r = st.call(C, tool["name"], {"script": "print(1)"})
            out[label] = cut(r, 500)
    finally:
        rules_file.write_text(rules0)
        (SECRETS / "plugins.yaml").write_text(plugins0)
        if gw_id:
            a.delete(f"/v1/gateways/{gw_id}")
        sh("docker", "rm", "-f", HUBEXEC, check=False)
        sh("docker", "compose", "restart", "contextforge")
        base.wait_healthy()
    called = '"script_len": 8' in out.get("allow, without deny:agents-never-execute", "").replace('\\"', '"')
    verdict("6_hub_execute", out["register_gateway"]["status"] in (200, 201) and called, **out)


def main(which: str) -> int:
    steps = {"register": register, "schema": schema, "servers": servers, "limits": limits, "hubexec": hubexec}
    for name, fn in steps.items():
        if which in (name, "all"):
            fn()
    prev = json.loads((OUT / "evidence.json").read_text()) if (OUT / "evidence.json").exists() else {}
    (OUT / "evidence.json").write_text(json.dumps(prev | EVIDENCE, ensure_ascii=False, indent=1))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))
