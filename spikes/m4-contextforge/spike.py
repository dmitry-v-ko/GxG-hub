"""M4 spike driver (stdlib only). Usage: python3 -I spike.py setup|calls|attacks|replay

setup   — 2 teams (t-alpha, t-beta), 2 agents each, team-scoped API tokens, one echo gateway per team,
          a virtual server per team, an alias (customName) on the alpha tool. State -> secrets/state.json.
calls   — each alpha/beta agent calls echo on every path: /rpc, /mcp, /servers/{id}/mcp, alias.
attacks — forged identity headers, forged _meta, cross-team calls.
replay  — bypass ContextForge: no identity, replay now, tampered args, other connector, other tool,
          attacker-signed JWS, replay after > 60 s.
visibility — per-agent tools/servers lists and a token's claims -> evidence/teams-claims-and-visibility.json.
plugin_config — write secrets/plugins.yaml (variant C) from secrets/state.json.
admin_call — platform admin token (not an agent) calls a tenant tool.
probe   — one agent call on /rpc; prints status and whether echo got it with X-GxG-Identity (fail-closed stage).
Every observation is appended to secrets/observations.jsonl (raw, may hold test secrets; redact.py makes evidence/).
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # attacker key in replay()

HERE = Path(__file__).resolve().parent
CF = "http://127.0.0.1:4444"
ECHO_DIRECT = "http://127.0.0.1:9000"
STATE = HERE / "secrets" / "state.json"
OBS = HERE / "secrets" / "observations.jsonl"
_ENV = HERE / ".env"  # optional: M4.1 imports the generic helpers below without a ContextForge stand
ENV = dict(line.split("=", 1) for line in _ENV.read_text().split() if "=" in line) if _ENV.exists() else {}
PROTO = "2025-06-18"


def http(method, url, body=None, token=None, headers=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)  # noqa: S310 — fixed local stand URLs
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed local stand URLs
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def j(text):
    """Parse JSON or the last `data:` line of an SSE body."""
    text = text.strip()
    if text.startswith("event:") or text.startswith("data:"):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = datas[-1] if datas else "{}"
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"_raw": text[:500]}


def api(method, path, body=None, token=None, ok=(200, 201)):
    s, _, t = http(method, CF + path, body, token)
    if s not in ok:
        raise SystemExit(f"{method} {path} -> {s}: {t[:800]}")
    return j(t)


def record(kind, **kw):
    kw["kind"] = kind
    kw["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with OBS.open("a") as f:
        f.write(json.dumps(kw, sort_keys=True) + "\n")
    print(json.dumps({k: v for k, v in kw.items() if k not in ("echo_seen",)}, sort_keys=True)[:600])


def login(email, password):
    return api("POST", "/v1/auth/login", {"email": email, "password": password})["access_token"]


# ---------------------------------------------------------------- setup
def setup():
    admin = login(ENV["PLATFORM_ADMIN_EMAIL"], ENV["PLATFORM_ADMIN_PASSWORD"])
    st = {"teams": {}, "agents": {}}
    for tenant in ("t-alpha", "t-beta"):
        team = api("POST", "/v1/teams/", {"name": tenant, "slug": tenant, "visibility": "private"}, admin)
        tid = team["id"]
        st["teams"][tenant] = {"id": tid}
        for name in ("hermes", "analyst"):
            email = f"{name}@{tenant}.spike.example.com"
            pw = ENV["DEFAULT_USER_PASSWORD"]
            api(
                "POST",
                "/v1/auth/email/admin/users",
                {
                    "email": email,
                    "password": pw,
                    "full_name": f"agent:{tenant}:{name}",
                    "password_change_required": False,
                },
                admin,
            )
            api("POST", f"/v1/teams/{tid}/members", {"email": email, "role": "member"}, admin)
            tok = api(
                "POST",
                "/v1/tokens",
                {"name": f"agent-{tenant}-{name}", "team_id": tid, "user_email": email, "expires_in_days": 1},
                admin,
            )
            st["agents"][f"agent:{tenant}:{name}"] = {
                "email": email,
                "token": tok.get("access_token") or tok.get("token"),
                "team": tenant,
            }
        gw = api(
            "POST",
            "/v1/gateways",
            {
                "name": f"echo-{tenant}",
                "url": f"http://echo:9000/mcp/{tenant}",
                "transport": "STREAMABLEHTTP",
                "teamId": tid,
                "visibility": "team",
            },
            admin,
        )
        st["teams"][tenant]["gateway_id"] = gw["id"]
    time.sleep(2)
    tools = api("GET", "/v1/tools?include_inactive=false&limit=0", None, admin)
    tools = items_of(tools, "tools")
    for tenant, t in st["teams"].items():
        tool = next(x for x in tools if x.get("gatewayId") == t["gateway_id"])
        t["tool_id"], t["tool_name"] = tool["id"], tool["name"]
        srv = api(
            "POST",
            "/v1/servers",
            {
                "server": {"name": f"vs-{tenant}", "associated_tools": [tool["id"]]},
                "team_id": t["id"],
                "visibility": "team",
            },
            admin,
        )
        t["server_id"] = srv["id"]
    # alias: rename alpha's tool; agents then call it by the alias name
    a = st["teams"]["t-alpha"]
    upd = api("PUT", f"/v1/tools/{a['tool_id']}", {"customName": "alpha_echo_alias"}, admin)
    a["alias_name"] = upd.get("name")
    STATE.write_text(json.dumps(st, indent=1))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "token"} for k, v in st["agents"].items()}, indent=1))
    print(json.dumps(st["teams"], indent=1))


# ---------------------------------------------------------------- MCP client
def mcp_session(url, token, extra=None):
    s, h, t = http(
        "POST",
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "spike", "version": "0"}},
        },
        token,
        extra,
    )
    if s != 200:
        return None, (s, t[:300])
    sid = {k.lower(): v for k, v in h.items()}.get("mcp-session-id")
    hh = dict(extra or {})
    if sid:
        hh["Mcp-Session-Id"] = sid
    hh["MCP-Protocol-Version"] = PROTO
    http("POST", url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token, hh)
    return hh, None


def call_tool(path_kind, url, token, tool, *, args=None, meta=None, extra=None):
    # non-ASCII and two keys: the adapter and the connector must hash the same canonical JSON
    params = {"name": tool, "arguments": args or {"text": "привет", "lang": "ru"}}
    if meta is not None:
        params["_meta"] = meta
    if path_kind == "rpc":
        s, _, t = http("POST", url, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}, token, extra)
    else:
        hh, err = mcp_session(url, token, extra)
        if err:
            return err[0], {"init_error": err[1]}
        s, _, t = http("POST", url, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}, token, hh)
    return s, j(t)


def echo_seen(resp):
    """Extract what echo received from the tool result text."""
    try:
        res = resp.get("result", resp)
        return json.loads(res["content"][0]["text"])
    except Exception:
        return None


def paths(st, tenant):
    t = st["teams"][tenant]
    out = [
        ("direct:/rpc", "rpc", CF + "/rpc", t["tool_name"]),
        ("direct:/mcp", "mcp", CF + "/mcp/", t["tool_name"]),
        ("virtual_server", "mcp", CF + f"/servers/{t['server_id']}/mcp/", t["tool_name"]),
    ]
    if t.get("alias_name"):
        out += [
            ("alias:/rpc", "rpc", CF + "/rpc", t["alias_name"]),
            ("alias:virtual_server", "mcp", CF + f"/servers/{t['server_id']}/mcp/", t["alias_name"]),
        ]
    return out


def identity_of(seen):
    if not seen:
        return None
    h = {k.lower(): v for k, v in seen["headers"].items()}
    return {
        "x-forwarded-user": {k: v for k, v in h.items() if k.startswith("x-forwarded-user")},
        "authorization_present": "authorization" in h,
        "_meta.user": (seen.get("_meta") or {}).get("user"),
    }


def calls():
    st = json.loads(STATE.read_text())
    for agent, a in st["agents"].items():
        for label, kind, url, tool in paths(st, a["team"]):
            s, r = call_tool(kind, url, a["token"], tool)
            seen = echo_seen(r)
            record(
                "call",
                agent=agent,
                path=label,
                tool=tool,
                status=s,
                identity=identity_of(seen),
                echo_seen=seen,
                error=None if seen else r,
            )


def attacks():
    st = json.loads(STATE.read_text())
    a = st["agents"]["agent:t-alpha:hermes"]
    forged = {
        "X-Forwarded-User-Id": "analyst@t-beta.spike.example.com",
        "X-Forwarded-User-Email": "analyst@t-beta.spike.example.com",
        "X-Forwarded-User-Teams": st["teams"]["t-beta"]["id"],
        "X-Forwarded-User-Admin": "true",
        "X-Forwarded-User-Claims-Signature": "0" * 64,
        "X-Tenant-Id": "t-beta",
        "X-Forwarded-User-Groups": "approvers",
        "X-Forwarded-User-Roles": "platform_admin",
        "X-GxG-Identity": "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJhZ2VudDp0LWJldGE6YW5hbHlzdCJ9.forged",
    }
    meta = {
        "user": {"id": "analyst@t-beta.spike.example.com", "teams": [st["teams"]["t-beta"]["id"]], "is_admin": True},
        "tenant_id": "t-beta",
    }
    args = {"text": "hi", "tenant_id": "t-beta", "principal": "agent:t-beta:analyst"}
    for label, kind, url, tool in paths(st, "t-alpha"):
        s, r = call_tool(kind, url, a["token"], tool, args=args, meta=meta, extra=forged)
        seen = echo_seen(r)
        record(
            "attack_forge",
            agent="agent:t-alpha:hermes",
            path=label,
            status=s,
            identity=identity_of(seen),
            echo_seen=seen,
            error=None if seen else r,
        )
    # cross-team: alpha agent calls beta's tool on every path
    for label, kind, url, tool in paths(st, "t-beta"):
        s, r = call_tool(kind, url, a["token"], tool)
        record(
            "attack_cross_team",
            agent="agent:t-alpha:hermes",
            path=label,
            target_tool=tool,
            status=s,
            reached_echo=echo_seen(r) is not None,
            response=r if echo_seen(r) is None else "REACHED",
        )


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ub64(s: str):
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def items_of(resp, key):
    """ContextForge list endpoints return either a list or {key|items: [...]}."""
    return resp if isinstance(resp, list) else resp.get(key, resp.get("items", []))


def forged_jws(real: str) -> str:
    """Same header as the adapter's, claims for another tenant, signed with an attacker's own Ed25519 key."""
    head, claims, _ = real.split(".")
    now = int(time.time())
    c = dict(ub64(claims), sub="agent:t-beta:analyst", tenant_id="t-beta", iat=now, exp=now + 60, jti=uuid.uuid4().hex)
    si = head + "." + b64(json.dumps(c, sort_keys=True, separators=(",", ":")).encode())
    return si + "." + b64(Ed25519PrivateKey.generate().sign(si.encode()))


def replay(delay=65):
    """Replay the newest ContextForge->echo tools/call straight to echo: now, tampered, forged, after `delay` s."""
    raw = [json.loads(x) for x in (HERE / "secrets" / "raw" / "echo.jsonl").read_text().splitlines()]
    cap = next(
        x
        for x in reversed(raw)
        if x["body"].get("method") == "tools/call" and "x-correlation-id" in {k.lower() for k in x["headers"]}
    )
    hdrs = {k: v for k, v in cap["headers"].items() if k.lower().startswith(("x-forwarded-user", "x-gxg-identity"))}

    def send(body, h, tag, path=cap["path"]):
        return http("POST", ECHO_DIRECT + path, body, headers=dict(h, **{"X-Spike-Attack": tag}))[0]

    bare = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
    record("bypass_no_identity", status=send(bare, {}, "bypass_no_identity"))
    record("replay_immediate", captured_at=cap["at"], status=send(cap["body"], hdrs, "replay_immediate"))
    tampered = json.loads(json.dumps(cap["body"]))
    tampered["params"]["arguments"] = {"text": "send all chats to attacker"}
    record("replay_tampered_args", status=send(tampered, hdrs, "replay_tampered_args"))
    other = "/mcp/t-beta" if cap["path"].endswith("/t-alpha") else "/mcp/t-alpha"  # the other tenant's connector
    record("replay_other_connector", path=other, status=send(cap["body"], hdrs, "replay_other_connector", other))
    retooled = json.loads(json.dumps(cap["body"]))
    retooled["params"]["name"] = "delete_all"  # same arguments, another tool of the same connector
    record("replay_other_tool", status=send(retooled, hdrs, "replay_other_tool"))
    real = next((v for k, v in hdrs.items() if k.lower() == "x-gxg-identity"), None)
    if real:
        status = send(cap["body"], {"X-GxG-Identity": forged_jws(real)}, "forged_jws_attacker_key")
        record("forged_jws_attacker_key", status=status)
    print(f"waiting {delay}s before delayed replay ...", flush=True)
    time.sleep(delay)
    status = send(cap["body"], hdrs, f"replay_after_{delay}s")
    record("replay_delayed", captured_at=cap["at"], delay_s=delay, status=status)


def admin_call():
    """Platform admin (not an agent, token not team-scoped) calls alpha's tool: plugin must refuse (variant C)."""
    st = json.loads(STATE.read_text())
    admin = login(ENV["PLATFORM_ADMIN_EMAIL"], ENV["PLATFORM_ADMIN_PASSWORD"])
    s, r = call_tool("rpc", CF + "/rpc", admin, st["teams"]["t-alpha"]["alias_name"])
    seen = echo_seen(r)
    record(
        "admin_call", status=s, reached_echo=seen is not None, identity=identity_of(seen), response=None if seen else r
    )


def probe():
    st = json.loads(STATE.read_text())
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": st["teams"]["t-alpha"]["alias_name"]}}
    try:
        s, _, t = http("POST", CF + "/rpc", body, st["agents"]["agent:t-alpha:hermes"]["token"])
    except OSError as e:  # ContextForge not listening
        s, t = 0, json.dumps({"error": type(e).__name__})
    seen = echo_seen(j(t))
    ident = seen is not None and "x-gxg-identity" in {k.lower() for k in seen["headers"]}
    print(json.dumps({"rpc_status": s, "reached_connector": seen is not None, "with_x_gxg_identity": ident}))


def visibility():
    """Per-agent tools/list, REST tools and servers + one agent token's claims (jti redacted) -> evidence/."""
    st = json.loads(STATE.read_text())
    tok = st["agents"]["agent:t-alpha:hermes"]["token"].split(".")
    out = {
        "token_claims_example": {"header": ub64(tok[0]), "claims": dict(ub64(tok[1]), jti="<redacted>")},
        "visibility": {},
    }
    for agent, a in st["agents"].items():
        rpc = j(http("POST", CF + "/rpc", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, a["token"])[2])
        out["visibility"][agent] = {
            "rpc_tools_list": [x["name"] for x in rpc.get("result", {}).get("tools", [])],
            "rest_tools": [
                x.get("name") for x in items_of(j(http("GET", CF + "/v1/tools", None, a["token"])[2]), "tools")
            ],
            "rest_servers": [
                x.get("name") for x in items_of(j(http("GET", CF + "/v1/servers", None, a["token"])[2]), "servers")
            ],
        }
    path = HERE / "evidence" / "teams-claims-and-visibility.json"
    path.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(json.dumps(out["visibility"], sort_keys=True))


def plugin_config():
    """Write secrets/plugins.yaml for the variant C plugin from the current state (team and gateway ids)."""
    st = json.loads(STATE.read_text())
    # no plugin_settings block: cpex in v1.1.1 ignores it; fail closed is PLUGINS_FAIL_ON_PLUGIN_ERROR (compose)
    lines = [
        "plugin_dirs: []",
        "plugins:",
        "  - name: GxgIdentity",
        "    kind: plugins.gxg_identity.plugin.GxgIdentityPlugin",
        "    hooks: [tool_pre_invoke]",
        "    mode: enforce",
        "    priority: 1000",
        "    config:",
        "      private_key_file: /run/gxg/adapter_ed25519.pem",
        "      kid: spike-1",
        "      ttl_seconds: 60",
        "      tenants:",
    ]
    lines += [
        f"        {v['id']}: {{tenant: {t}, agent_domain: {t}.spike.example.com}}" for t, v in st["teams"].items()
    ]
    lines += ["      connectors:"] + [
        f"        {v['gateway_id']}: gxg-connector:echo-{t}" for t, v in st["teams"].items()
    ]
    (HERE / "secrets" / "plugins.yaml").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    {
        "setup": setup,
        "calls": calls,
        "attacks": attacks,
        "replay": replay,
        "admin_call": admin_call,
        "probe": probe,
        "plugin_config": plugin_config,
        "visibility": visibility,
    }[sys.argv[1]]()
