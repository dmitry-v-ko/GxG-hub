#!/usr/bin/env python3
"""Reject commits that contain secret-like files (AC-M0-04, SEC secrets policy).

Checks, for every file it is asked to look at:

* file name: ``*.pem``, ``*.key``, ``*.p12``, ``*.pfx``, ``.env``, ``.env.*``
  (except ``.env.example``) and default SSH private key names;
* content: a PEM private key header (``-----BEGIN ... PRIVATE KEY-----``) that
  starts a line, or that is directly followed by key material (e.g. a key
  pasted into a JSON/YAML string as ``"-----BEGIN...-----\\nMIIE..."``).
  A header merely quoted inside prose (as in the implementation plan) is not
  key material and is not reported.

Modes (combinable):

* ``--tree [REV]``  every file in the tree of REV (default ``HEAD``);
* ``--range A..B``  every file added or modified by each commit in ``A..B``
  (catches a secret that was committed and deleted again inside a PR).
  If ``A`` is the all-zero SHA (new branch), all commits reachable from ``B``.

Findings print ``<commit>:<path>: <rule>`` and never the matched content.
Exit code: 0 — clean, 1 — findings, 2 — usage or git error.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FORBIDDEN_NAME_PATTERNS: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".env",
    ".env.*",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
ALLOWED_NAMES: frozenset[str] = frozenset({".env.example"})

_PEM_HEADER = r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
# Header at the start of a line (real PEM files, heredocs, multi-line strings).
_PEM_LINE_START = re.compile(rf"(?m)^[ \t]*{_PEM_HEADER}")
# Header followed by key material, separated by a newline or an escaped "\n".
_PEM_WITH_BODY = re.compile(rf"{_PEM_HEADER}(?:\r?\n|(?:\\r)?\\n)[ \t]*[A-Za-z0-9+/=]{{16,}}")

ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class Finding:
    rev: str
    path: str
    rule: str

    def __str__(self) -> str:
        return f"{self.rev[:12]}:{self.path}: {self.rule}"


def name_rule(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in ALLOWED_NAMES:
        return None
    for pattern in FORBIDDEN_NAME_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return f"forbidden file name pattern {pattern!r}"
    return None


def content_rule(data: bytes) -> str | None:
    text = data.decode("utf-8", errors="replace")
    if _PEM_LINE_START.search(text) or _PEM_WITH_BODY.search(text):
        return "PEM private key block"
    return None


def check_file(rev: str, path: str, data: bytes | None) -> list[Finding]:
    findings: list[Finding] = []
    rule = name_rule(path)
    if rule:
        findings.append(Finding(rev, path, rule))
    if data is not None:
        rule = content_rule(data)
        if rule:
            findings.append(Finding(rev, path, rule))
    return findings


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _split_z(raw: bytes) -> list[str]:
    return [p.decode("utf-8", errors="surrogateescape") for p in raw.split(b"\0") if p]


def _blob(repo: Path, rev: str, path: str) -> bytes | None:
    try:
        return _git(repo, "cat-file", "blob", f"{rev}:{path}")
    except GitError:
        return None  # submodule entry or similar: nothing to read


def files_in_tree(repo: Path, rev: str) -> list[str]:
    return _split_z(_git(repo, "ls-tree", "-r", "-z", "--name-only", rev))


def commits_in_range(repo: Path, spec: str) -> list[str]:
    base, sep, head = spec.partition("..")
    if not sep or not head:
        raise GitError(f"range must look like A..B, got {spec!r}")
    rev_args = [head] if (not base or base == ZERO_SHA) else [f"{base}..{head}"]
    out = _git(repo, "rev-list", "--reverse", *rev_args).decode().split()
    return out


def files_changed_by(repo: Path, commit: str) -> list[str]:
    raw = _git(
        repo,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "-r",
        "-z",
        "-m",
        "--diff-filter=ACMRT",
        "--name-only",
        commit,
    )
    return sorted(set(_split_z(raw)))


def scan_tree(repo: Path, rev: str = "HEAD") -> list[Finding]:
    commit = _git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    return list(_scan(repo, ((commit, p) for p in files_in_tree(repo, commit))))


def scan_range(repo: Path, spec: str) -> list[Finding]:
    pairs = ((c, p) for c in commits_in_range(repo, spec) for p in files_changed_by(repo, c))
    return list(_scan(repo, pairs))


def _scan(repo: Path, pairs: Iterable[tuple[str, str]]) -> Iterator[Finding]:
    for rev, path in pairs:
        yield from check_file(rev, path, _blob(repo, rev, path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--tree", nargs="?", const="HEAD", metavar="REV")
    parser.add_argument("--range", dest="range_", metavar="A..B")
    args = parser.parse_args(argv)
    if args.tree is None and args.range_ is None:
        args.tree = "HEAD"
    try:
        findings: list[Finding] = []
        if args.tree is not None:
            findings += scan_tree(args.repo, args.tree)
        if args.range_ is not None:
            findings += scan_range(args.repo, args.range_)
    except GitError as exc:
        print(f"check_secrets: {exc}", file=sys.stderr)
        return 2
    for f in dict.fromkeys(findings):
        print(f"SECRET: {f}", file=sys.stderr)
    if findings:
        print(
            f"check_secrets: {len(set(findings))} finding(s). Remove the file from the commits, "
            "rotate the secret, keep real values only in .env (ignored by Git).",
            file=sys.stderr,
        )
        return 1
    print("check_secrets: no secret-like files found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
