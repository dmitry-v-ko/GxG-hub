"""M4.1: the M4 calls and attacks against any gateway candidate. Usage: python3 -I attacks.py <candidate dir>

Reuses the generic helpers of spikes/m4-contextforge/spike.py (http, MCP session, tools/call, forged JWS) and the
echo-MCP + verifier of M4 (echo runs with the adapter public key, so every tools/call gets a variant C verdict).
The candidate dir holds secrets/stand.json (written by its run.sh, git-ignored):
  {"paths": {"t-alpha": [{"label", "url", "tool"}], "t-beta": [...]},
   "tokens": {"agent:t-alpha:hermes": ..., "agent:t-alpha:analyst": ..., "agent:t-beta:...": ..., "admin": ... | null}}
Observations -> <dir>/secrets/observations.jsonl; summary -> <dir>/secrets/summary.json.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

M4 = Path(__file__).resolve().parent.parent / "m4-contextforge"
sys.path.insert(0, str(M4))  # python -I drops the script dir
import check_verdicts  # noqa: E402 — M4 attack tags and expected rejections
import spike  # noqa: E402 — M4 generic helpers

DIR = Path(sys.argv[1]).resolve()
ECHO = "http://127.0.0.1:9000"
RAW = DIR / "secrets/raw/echo.jsonl"
spike.OBS = DIR / "secrets/observations.jsonl"
ST = json.loads((DIR / "secrets/stand.json").read_text())
AGENTS = [a for a in ST["tokens"] if a.startswith("agent:")]
FORGED = {  # the same forged identity as M4 attacks(): t-alpha agent claims to be t-beta / admin
    "X-Forwarded-User-Id": "analyst@t-beta.spike.example.com",
    "X-Forwarded-User-Email": "analyst@t-beta.spike.example.com",
    "X-Forwarded-User-Teams": "t-beta",
    "X-Forwarded-User-Admin": "true",
    "X-Tenant-Id": "t-beta",
    "X-Forwarded-User-Roles": "platform_admin",
    "X-GxG-Identity": "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJhZ2VudDp0LWJldGE6YW5hbHlzdCJ9.forged",
}
META_KEY = "gxg.dev/identity"  # where the ToolHive webhook variant puts the JWS (toolhive/webhook.py)
META = {
    "user": {"id": "analyst@t-beta.spike.example.com", "teams": ["t-beta"], "is_admin": True},
    "tenant_id": "t-beta",
    META_KEY: "forged-by-client",  # not JWT-shaped: Docker MCP Gateway's secret scan blocks JWT-looking results
}
ARGS = {"text": "hi", "tenant_id": "t-beta", "principal": "agent:t-beta:analyst"}
HOP = {"host", "content-length", "connection", "accept-encoding", "user-agent", "x-spike-attack"}


def seen_identity(seen):
    """What reached the connector: identity-looking headers (values cut), _meta, the verifier's verdict."""
    if not seen:
        return None
    h = {k.lower(): v for k, v in seen["headers"].items()}
    keys = ("authorization", "x-forwarded", "x-tenant", "x-gxg", "x-user", "x-mcp", "x-toolhive", "x-docker")
    return {
        "identity_headers": {k: v[:24] + "…" for k, v in h.items() if k.startswith(keys)},
        "all_header_names": sorted(h),
        "_meta": seen.get("_meta"),
        "arguments": seen.get("arguments"),
        "verdict": seen.get("verdict"),
    }


def call(kind, agent, p, token, pk="mcp", **kw):
    if token:  # Docker MCP Gateway: one shared token per instance, whoever the caller is
        token = ST.get("token_for_url", {}).get(p["url"], token)
    s, r = spike.call_tool(pk, p["url"], token, p["tool"], **kw)
    seen = spike.echo_seen(r)
    spike.record(
        kind,
        agent=agent,
        path=p["label"],
        tool=p["tool"],
        status=s,
        reached_echo=seen is not None,
        seen=seen_identity(seen),
        response=None if seen else r,
    )


def run_calls():
    for a in AGENTS:
        for p in ST["paths"][a.split(":")[1]]:
            call("call", a, p, ST["tokens"][a])


def run_attacks():
    alpha, beta = "agent:t-alpha:hermes", "agent:t-beta:analyst"
    for p in ST["paths"]["t-alpha"]:
        call("attack_forge", alpha, p, ST["tokens"][alpha], args=ARGS, meta=META, extra=FORGED)
        call("attack_no_token", None, p, None)
        if ST["tokens"].get("admin"):
            call("admin_call", "admin", p, ST["tokens"]["admin"])
    for p in ST["paths"]["t-beta"]:  # cross-tenant: alpha's token on beta's endpoint / tool
        call("attack_cross_tenant", alpha, p, ST["tokens"][alpha])
    for p in ST.get("hidden", []):  # a tool filtered out of tools/list, called by name by an agent the stand names
        who = p.get("agent", beta)
        call("attack_hidden_tool", who, p, ST["tokens"][who])


def run_midsession():
    """Sessions opened while the stand is healthy; tools/call in them, then ST["stop_mid"] (e.g. stop the webhook),
    then tools/call again in the same sessions and in fresh ones."""
    tok = ST["tokens"]
    held = [(a, p, spike.mcp_session(p["url"], tok[a])[0]) for a in AGENTS for p in ST["paths"][a.split(":")[1]]]
    for phase in ("before_stop", "after_stop"):
        if phase == "after_stop":
            subprocess.run(ST["stop_mid"], check=True)  # noqa: S603 — fixed command from our run.sh
            spike.record("stopped_mid_session", cmd=ST["stop_mid"])
        for a, p, hh in held:  # "rpc" = tools/call with the held session headers, no new initialize
            call(f"call_same_session_{phase}", a, p, ST["tokens"][a], pk="rpc", extra=hh)
    run_calls()


def run_replay(delay=65):
    """M4 replay(): the newest gateway->echo tools/call replayed straight to echo, with every header it carried."""
    raw = [json.loads(x) for x in RAW.read_text().splitlines() if x.strip()]
    cap = next(
        x
        for x in reversed(raw)
        if x["body"].get("method") == "tools/call" and "x-spike-attack" not in {k.lower() for k in x["headers"]}
    )
    hdrs = {k: v for k, v in cap["headers"].items() if k.lower() not in HOP}

    def send(body, h, tag, path=cap["path"]):
        return spike.http("POST", ECHO + path, body, headers=dict(h, **{"X-Spike-Attack": tag}))[0]

    bare = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}}
    spike.record("bypass_no_identity", status=send(bare, {}, "bypass_no_identity"))
    spike.record("replay_immediate", status=send(cap["body"], hdrs, "replay_immediate"), replayed_headers=sorted(hdrs))
    tampered = json.loads(json.dumps(cap["body"]))
    tampered["params"]["arguments"] = {"text": "send all chats to attacker"}
    spike.record("replay_tampered_args", status=send(tampered, hdrs, "replay_tampered_args"))
    other = "/mcp/t-beta" if cap["path"].endswith("/t-alpha") else "/mcp/t-alpha"
    spike.record("replay_other_connector", status=send(cap["body"], hdrs, "replay_other_connector", other))
    retooled = json.loads(json.dumps(cap["body"]))
    retooled["params"]["name"] = "delete_all"
    spike.record("replay_other_tool", status=send(retooled, hdrs, "replay_other_tool"))
    meta = cap["body"]["params"].get("_meta") or {}
    accepted = "accepted" in (cap.get("verdict") or {})  # a client-forged _meta value is not "the real one"
    real = (meta.get(META_KEY) if accepted else None) or next(
        (v for k, v in hdrs.items() if k.lower() == "x-gxg-identity"), None
    )
    if real:  # forged JWS in the same place the real one came: _meta (webhook variant) or the header
        forged, h = json.loads(json.dumps(cap["body"])), {"X-GxG-Identity": spike.forged_jws(real)}
        if accepted and META_KEY in meta:
            forged["params"]["_meta"][META_KEY] = h.pop("X-GxG-Identity")
        spike.record("forged_jws_attacker_key", status=send(forged, h, "forged_jws_attacker_key"))
    time.sleep(delay)
    spike.record("replay_delayed", delay_s=delay, status=send(cap["body"], hdrs, f"replay_after_{delay}s"))


def summary():
    """Per observation kind: reached echo? which identity headers arrived? verifier verdicts of direct attacks."""
    obs = [json.loads(x) for x in spike.OBS.read_text().splitlines() if x.strip()]
    raw = [json.loads(x) for x in RAW.read_text().splitlines() if x.strip()]
    direct = {}
    for r in raw:
        tag = {k.lower(): v for k, v in r["headers"].items()}.get("x-spike-attack")
        if tag and r["body"].get("method") == "tools/call":
            direct[tag] = r.get("verdict")
    rows = [
        {k: o.get(k) for k in ("kind", "agent", "path", "tool", "status", "reached_echo")}
        | {
            "identity_headers": (o.get("seen") or {}).get("identity_headers"),
            "verdict": (o.get("seen") or {}).get("verdict"),
            "forged_meta_reached": ((o.get("seen") or {}).get("_meta") or {}).get("tenant_id") == "t-beta",
        }
        for o in obs
        if "path" in o
    ]
    expected = dict(check_verdicts.EXPECTED)
    return {"gateway_calls": rows, "direct_to_connector": direct, "m4_expected_with_variant_c": expected}


if __name__ == "__main__":
    for step in sys.argv[2:] or ["calls", "attacks", "replay"]:
        {"calls": run_calls, "attacks": run_attacks, "replay": run_replay, "midsession": run_midsession}[step]()
    (DIR / "secrets/summary.json").write_text(json.dumps(summary(), ensure_ascii=False, indent=1, sort_keys=True))
