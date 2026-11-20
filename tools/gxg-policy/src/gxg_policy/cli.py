"""`gxg-policy validate | lint | diff | build | verify`. Exit: 0 ok, 1 findings or bad bundle, 2 usage/input error."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from gxg_policy.bundle import BUNDLE, SIG, BundleError, build, unb64url, verify
from gxg_policy.diff import PolicyError, diff, markdown
from gxg_policy.lint import lint
from gxg_policy.load import Policies, load

KEY_ENV = "GXG_BUNDLE_SIGNING_KEY"  # base64url 32-byte Ed25519 seed, from CI secrets
KID_ENV = "GXG_BUNDLE_KID"
TS = "%Y-%m-%dT%H:%M:%SZ"  # built_at: RFC 3339 UTC, seconds (GR-11a)


def _report(pol: Policies) -> int:
    for f in pol.findings:
        print(f"{f.level}: {f}", file=sys.stderr if f.level == "error" else sys.stdout)
    print(f"{len(pol.errors)} error(s), {len(pol.findings) - len(pol.errors)} warning(s)")
    return 1 if pol.errors else 0


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S607


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gxg-policy", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("validate", help="JSON Schema check of every policy file")
    s.add_argument("root", type=Path)
    s = sub.add_parser("lint", help="schema + cross-reference checks")
    s.add_argument("root", type=Path)
    s.add_argument("--manifest", type=Path, action="append", help="connector manifest (default: repo manifests)")
    s = sub.add_parser("diff", help="rights diff: widening / narrowing")
    s.add_argument("base", type=Path)
    s.add_argument("head", type=Path)
    s.add_argument("--json", type=Path, help="write the JSON report here (default: stdout)")
    s.add_argument("--md", type=Path, help="write the Markdown report here")
    s = sub.add_parser("build", help=f"build and sign {BUNDLE} (key: ${KEY_ENV})")
    s.add_argument("root", type=Path)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--bundle-version", help="default: git rev-parse HEAD")
    s.add_argument("--built-at", help="RFC 3339 UTC, default: now")
    s.add_argument("--kid", default=os.environ.get(KID_ENV), help=f"default: ${KID_ENV}")
    s.add_argument("--manifest", type=Path, action="append")
    s = sub.add_parser("verify", help=f"check {SIG} and the bundle shape")
    s.add_argument("dir", type=Path)
    s.add_argument("--kid", required=True)
    s.add_argument("--public-key", required=True, help="ed25519:<base64url raw key>")
    a = p.parse_args(argv)
    commands = {"validate": _validate, "lint": _lint, "diff": _diff, "build": _build, "verify": _verify}
    return commands[a.cmd](a)


def _validate(a: argparse.Namespace) -> int:
    return _report(load(a.root))


def _lint(a: argparse.Namespace) -> int:
    return _report(lint(a.root, a.manifest))


def _diff(a: argparse.Namespace) -> int:
    try:
        report = diff(a.base, a.head)
    except PolicyError as exc:
        print(f"error: invalid policies:\n{exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if a.json:
        a.json.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if a.md:
        a.md.write_text(markdown(report), encoding="utf-8")
    return 0


def _build(a: argparse.Namespace) -> int:
    seed_b64 = os.environ.get(KEY_ENV)
    if not seed_b64 or not a.kid:
        print(f"error: ${KEY_ENV} and --kid/${KID_ENV} are required", file=sys.stderr)
        return 2
    pol = lint(a.root, a.manifest)
    if pol.errors:
        return _report(pol)
    built_at = a.built_at or datetime.now(UTC).strftime(TS)
    try:  # round-trip: strptime alone accepts 2026-1-1T0:0:0Z
        if datetime.strptime(built_at, TS).strftime(TS) != built_at:
            raise ValueError(built_at)
    except ValueError:
        print(f"error: --built-at must be RFC 3339 UTC like 2026-01-01T00:00:00Z, got {built_at!r}", file=sys.stderr)
        return 2
    try:
        seed = unb64url(seed_b64)
        build(pol, a.out, bundle_version=a.bundle_version or _git_head(), built_at=built_at, seed=seed, kid=a.kid)
    except (BundleError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {a.out / BUNDLE} and {a.out / SIG}")
    return 0


def _verify(a: argparse.Namespace) -> int:
    try:
        doc = verify((a.dir / BUNDLE).read_bytes(), (a.dir / SIG).read_bytes(), {a.kid: a.public_key})
    except (BundleError, OSError) as exc:
        print(f"error: bundle rejected: {exc}", file=sys.stderr)
        return 1
    print(f"ok: bundle_version={doc['bundle_version']} built_at={doc['built_at']} tenants={sorted(doc['tenants'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
