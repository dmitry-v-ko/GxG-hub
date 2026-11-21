"""Asserts the variant C verdicts that echo recorded on arrival; run_spike.sh fails on any FAIL line.

Usage: python3 -I check_verdicts.py <variant> [--attacks]
Writes evidence/variant-<variant>.verify_jws.jsonl (one line per tools/call echo received) and checks:
- every call through ContextForge that reached echo was accepted, with the calling agent's identity and tool=echo;
- no agent call was refused by the identity plugin; the platform admin call was refused by it;
- every agent has at least one accepted call;
- --attacks: every direct attack was rejected with exactly the expected reason.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED = {
    "bypass_no_identity": "missing X-GxG-Identity",
    "replay_immediate": "replayed jti",
    "replay_tampered_args": "arguments hash mismatch",
    "replay_other_connector": "InvalidAudienceError: Audience doesn't match (strict)",
    "replay_other_tool": "bad tool",
    "forged_jws_attacker_key": "InvalidSignatureError: Signature verification failed",
    "replay_after_65s": "ExpiredSignatureError: Signature has expired",
}
DENIED = "GXG_IDENTITY_DENIED"


def rows(p: Path) -> list:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def check(variant: str, attacks: bool) -> list[str]:
    log = [r for r in rows(HERE / "secrets/raw/echo.jsonl") if r["body"].get("method") == "tools/call"]
    verdicts = []
    for r in log:
        src = {k.lower(): v for k, v in r["headers"].items()}.get("x-spike-attack", "via-contextforge")
        verdicts.append({"at": r["at"], "source": src, "path": r["path"], **r.get("verdict", {"missing": True})})
    out = HERE / f"evidence/variant-{variant}.verify_jws.jsonl"
    out.write_text("".join(json.dumps(v, sort_keys=True) + "\n" for v in verdicts))
    fail = [
        f"{v['source']} {v['at']}: {v}" for v in verdicts if v["source"] == "via-contextforge" and "accepted" not in v
    ]
    obs = rows(HERE / "secrets/observations.jsonl")
    agents = {o["agent"] for o in obs if o["kind"] == "call"}
    ok_agents = set()
    for o in obs:
        seen = o.get("echo_seen")
        if o["kind"] in ("call", "attack_forge") and seen:
            got = (seen.get("verdict") or {}).get("accepted") or {}
            if got.get("principal") != o["agent"] or got.get("tool") != "echo":
                fail.append(f"{o['kind']} {o['agent']} {o['path']}: verdict {seen.get('verdict')}")
            ok_agents.add(o["agent"])
        if o["kind"] == "call" and DENIED in json.dumps(o.get("error")):
            fail.append(f"call {o['agent']} {o['path']}: refused by the identity plugin")
        if o["kind"] == "admin_call" and (o["reached_echo"] or DENIED not in json.dumps(o["response"])):
            fail.append(f"admin_call: not refused by the identity plugin: {o['response']}")
    fail += [f"no accepted call for {a}" for a in sorted(agents - ok_agents)]
    if not any(o["kind"] == "admin_call" for o in obs):
        fail.append("admin_call: not run")
    for tag, why in EXPECTED.items() if attacks else ():
        got = [v.get("rejected") for v in verdicts if v["source"] == tag]
        if got != [why]:
            fail.append(f"{tag}: expected rejected {why!r}, got {got}")
    return fail


if __name__ == "__main__":
    problems = check(sys.argv[1], "--attacks" in sys.argv)
    for p in problems:
        print("FAIL:", p)
    print(f"check_verdicts {sys.argv[1]}: {'FAIL' if problems else 'OK'}")
    sys.exit(1 if problems else 0)
