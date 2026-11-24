#!/usr/bin/env python3
"""Offline check of policies/network/tailscale-acl.hujson (SEC-26, SEC-27, AC-M14-08). No network.

    python scripts/network/check_acl.py [policy.hujson]

Checks: HuJSON parses; every tag in TS_TAGS of deploy/railway/services.yaml is in tagOwners (owner autogroup:admin);
no wildcards, no funnel, no exit node; every allowed (src, dst) pair has an `accept` test, every tag has a `deny` test;
each test agrees with the rules (a small evaluator: exact src and dst:port match). Exit 1 on any problem.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "policies/network/tailscale-acl.hujson"
SERVICES = ROOT / "deploy/railway/services.yaml"


def strip_hujson(text: str) -> str:
    """Remove // and /* */ comments (outside strings) and trailing commas."""
    out: list[str] = []
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        else:
            out.append(c)
        i += 1
    # trailing commas: strings are matched first so a comma inside one is left alone
    pat = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])')
    return pat.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else m.group(1), "".join(out))


def load(path: Path) -> dict[str, Any]:
    data = json.loads(strip_hujson(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError("policy root is not an object")
    return data


def used_tags(services: Path) -> set[str]:
    spec = yaml.safe_load(services.read_text(encoding="utf-8"))
    tags: set[str] = set()
    for svc in spec["services"].values():
        for t in str(svc.get("env", {}).get("TS_TAGS", "")).replace(",", " ").split():
            tags.add(t)
    return tags


def pairs(acls: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(s, d) for r in acls if r.get("action") == "accept" for s in r["src"] for d in r["dst"]}


def check(policy: dict[str, Any], tags_in_use: set[str]) -> list[str]:  # noqa: PLR0912
    errs: list[str] = []
    owners = policy.get("tagOwners", {})
    for t in sorted(tags_in_use | {"tag:agent"}):
        if owners.get(t) != ["autogroup:admin"]:
            errs.append(f"tagOwners: {t} must be owned by exactly autogroup:admin")
    for g in ("group:operators", "group:approvers"):
        if g not in policy.get("groups", {}):
            errs.append(f"groups: {g} missing")
    if "grants" in policy:
        errs.append("use `acls` only (the checker evaluates them); `grants` is not supported")
    acls = policy.get("acls", [])
    allowed = pairs(acls)
    for r in acls:
        if r.get("action") != "accept":
            errs.append(f"acls: unexpected action {r.get('action')!r}")
        for x in [*r.get("src", []), *r.get("dst", [])]:
            if "*" in x or "autogroup:internet" in x or ("autogroup:" in x and x != "autogroup:admin"):
                errs.append(f"acls: wildcard or autogroup not allowed: {x}")
        for d in r.get("dst", []):
            if d.count(":") != 2 or not d.rsplit(":", 1)[1].isdigit():  # tag:name:port
                errs.append(f"acls: dst must be tag:<name>:<single port>: {d}")
    # Funnel / exit node / subnet routes (SEC-27)
    for na in policy.get("nodeAttrs", []):
        if "funnel" in json.dumps(na).lower():
            errs.append("nodeAttrs: funnel is forbidden")
    if "autoApprovers" in policy:
        errs.append("autoApprovers (routes, exit node) are forbidden on Hub nodes")
    # tests
    accepted: set[tuple[str, str]] = set()
    denied_dst: set[str] = set()
    for t in policy.get("tests", []):
        src = t["src"]
        for d in t.get("accept", []):
            accepted.add((src, d))
            if (src, d) not in allowed:
                errs.append(f"tests: accept {src} -> {d} but no rule allows it")
        for d in t.get("deny", []):
            denied_dst.add(d)
            if (src, d) in allowed:
                errs.append(f"tests: deny {src} -> {d} but a rule allows it")
    for s, d in sorted(allowed - accepted):
        errs.append(f"tests: rule {s} -> {d} has no accept test")
    for tag in sorted(owners):
        if tag != "tag:agent" and not any(d.startswith(tag + ":") for d in denied_dst):
            errs.append(f"tests: no deny test with dst {tag}")
        if tag == "tag:agent" and not any(t["src"] == tag and t.get("deny") for t in policy.get("tests", [])):
            errs.append("tests: no deny test with src tag:agent")
    return errs


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else POLICY
    try:
        errs = check(load(path), used_tags(SERVICES))
    except (ValueError, KeyError, OSError) as exc:
        errs = [f"cannot read policy: {exc!r}"]
    for msg in errs:
        print(f"check_acl: {msg}", file=sys.stderr)
    if not errs:
        print("check_acl: OK")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
