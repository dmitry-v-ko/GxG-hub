"""ADR-0001 protocol against option C (GxG identity adapter plugin + connector-side JWS verifier).

The echo server verifies online (gxg_verify.py) and logs its verdict; this script only sends
requests and reads verdicts. Captured tokens for replay come from evidence/raw/jws.jsonl, i.e.
what an attacker with access to the upstream's traffic would see.

Usage: python3 scripts/attacks_c.py <label> [--paths-only]
"""

import base64
import json
import sys
import time
import uuid

import cf
from cf import ROOT, load_state, mcp, rpc

LABEL = sys.argv[1] if len(sys.argv) > 1 else "c"
PATHS_ONLY = "--paths-only" in sys.argv
S = load_state()
T = S["teams"]
LOG = ROOT / "evidence" / "echo-c.jsonl"
RAW = ROOT / "evidence" / "raw" / "jws.jsonl"
ECHO = "http://127.0.0.1:9100"
H = "X-GXG-Identity"
results = []


def tok(agent):
    return S["agents"][agent]["token"]


def find(path, nonce, wait=3.0, after=0):
    end = time.time() + wait
    while time.time() < end:
        for line in reversed(path.read_text().splitlines()[after:]):
            e = json.loads(line)
            args = e.get("arguments") if "values" in e else ((e.get("body") or {}).get("params") or {}).get("arguments")
            if (args or {}).get("text") == nonce:
                return e
        time.sleep(0.2)
    return None


def captured_token(nonce):
    e = find(RAW, nonce)
    return e["values"][0] if e else None


def case(cid, desc, agent, fn, expect, args=None, match=None):
    """match: nonce to look up when the request re-sends earlier arguments (replays)."""
    nonce = f"{LABEL}-{cid}-{uuid.uuid4().hex[:8]}"
    a = dict(args or {}, text=nonce)
    before = len(LOG.read_text().splitlines())
    code, resp, _ = fn(a)
    e = find(LOG, match or nonce, after=before)
    rec = {
        "id": cid,
        "desc": desc,
        "agent": agent,
        "expect": expect,
        "cf_status": code,
        "nonce": nonce,
        "cf_error": resp.get("error") if isinstance(resp, dict) else str(resp)[:300],
        "delivered_to_echo": bool(e),
    }
    if e:
        rec["echo_path"] = e["path"]
        rec["echo_peer"] = e["peer"]
        rec["identity_headers_seen"] = e["headers"].get("x-gxg-identity")
        rec["forwarded_user_headers_seen"] = {k: v for k, v in e["headers"].items() if k.startswith("x-forwarded-user")}
        rec["echo_meta"] = e["body"]["params"].get("_meta")
        rec["echo_arguments"] = e["body"]["params"].get("arguments")
        rec["verdict"] = e.get("gxg_verdict")
    v = rec.get("verdict") or {}
    rec["accepted_identity"] = v.get("result") if v.get("accepted") else None
    results.append(rec)
    print(cid, code, "delivered" if e else "not-delivered", v.get("accepted"), str(v.get("result"))[:110])
    return rec


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()


def unb64(s):
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


vs = {k: f"/servers/{v['server_id']}/mcp" for k, v in T.items()}

# --- criterion 4: call paths ---
for agent, ag in S["agents"].items():
    t, u = T[ag["team"]], vs[ag["team"]]
    for cid, desc, fn in [
        ("direct-rpc", "direct tool via /rpc", lambda a, t=t, g=agent: rpc(tok(g), t["direct_tool"], a)),
        (
            "direct-mcp",
            "direct tool via global /mcp",
            lambda a, t=t, g=agent: mcp(tok(g), "/mcp/", t["direct_tool"], a),
        ),
        ("alias-rpc", "alias via /rpc", lambda a, t=t, g=agent: rpc(tok(g), t["alias_tool"], a)),
        ("vs-direct", "virtual server, plain tool", lambda a, t=t, g=agent, u=u: mcp(tok(g), u, t["direct_tool"], a)),
        ("vs-alias", "virtual server, aliased tool", lambda a, t=t, g=agent, u=u: mcp(tok(g), u, t["alias_tool"], a)),
    ]:
        case(f"P-{cid}-{agent}", desc, agent, fn, "accepted, sub/tenant = caller")

if not PATHS_ONLY:
    alias, alpha = T["t-alpha"]["alias_tool"], T["t-alpha"]
    beta_id = T["t-beta"]["id"]
    beta_tok = captured_token(next(r["nonce"] for r in results if r["id"] == "P-direct-rpc-t-beta-a1"))

    # --- criterion 1: agent-controlled inputs ---
    forged_fwd = {"X-Forwarded-User-Id": "t-beta-a1@example.com", "X-Forwarded-User-Teams": beta_id}
    forged_meta = {"user": {"id": "t-beta-a1@example.com", "teams": [beta_id]}, "gxg_identity": beta_tok}
    for path, fn in [
        ("rpc", lambda a, **k: rpc(tok("t-alpha-a1"), alias, a, **k)),
        ("vs", lambda a, **k: mcp(tok("t-alpha-a1"), vs["t-alpha"], alias, a, **k)),
    ]:
        case(
            f"F-own-jws-{path}",
            f"agent sends a valid captured {H} of t-beta-a1 ({path})",
            "t-alpha-a1",
            lambda a, f=fn: f(a, headers={H: beta_tok}),
            "dropped; identity = t-alpha-a1",
        )
        case(
            f"F-garbage-jws-{path}",
            f"agent sends garbage {H} ({path})",
            "t-alpha-a1",
            lambda a, f=fn: f(a, headers={H: "x.y.z"}),
            "dropped; identity = t-alpha-a1",
        )
        case(
            f"F-fwd-headers-{path}",
            f"agent sends X-Forwarded-User-* ({path})",
            "t-alpha-a1",
            lambda a, f=fn: f(a, headers=forged_fwd),
            "ignored; identity = t-alpha-a1",
        )
        case(
            f"F-meta-{path}",
            f"agent sends forged _meta.user and _meta.gxg_identity ({path})",
            "t-alpha-a1",
            lambda a, f=fn: f(a, meta=forged_meta),
            "ignored; identity = t-alpha-a1",
        )
        case(
            f"F-args-{path}",
            f"agent puts tenant_id/agent_id into arguments ({path})",
            "t-alpha-a1",
            lambda a, f=fn: f(a),
            "args are data; identity = t-alpha-a1",
            args={"tenant_id": beta_id, "agent_id": "t-beta-a1", "sub": "t-beta-a1@example.com"},
        )

    # --- cross-team (informational) ---
    case(
        "X-rpc-beta-tool",
        "t-alpha-a1 calls t-beta tool via /rpc",
        "t-alpha-a1",
        lambda a: rpc(tok("t-alpha-a1"), T["t-beta"]["direct_tool"], a),
        "not delivered",
    )
    case(
        "X-vs-beta",
        "t-alpha-a1 calls t-beta virtual server",
        "t-alpha-a1",
        lambda a: mcp(tok("t-alpha-a1"), vs["t-beta"], T["t-beta"]["direct_tool"], a),
        "not delivered",
    )

    # --- criterion 2: bypass ContextForge (direct to echo) ---
    legit = case(
        "C-legit-capture",
        "legit t-alpha-a1 call whose token the attacker captures",
        "t-alpha-a1",
        lambda a: rpc(tok("t-alpha-a1"), alpha["direct_tool"], a),
        "accepted",
    )
    cap, cap_at = captured_token(legit["nonce"]), time.time()
    h, p, sig = cap.split(".")
    claims = unb64(p)
    d_alpha, d_beta = ECHO + "/t-alpha/direct/mcp", ECHO + "/t-beta/direct/mcp"
    case("B-noid", "direct call, no identity", "attacker", lambda a: rpc(None, "echo", a, endpoint=d_alpha), "rejected")
    case(
        "B-fwd-only",
        "direct call with X-Forwarded-User-* only",
        "attacker",
        lambda a: rpc(None, "echo", a, endpoint=d_alpha, headers=forged_fwd),
        "rejected",
    )
    case(
        "B-alg-none",
        "direct call, unsigned token (alg=none) with t-alpha-a1 claims",
        "attacker",
        lambda a: rpc(
            None, "echo", a, endpoint=d_alpha, headers={H: b64({"alg": "none", "typ": "gxg-id+jwt"}) + "." + p + "."}
        ),
        "rejected",
    )
    case(
        "B-tenant-swap",
        "captured token with tenant_id changed to t-beta (signature kept)",
        "attacker",
        lambda a: rpc(
            None, "echo", a, endpoint=d_alpha, headers={H: f"{h}.{b64(dict(claims, tenant_id=beta_id))}.{sig}"}
        ),
        "rejected: signature",
    )
    case(
        "B-other-aud",
        "captured token sent to the t-beta connector URL",
        "attacker",
        lambda a: rpc(None, "echo", a, endpoint=d_beta, headers={H: cap}),
        "rejected: audience",
    )
    case(
        "B-other-args",
        "captured token with different arguments",
        "attacker",
        lambda a: rpc(None, "echo", a, endpoint=d_alpha, headers={H: cap}),
        "rejected: ahs or jti",
    )
    # --- criterion 3: replay ---
    case(
        "R-replay-fast",
        f"replay captured token with the same arguments after {time.time() - cap_at:.0f}s",
        "attacker",
        lambda a: rpc(None, "echo", dict(legit["echo_arguments"]), endpoint=d_alpha, headers={H: cap}),
        "rejected: replayed jti",
        match=legit["nonce"],
    )
    wait = max(0, 61 - (time.time() - cap_at)) + 5
    print(f"waiting {wait:.0f}s")
    time.sleep(wait)
    r = case(
        "R-replay-66s",
        "replay captured token, same arguments, >60 s later",
        "attacker",
        lambda a: rpc(None, "echo", dict(legit["echo_arguments"]), endpoint=d_alpha, headers={H: cap}),
        "rejected: expired",
        match=legit["nonce"],
    )
    r["replay_age_s"] = round(time.time() - cap_at, 1)
    after = case(
        "R-fresh-after",
        "new legit call after the wait",
        "t-alpha-a1",
        lambda a: rpc(tok("t-alpha-a1"), alpha["direct_tool"], a),
        "accepted, new jti",
    )
    after["jti_differs_from_captured"] = (after.get("accepted_identity") or {}).get("jti") != claims["jti"]

out = ROOT / "evidence" / f"results-{LABEL}.json"
out.write_text(json.dumps(results, indent=2, sort_keys=True))
print("wrote", out.relative_to(cf.ROOT))
