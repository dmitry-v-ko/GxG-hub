#!/usr/bin/env python3
"""DoD-4 for M2: each single-condition mutation of gxg-policy / the widening gate must turn a test red.

Applies one mutation at a time to the working tree, runs the M2 tests (`pytest -x`), restores the file.
Prints one line per mutation and a summary; exit 1 if any mutation survives.
Run from the repository root: `uv run python scripts/ci/mutate_m2.py [> reports/mutations/M2-cycle-N.txt]`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = "tools/gxg-policy/src/gxg_policy/"
GATE = "scripts/ci/widening_gate.py"
JOB = "scripts/ci/grants_widening.sh"
TESTS = ["tools/gxg-policy/tests", "tests/ci/test_widening_gate.py", "tests/ci/test_grants_widening_job.py"]

# (file, exact text, replacement, what the mutation breaks)
MUTATIONS: list[tuple[str, str, str, str]] = [
    # diff: grants
    (
        SRC + "diff.py",
        'self.add(True, **pair, change="added"',
        'self.add(False, **pair, change="added"',
        "added pair -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(False, **pair, change="removed")',
        'self.add(True, **pair, change="removed")',
        "removed pair -> widening",
    ),
    (
        SRC + "diff.py",
        'for s in expand(g["subjects"], doc.get("groups", {})):',
        'for s in g["subjects"]:',
        "no group expansion in grants",
    ),
    (
        SRC + "diff.py",
        "                    hx > bx,\n",
        "                    hx < bx,\n",
        "expiry extended -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(True, tenant=tenant, kind="role"',
        'self.add(False, tenant=tenant, kind="role"',
        "new role -> narrowing",
    ),
    # diff: approval rules
    (
        SRC + "diff.py",
        'self.add(h[rid]["decision"] == "auto_approve", **e, change="added"',
        'self.add(False, **e, change="added"',
        "new auto_approve -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(b[rid]["decision"] != "auto_approve", **e, change="removed"',
        'self.add(False, **e, change="removed"',
        "removed require_approval -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(True, **e, change="match_changed")',
        'self.add(False, **e, change="match_changed")',
        "match_changed -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(True, **e, change=f"{field}_added"',
        'self.add(False, **e, change=f"{field}_added"',
        "approvers/requesters added -> narrowing",
    ),
    (
        SRC + "diff.py",
        'self.add(False, **e, change=f"{field}_removed"',
        'self.add(True, **e, change=f"{field}_removed"',
        "approvers/requesters removed -> widening",
    ),
    (
        SRC + "diff.py",
        'rule["match"]["requesters"] = sorted(expand(rule["match"]["requesters"], groups))',
        "pass",
        "requester groups not expanded",
    ),
    (
        SRC + "diff.py",
        'rule["approvers"] = sorted(expand(rule["approvers"], groups))',
        "pass",
        "approver groups not expanded",
    ),
    (
        SRC + "diff.py",
        'self.sets(e, "requesters", bm.pop("requesters", ["*"]), hm.pop("requesters", ["*"]))',
        'bm.pop("requesters", None), hm.pop("requesters", None)',
        "auto_approve requesters ignored",
    ),
    (SRC + "diff.py", "set() if \"*\" in ba else ha - ba", "ha - ba", "'*' -> specific approvers reported as widening"),
    (
        SRC + "diff.py",
        "wider = STRICTNESS[h[\"decision\"]] < STRICTNESS[b[\"decision\"]]",
        "wider = False",
        "relaxed decision -> narrowing",
    ),
    # lint
    (
        SRC + "lint.py",
        "                    if s[6:] not in groups:\n",
        "                    if False:\n",
        "dangling group subject",
    ),
    (SRC + "lint.py", 'if other is not None and other != tenant:', 'if False:', "foreign principal"),
    (SRC + "lint.py", "elif conns[conn] != domain:", "elif False:", "RID of the wrong domain"),
    (
        SRC + "lint.py",
        'if "high_risk" in match.get("effect_classes", []):',
        "if False:",
        "auto_approve high_risk class",
    ),
    (SRC + "lint.py", 'elif "approver" not in roles.get(person, []):', "elif False:", "approver without role"),
    (SRC + "lint.py", "if seconds(value) > SYSTEM_DEFAULTS[key]:", "if False:", "defaults over system ttl"),
    (SRC + "lint.py", "            if value in seen:\n", "            if False:\n", "duplicate id"),
    # load / cli
    (
        SRC + "cli.py",
        "if datetime.strptime(built_at, TS).strftime(TS) != built_at:",
        "if False:",
        "--built-at not checked (format)",
    ),
    # bundle
    (SRC + "bundle.py", 'if sig["kid"] not in keys:', "if False:", "unknown kid check"),
    (
        SRC + "bundle.py",
        'if not isinstance(doc, dict) or set(doc) != BUNDLE_KEYS or doc["schema_version"] != 1:',
        "if not isinstance(doc, dict):",
        "verify shape check",
    ),
    (SRC + "bundle.py", "if jcs(doc) != data:", "if False:", "JCS check in verify"),
    (
        SRC + "bundle.py",
        'g["subjects"] = expand(g["subjects"], grants.get("groups", {}))',
        "pass",
        "no group expansion in bundle",
    ),
    (
        SRC + "bundle.py",
        'key=lambda k: k.encode("utf-16-be")',
        "key=lambda k: k",
        "code point instead of UTF-16 key order",
    ),
    # gate
    (GATE, "if actor not in owners:", "if False:", "label by a non-owner counts"),
    (GATE, 'app = event.get("performed_via_github_app")', "app = None", "label via GitHub App counts"),
    (GATE, "if at <= arrival:", "if False:", "label older than the head commit counts"),
    (GATE, "if forced and at <= max(forced):", "if False:", "force-push after the label ignored"),
    (GATE, 'and r.get("head_branch") == pr["head"]["ref"]', "", "runs of other branches date the head"),
    (
        GATE,
        'and (not r.get("pull_requests") or pr["number"] in {x.get("number") for x in r["pull_requests"]})',
        "",
        "runs of other PRs date the head",
    ),
    (GATE, 'return "the arrival time of the head commit is unknown"', "return None", "unknown arrival passes"),
    (
        GATE,
        'if report["widening"] and LABEL in labels:',
        'if report["widening"] and LABEL in labels and pr.get("action") == "synchronize":',
        "staleness only on synchronize",
    ),
    (GATE, "ok = n == 0", "ok = True", "no-PR mode passes widening"),
    # job script
    (JOB, 'base=$(git merge-base origin/main HEAD)', "base=HEAD", "push run diffs HEAD against itself"),
    (JOB, 'if [ "$EVENT" != "pull_request" ]; then\n  exec', 'if false; then\n  exec', "push run uses PR mode"),
]


def run_tests() -> bool:
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False).returncode == 0


def main() -> int:
    if not run_tests():
        print("baseline: tests are red, nothing to measure")
        return 2
    survived = 0
    for file, old, new, what in MUTATIONS:
        path = ROOT / file
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"STALE     {file}: {what} (text found {text.count(old)} times)")
            survived += 1
            continue
        try:
            path.write_text(text.replace(old, new), encoding="utf-8")
            green = run_tests()
        finally:
            path.write_text(text, encoding="utf-8")
        survived += green
        print(f"{'SURVIVED' if green else 'killed  '}  {file}: {what}")
    print(f"{len(MUTATIONS) - survived}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
