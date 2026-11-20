#!/usr/bin/env python3
"""CI check `grants-widening` (AC-M2-04, GR-10).

Red while the policy diff report has `widening` entries and the PR has no `widening-approved`
label added by a user listed in CODEOWNERS (read from the PR *base* commit, so a PR cannot add
its author there). The label must be added by hand: an event performed via a GitHub App
(an agent acting with the owner's credentials) does not count. On a new push with widening the
label is removed, so the approval always covers what the owner saw.

Also posts the Markdown diff report as one PR comment, updated in place.

Usage (in a `pull_request` workflow):
  widening_gate.py --report diff.json --markdown diff.md --codeowners base-CODEOWNERS
Env: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, GITHUB_API_URL, GITHUB_STEP_SUMMARY.
Exit: 0 — pass, 1 — widening not approved, 2 — usage or API error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

LABEL = "widening-approved"
MARKER = "<!-- gxg-policy-diff -->"
BOT = "github-actions[bot]"

Api = Callable[[str, str, Any], Any]


def codeowners(text: str) -> set[str]:
    """Users (not teams) named anywhere in CODEOWNERS, lower-case, without `@`."""
    owners: set[str] = set()
    for line in text.splitlines():
        for tok in line.split("#", 1)[0].split()[1:]:
            if tok.startswith("@") and "/" not in tok:
                owners.add(tok[1:].lower())
    return owners


def last_label_event(events: list[dict[str, Any]], label: str = LABEL) -> dict[str, Any] | None:
    """The `labeled` event that put the label currently on the PR (None if it was removed afterwards)."""
    current: dict[str, Any] | None = None
    for e in events:
        if e.get("label", {}).get("name") == label:
            if e.get("event") == "labeled":
                current = e
            elif e.get("event") == "unlabeled":
                current = None
    return current


def decide(
    report: dict[str, Any], labels: set[str], event: dict[str, Any] | None, owners: set[str]
) -> tuple[bool, str]:
    n = len(report["widening"])
    if n == 0:
        return True, "no widening in the policy diff"
    if LABEL not in labels:
        return False, f"{n} widening change(s): a code owner must add the `{LABEL}` label"
    if event is None:
        return False, f"`{LABEL}` is on the PR but its `labeled` event was not found"
    actor = str((event.get("actor") or {}).get("login", "")).lower()
    app = event.get("performed_via_github_app")
    if app:
        return False, f"`{LABEL}` was added via the GitHub App {app.get('slug')!r}; the owner must add it by hand"
    if actor not in owners:
        return False, f"`{LABEL}` was added by @{actor}, who is not in CODEOWNERS ({sorted(owners)})"
    return True, f"{n} widening change(s) approved by @{actor} (`{LABEL}`)"


def github_api(token: str, base_url: str) -> Api:
    def call(method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(  # noqa: S310 - fixed https API base from the runner
            base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw = resp.read()
        return json.loads(raw) if raw else None

    return call


def paged(api: Api, path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 100):
        chunk = api("GET", f"{path}?per_page=100&page={page}", None)
        out += chunk
        if len(chunk) < 100:
            break
    return out


def upsert_comment(api: Api, issue: str, body: str) -> None:
    for c in paged(api, f"{issue}/comments"):
        if c.get("user", {}).get("login") == BOT and MARKER in c.get("body", ""):
            api("PATCH", issue.rsplit("/", 1)[0] + f"/comments/{c['id']}", {"body": body})
            return
    api("POST", f"{issue}/comments", {"body": body})


def run(api: Api, repo: str, event: dict[str, Any], report: dict[str, Any], *, markdown: str, owners: set[str]) -> int:
    number = event["pull_request"]["number"]
    issue = f"/repos/{repo}/issues/{number}"
    labels = {lab["name"] for lab in api("GET", f"{issue}/labels", None)}
    note = ""
    if event.get("action") == "synchronize" and report["widening"] and LABEL in labels:
        api("DELETE", f"{issue}/labels/{urllib.parse.quote(LABEL)}", None)
        labels.discard(LABEL)
        note = f"New commits since the approval: `{LABEL}` was removed, approve again after review.\n\n"
    ok, msg = decide(report, labels, last_label_event(paged(api, f"{issue}/events")), owners)
    body = f"{markdown}\n**grants-widening: {'pass' if ok else 'FAIL'}** — {note}{msg}\n"
    try:
        upsert_comment(api, issue, body)
    except urllib.error.HTTPError as exc:  # e.g. a fork PR with a read-only token: the check still decides
        print(f"::warning::could not post the diff comment: {exc}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body)
    print(("" if ok else "::error::") + note + msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--markdown", type=Path, required=True)
    p.add_argument("--codeowners", type=Path, required=True, help="CODEOWNERS of the PR base commit")
    a = p.parse_args(argv)
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        api = github_api(os.environ["GITHUB_TOKEN"], os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        return run(
            api,
            os.environ["GITHUB_REPOSITORY"],
            event,
            json.loads(a.report.read_text(encoding="utf-8")),
            markdown=a.markdown.read_text(encoding="utf-8"),
            owners=codeowners(a.codeowners.read_text(encoding="utf-8")),
        )
    except (KeyError, OSError, urllib.error.URLError, ValueError) as exc:
        print(f"::error::grants-widening could not decide: {exc!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
