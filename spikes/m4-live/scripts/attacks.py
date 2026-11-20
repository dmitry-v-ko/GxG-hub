"""Runs the ADR-0001 spike protocol against the live stand and writes evidence/results.json.

Every case sends a unique nonce in arguments.text, then looks up what the echo server
actually received (evidence/echo.jsonl) and runs the best verifier a connector can build on
Option A (verify.py). Nothing is simulated: if a call fails, the case records the failure.
"""

import hashlib
import hmac
import json
import time
import uuid

from cf import ROOT, env, http, load_state

S = load_state()
E = env()
LOG = ROOT / "evidence" / "echo.jsonl"
ECHO_DIRECT = "http://127.0.0.1:9100"
SECRET = E["IDENTITY_CLAIMS_SECRET"]
PFX = "x-forwarded-user-"
TEAM_IDS = {v["id"]: k for k, v in S["teams"].items()}
results = []


def tok(agent):
    return S["agents"][agent]["token"]


def verify(headers):
    """Best-effort Option A verifier: HMAC(id:email). Returns (accepted, identity|reason)."""
    sig = headers.get(PFX + "claims-signature")
    uid, email = headers.get(PFX + "id"), headers.get(PFX + "email", "")
    if not (sig and uid):
        return False, "no identity headers"
    want = hmac.new(SECRET.encode(), f"{uid}:{email}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig):
        return False, "bad signature"
    teams = [t for t in headers.get(PFX + "teams", "").split(",") if t]
    if len(teams) != 1:
        return False, f"ambiguous tenant: teams={teams}"
    # No timestamp/nonce/aud exists in the propagated claims, so freshness cannot be checked.
    return True, {"principal": uid, "tenant_id": TEAM_IDS.get(teams[0], teams[0])}


def seen(nonce, wait=3.0):
    end = time.time() + wait
    while time.time() < end:
        for line in reversed(LOG.read_text().splitlines()):
            e = json.loads(line)
            args = ((e.get("body") or {}).get("params") or {}).get("arguments") or {}
            if args.get("text") == nonce:
                return e
        time.sleep(0.2)
    return None


def rpc(token, name, args, meta=None, headers=None, endpoint="/rpc"):
    params = {"name": name, "arguments": args}
    if meta is not None:
        params["_meta"] = meta
    return http("POST", endpoint, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, token, headers)


def mcp(token, endpoint, name, args, meta=None, headers=None):
    """Streamable HTTP: initialize -> notifications/initialized -> tools/call."""
    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "m4-spike", "version": "0"},
        },
    }
    code, d, rh = http("POST", endpoint, init, token, headers)
    sid = {k.lower(): v for k, v in rh.items()}.get("mcp-session-id")
    h = dict(headers or {})
    if sid:
        h["Mcp-Session-Id"] = sid
    if code != 200:
        return code, d, rh
    http("POST", endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token, h)
    params = {"name": name, "arguments": args}
    if meta is not None:
        params["_meta"] = meta
    return http("POST", endpoint, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}, token, h)


def case(cid, desc, agent, fn, expect, **kw):
    nonce = f"{cid}-{uuid.uuid4().hex[:8]}"
    args = dict(kw.pop("args", {}), text=nonce)
    code, resp, _ = fn(args)
    e = seen(nonce)
    rec = {
        "id": cid,
        "desc": desc,
        "agent": agent,
        "expect": expect,
        "cf_status": code,
        "cf_error": (resp.get("error") if isinstance(resp, dict) else str(resp)[:300]),
        "delivered_to_echo": bool(e),
    }
    if e:
        h = e["headers"]
        rec["echo_path"] = e["path"]
        rec["echo_identity_headers"] = {k: v for k, v in h.items() if k.startswith(PFX)}
        rec["echo_meta"] = e["body"]["params"].get("_meta")
        rec["echo_arguments"] = e["body"]["params"].get("arguments")
        accepted, result = verify(h)
        rec["verifier"] = {"accepted": accepted, "result": result}
    results.append(rec)
    print(cid, code, "delivered" if e else "not-delivered", rec.get("verifier"))
    return rec


T = S["teams"]
vs = {k: f"/servers/{v['server_id']}/mcp" for k, v in T.items()}
alias = T["t-alpha"]["alias_tool"]
beta_tool = T["t-beta"]["direct_tool"]

# --- Call paths (criterion 4): every agent x {direct /rpc, direct global /mcp, alias /rpc, virtual server x2} ---
for agent, ag in S["agents"].items():
    t = T[ag["team"]]
    for cid, desc, fn in [
        (
            "direct-rpc",
            f"direct tool {t['direct_tool']} via /rpc",
            lambda a, t=t, g=agent: rpc(tok(g), t["direct_tool"], a),
        ),
        (
            "direct-mcp",
            "direct tool via global /mcp streamable HTTP",
            lambda a, t=t, g=agent: mcp(tok(g), "/mcp/", t["direct_tool"], a),
        ),
        ("alias-rpc", f"alias {t['alias_tool']} via /rpc", lambda a, t=t, g=agent: rpc(tok(g), t["alias_tool"], a)),
        (
            "vs-direct",
            "virtual server, non-aliased tool",
            lambda a, t=t, g=agent, u=vs[ag["team"]]: mcp(tok(g), u, t["direct_tool"], a),
        ),
        (
            "vs-alias",
            "virtual server, aliased tool",
            lambda a, t=t, g=agent, u=vs[ag["team"]]: mcp(tok(g), u, t["alias_tool"], a),
        ),
    ]:
        case(f"P-{cid}-{agent}", desc, agent, fn, "identity of caller")
case(
    "P-alias-origname-t-alpha-a1",
    f"alias: call by original name {T['t-alpha']['alias_original_name']}",
    "t-alpha-a1",
    lambda a: rpc(tok("t-alpha-a1"), T["t-alpha"]["alias_original_name"], a),
    "routes with identity, or not found",
)

# --- Forgery by the agent (criterion 1) ---
beta_id = T["t-beta"]["id"]
forged_h = {
    "X-Forwarded-User-Id": "t-beta-a1@example.com",
    "X-Forwarded-User-Email": "t-beta-a1@example.com",
    "X-Forwarded-User-Teams": beta_id,
    "X-Forwarded-User-Team-Id": beta_id,
    "X-Forwarded-User-Admin": "true",
    "X-Forwarded-User-Claims-Signature": "0" * 64,
}
forged_meta = {
    "user": {"id": "t-beta-a1@example.com", "email": "t-beta-a1@example.com", "teams": [beta_id], "is_admin": True}
}
forged_args = {"tenant_id": "t-beta", "agent_id": "t-beta-a1", "principal": "agent:t-beta:a1"}
for path, fn in [
    ("rpc", lambda a, **k: rpc(tok("t-alpha-a1"), alias, a, **k)),
    ("vs", lambda a, **k: mcp(tok("t-alpha-a1"), vs["t-alpha"], alias, a, **k)),
]:
    case(
        f"F-headers-{path}",
        f"agent sends forged X-Forwarded-User-* headers ({path})",
        "t-alpha-a1",
        lambda a, f=fn: f(a, headers=forged_h),
        "forged headers dropped/overwritten",
    )
    case(
        f"F-meta-{path}",
        f"agent sends forged _meta.user ({path})",
        "t-alpha-a1",
        lambda a, f=fn: f(a, meta=forged_meta),
        "forged _meta.user overwritten",
    )
    case(
        f"F-args-{path}",
        f"agent puts tenant_id/agent_id into arguments ({path})",
        "t-alpha-a1",
        lambda a, f=fn: f(dict(a, **forged_args)),
        "args are data, identity unchanged",
    )

# --- Cross-team (TEN-01, informational) ---
case(
    "X-rpc-beta-tool",
    "t-alpha-a1 calls t-beta tool by name via /rpc",
    "t-alpha-a1",
    lambda a: rpc(tok("t-alpha-a1"), beta_tool, a),
    "denied, not delivered",
)
case(
    "X-vs-beta",
    "t-alpha-a1 calls t-beta virtual server",
    "t-alpha-a1",
    lambda a: mcp(tok("t-alpha-a1"), vs["t-beta"], beta_tool, a),
    "denied, not delivered",
)
case(
    "X-rpc-beta-alias",
    "t-alpha-a1 calls t-beta alias by name via /rpc",
    "t-alpha-a1",
    lambda a: rpc(tok("t-alpha-a1"), T["t-beta"]["alias_tool"], a),
    "denied, not delivered",
)
case(
    "X-vs-mismatch",
    "t-alpha-a1 calls t-beta tool via t-alpha virtual server",
    "t-alpha-a1",
    lambda a: mcp(tok("t-alpha-a1"), vs["t-alpha"], beta_tool, a),
    "denied, not delivered",
)

# --- Bypass ContextForge: direct calls to the echo server (criterion 2) ---
captured = next(r for r in results if r["id"] == "P-direct-rpc-t-alpha-a1")["echo_identity_headers"]
captured_at = time.time()
case(
    "B-direct-noid",
    "direct call to echo, no identity",
    "attacker",
    lambda a: rpc(None, "echo", a, endpoint=ECHO_DIRECT + "/t-alpha/direct/mcp"),
    "verifier rejects",
)
case(
    "B-direct-forged",
    "direct call to echo, forged headers without secret",
    "attacker",
    lambda a: rpc(None, "echo", a, endpoint=ECHO_DIRECT + "/t-alpha/direct/mcp", headers=forged_h),
    "verifier rejects",
)
swapped = dict(captured, **{PFX + "teams": beta_id})
case(
    "B-direct-teamswap",
    "direct call to echo, captured signed headers of t-alpha-a1 with teams swapped to t-beta",
    "attacker",
    lambda a: rpc(None, "echo", a, endpoint=ECHO_DIRECT + "/t-beta/direct/mcp", headers=swapped),
    "verifier rejects (team must be signed)",
)

# --- Replay > 60 s (criterion 3) ---
wait = max(0, 61 - (time.time() - captured_at)) + 5
print(f"waiting {wait:.0f}s for replay window")
time.sleep(wait)
rec = case(
    "R-replay-direct-66s",
    "replay captured t-alpha-a1 identity headers directly to echo after >60 s",
    "attacker",
    lambda a: rpc(None, "echo", a, endpoint=ECHO_DIRECT + "/t-alpha/direct/mcp", headers=captured),
    "verifier rejects",
)
rec["replay_age_s"] = round(time.time() - captured_at, 1)
r2 = case(
    "R-fresh-vs-old-signature",
    "new legit call 66 s later: compare signature with the captured one",
    "t-alpha-a1",
    lambda a: rpc(tok("t-alpha-a1"), alias, a),
    "signature changes per request",
)
r2["signature_identical_to_captured"] = (
    r2.get("echo_identity_headers", {}).get(PFX + "claims-signature") == captured[PFX + "claims-signature"]
)

(ROOT / "evidence" / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True))
print("wrote evidence/results.json")
