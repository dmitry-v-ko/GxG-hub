#!/usr/bin/env python3
"""Reject commits that contain secret-like files (AC-M0-04, SEC secrets policy).

Checks, for every file it is asked to look at:

* file name, case-insensitively: ``*.pem``, ``*.key``, ``*.p12``, ``*.pfx``,
  ``.env``, ``.env.*`` (except ``.env.example``) and default SSH private key
  names (so ``KEY.PEM``, ``Server.Key`` and ``.ENV`` are rejected too);
* content: ANY occurrence of a PEM private key header
  (five dashes, ``BEGIN``, optional words, ``PRIVATE KEY``) anywhere in the file: at the start of a
  line, inline after ``KEY=``, inside quotes, in source code, in prose.
  The only exception: in the paths listed in ``CONTENT_ALLOWLIST`` one quoted
  header (backtick, header, backtick) is removed once before the scan; any other
  header in those files, and the same quote anywhere else, is reported.

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
import hashlib
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

# Exact repository paths that may contain ALLOWED_QUOTE once. The rest of the file is
# scanned as usual. Keep this list minimal; every entry needs a reason.
CONTENT_ALLOWLIST: dict[str, str] = {
    "docs/implementation-plan-v1.md": "AC-M0-04 in the plan quotes the header as an inline code span",
}
# Backtick + header + backtick, built from parts so this file never spells the header.
ALLOWED_QUOTE = ("`-----" + "BEGIN PRIVATE" + " KEY-----`").encode()

# Exact historical file versions (path + Git blob SHA-1) that quote the header in prose.
# They are already in the pushed history of this branch, which must not be rewritten,
# so a PR scan (`--range`) would otherwise stay red forever. Any other content at the
# same path, including the next edit of the same file, is scanned normally.
HISTORICAL_BLOB_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "reports/conformance/M0.json",
        "fcc7ad045456b4ada7d9752fa4d2f1719dbd0d86",
    ): "M0 cycle 1 report (commit 9d34699) quotes AC-M0-04; no key material",
    (
        "reports/critic/M0-cycle-1.json",
        "8bcef7589e7d3b0ba8f446e22ef01e0109f3e794",
    ): "M0 cycle 1 critic report (commit 3182606) quotes AC-M0-04; no key material",
}

# Any PEM private key header: five dashes, BEGIN, zero or more words (RSA, EC, DSA,
# OPENSSH, ENCRYPTED, PGP, ...), PRIVATE KEY. Trailing dashes are not required; case
# and runs of spaces/tabs are ignored. (This file never spells a header out, so it
# is scanned like any other file.)
_PEM_PRIVATE_KEY = re.compile(r"-----[ \t]*BEGIN[ \t]+(?:[A-Z0-9]+[ \t]+)*PRIVATE[ \t]+KEY", re.IGNORECASE)

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
        if fnmatch.fnmatchcase(name.lower(), pattern):
            return f"forbidden file name pattern {pattern!r}"
    return None


def git_blob_sha(data: bytes) -> str:
    """The SHA-1 Git uses as the blob id of ``data`` (``git hash-object``)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def content_rule(data: bytes, path: str = "") -> str | None:
    if (path, git_blob_sha(data)) in HISTORICAL_BLOB_ALLOWLIST:
        return None
    if path in CONTENT_ALLOWLIST:
        data = data.replace(ALLOWED_QUOTE, b"", 1)
    # UTF-16/32 text would hide the ASCII header; scan the common encodings.
    texts = [data.decode("utf-8", errors="replace")]
    if b"\0" in data:
        texts += [data.decode(enc, errors="replace") for enc in ("utf-16-le", "utf-16-be")]
    if any(_PEM_PRIVATE_KEY.search(t) for t in texts):
        return "PEM private key header"
    return None


def check_file(rev: str, path: str, data: bytes | None) -> list[Finding]:
    findings: list[Finding] = []
    rule = name_rule(path)
    if rule:
        findings.append(Finding(rev, path, rule))
    if data is not None:
        rule = content_rule(data, path)
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
