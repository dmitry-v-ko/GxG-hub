#!/usr/bin/env python3
"""CI check `grants-widening` (AC-M2-04, GR-10).

Red while the policy diff report has `widening` entries and the PR has no `widening-approved`
label added by a user listed in CODEOWNERS (read from the PR *base* commit, so a PR cannot add
its author there). The label must be added by hand: an event performed via a GitHub App
(an agent acting with the owner's credentials) does not count.

The approval binds to the widening it saw. A label newer than the moment the head SHA arrived on
the PR (server time: the first `pull_request` workflow run for that SHA on this PR after the latest
force-push, from the PR's own head repository) covers the head. An older label still covers it when
every widening finding of the head was already a finding of the commit that was the head when the
label was added (`--repo`/`--main`: both sides computed the same way, against their merge-base with
main, so a merge of main changes nothing). Otherwise the label is stale: it is removed, the check is red.

Outside a pull request (`--no-pr`: push or workflow_dispatch to a branch other than main) there
is no label to look at, so any widening is red: fail closed.

Also posts the Markdown diff report as one PR comment, updated in place. The comment is found by
the first line of the Markdown file (the marker written by `gxg-policy diff`).

Every finding in `--gate-findings` (gate_diff.py: control changes in CI, tests, lint, exception lists,
schemas, packages and the gate itself) is added to the report as a widening entry: it needs the same label.

Usage (both modes also take --gate-findings FILE; PR mode also --repo DIR --main REF):
  widening_gate.py --report diff.json --markdown diff.md --codeowners base-CODEOWNERS   (pull_request)
  widening_gate.py --report diff.json --markdown diff.md --no-pr                        (other events)
Env (PR mode): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, GITHUB_API_URL; GITHUB_STEP_SUMMARY.
Exit: 0 — pass, 1 — widening not approved, 2 — usage or API error.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

LABEL = "widening-approved"
BOT = "github-actions[bot]"

Api = Callable[[str, str, Any], Any]
Carry = Callable[[str], "list[dict[str, Any]] | None"]


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


def upsert_comment(api: Api, issue: str, body: str, marker: str) -> None:
    for c in paged(api, f"{issue}/comments"):
        if c.get("user", {}).get("login") == BOT and c.get("body", "").startswith(marker):
            api("PATCH", issue.rsplit("/", 1)[0] + f"/comments/{c['id']}", {"body": body})
            return
    api("POST", f"{issue}/comments", {"body": body})


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def head_arrival(api: Api, repo: str, pr: dict[str, Any], events: list[dict[str, Any]]) -> datetime | None:
    """Server time the head SHA (last) arrived on this PR: the earliest `pull_request` run for it created
    after the latest force-push. Runs before that force-push belong to an earlier visit of the same SHA
    (B, force-push to A, fast-forward back to B), runs from another head repository to another PR."""
    sha = pr["head"]["sha"]
    repo_id = (pr["head"].get("repo") or {}).get("id")
    forced = max((_ts(e["created_at"]) for e in events if e.get("event") == "head_ref_force_pushed"), default=None)
    runs = api("GET", f"/repos/{repo}/actions/runs?event=pull_request&head_sha={sha}&per_page=100", None)
    times = [
        _ts(r["created_at"])
        for r in runs.get("workflow_runs", [])
        if r.get("head_sha") == sha
        and r.get("head_branch") == pr["head"]["ref"]
        and repo_id is not None
        and (r.get("head_repository") or {}).get("id") == repo_id
        # Runs linked to other PRs do not count (same SHA pushed to another branch earlier).
        and (not r.get("pull_requests") or pr["number"] in {x.get("number") for x in r["pull_requests"]})
    ]
    return min((t for t in times if forced is None or t > forced), default=None)


def head_at(api: Api, repo: str, pr: dict[str, Any], at: datetime) -> str | None:
    """The head SHA of this PR at `at`: the SHA of its latest `pull_request` run created no later than `at`."""
    ref = pr["head"]["ref"]
    repo_id = (pr["head"].get("repo") or {}).get("id")
    q = urllib.parse.urlencode({"event": "pull_request", "branch": ref, "per_page": 100})
    runs = api("GET", f"/repos/{repo}/actions/runs?{q}", None)
    best: tuple[datetime, str] | None = None
    for r in runs.get("workflow_runs", []):
        t = _ts(r["created_at"])
        if (
            t <= at
            and r.get("head_branch") == ref
            and repo_id is not None
            and (r.get("head_repository") or {}).get("id") == repo_id
            and (not r.get("pull_requests") or pr["number"] in {x.get("number") for x in r["pull_requests"]})
            and (best is None or t > best[0])
        ):
            best = (t, r["head_sha"])
    return None if best is None else best[1]


def _key(entry: dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True)


def uncovered(head: list[dict[str, Any]], approved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings of the head that the approved commit did not have (as a multiset: two equal lines need two)."""
    left = [_key(e) for e in approved]
    out = []
    for e in head:
        k = _key(e)
        if k in left:
            left.remove(k)
        else:
            out.append(e)
    return out


def carried(
    api: Api, repo: str, pr: dict[str, Any], label: dict[str, Any], *, report: dict[str, Any], carry: Carry
) -> tuple[bool, str]:
    """Whether a label older than the head still covers it, and why (see the module docstring)."""
    sha = head_at(api, repo, pr, _ts(label["created_at"]))
    if sha is None:
        return False, "the head commit at the time of the label is unknown"
    approved = carry(sha)
    if approved is None:
        return False, f"the widening of {sha[:12]} (the head at the time of the label) could not be computed"
    new = uncovered(report["widening"], approved)
    if new:
        shown = "; ".join(str(e.get("detail") or e.get("path") or e.get("kind")) for e in new[:5])
        return False, f"{len(new)} widening change(s) since the label on {sha[:12]}: {shown}"
    return True, f"`{LABEL}` from {sha[:12]} still covers the head: no new widening since the label"


def stale(label: dict[str, Any] | None, arrival: datetime | None) -> str | None:
    """Why the approval does not cover the head commit, or None if it does."""
    if label is None:
        return None
    if arrival is None:
        return "the arrival time of the head commit is unknown"
    if _ts(label["created_at"]) <= arrival:
        return f"the label ({label['created_at']}) is older than the head commit on the PR ({arrival.isoformat()})"
    return None


def report_out(body: str, ok: bool, msg: str) -> int:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body)
    print(("" if ok else "::error::") + msg)
    return 0 if ok else 1


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def with_gate_findings(
    report: dict[str, Any], markdown: str, findings: list[dict[str, Any]]
) -> tuple[dict[str, Any], str]:
    """Control changes found by gate_diff.py count as widening: the label must cover them."""
    if not findings:
        return report, markdown
    rows = "".join(f"| {_cell(f['rule'])} | `{_cell(f['path'])}` | {_cell(f['detail'])} |\n" for f in findings)
    note = (
        f"\n### Control changes ({len(findings)}, need the `{LABEL}` label)\n\n"
        f"| rule | path | detail |\n| --- | --- | --- |\n{rows}"
    )
    return {**report, "widening": report["widening"] + findings}, markdown + note


def run_no_pr(report: dict[str, Any], *, markdown: str) -> int:
    n = len(report["widening"])
    ok = n == 0
    msg = (
        "no widening against the merge-base with main"
        if ok
        else f"{n} widening change(s) outside a pull request: widening is approved only on a PR"
        f" with the `{LABEL}` label from a code owner"
    )
    return report_out(f"{markdown}\n**grants-widening: {'pass' if ok else 'FAIL'}** — {msg}\n", ok, msg)


def run(
    api: Api,
    repo: str,
    event: dict[str, Any],
    report: dict[str, Any],
    *,
    markdown: str,
    owners: set[str],
    carry: Carry | None = None,
) -> int:
    pr = event["pull_request"]
    issue = f"/repos/{repo}/issues/{pr['number']}"
    labels = {lab["name"] for lab in api("GET", f"{issue}/labels", None)}
    events = paged(api, f"{issue}/events")
    label = last_label_event(events)
    note = ""
    if report["widening"] and LABEL in labels:
        why = stale(label, head_arrival(api, repo, pr, events))
        if why and carry is not None and label is not None:
            ok, reason = carried(api, repo, pr, label, report=report, carry=carry)
            if ok:
                why, note = None, reason + ".\n\n"
            else:
                why = f"{why}, and {reason}"
        if why:
            api("DELETE", f"{issue}/labels/{urllib.parse.quote(LABEL)}", None)
            labels.discard(LABEL)
            note = f"`{LABEL}` was removed: {why}. Review the new commits and approve again.\n\n"
    ok, msg = decide(report, labels, label, owners)
    body = f"{markdown}\n**grants-widening: {'pass' if ok else 'FAIL'}** — {note}{msg}\n"
    try:
        upsert_comment(api, issue, body, markdown.split("\n", 1)[0])
    except urllib.error.HTTPError as exc:  # e.g. a fork PR with a read-only token: the check still decides
        print(f"::warning::could not post the diff comment: {exc}")
    return report_out(body, ok, note + msg)


def carry_from(repo: Path, main_ref: str) -> Carry:
    """Widening of a past head, computed by gate_diff.py next to this file (main's copy in CI)."""
    widening_at = runpy.run_path(str(Path(__file__).with_name("gate_diff.py")))["widening_at"]
    tmp = Path(os.environ.get("RUNNER_TEMP") or tempfile.mkdtemp())

    def carry(sha: str) -> list[dict[str, Any]] | None:
        try:
            result: list[dict[str, Any]] = widening_at(repo, main_ref, sha, tmp)
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            print(f"::warning::widening of {sha} not computed: {exc!r}")
            return None
        return result

    return carry


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--markdown", type=Path, required=True)
    p.add_argument("--codeowners", type=Path, help="CODEOWNERS of the PR base commit (PR mode)")
    p.add_argument("--no-pr", action="store_true", help="not a pull_request event: any widening fails")
    p.add_argument("--gate-findings", type=Path, help="gate_diff.py JSON: each finding counts as widening")
    p.add_argument("--repo", type=Path, help="checkout with the PR history (PR mode): lets an older label carry over")
    p.add_argument("--main", default="refs/remotes/origin/main", help="main ref for --repo")
    a = p.parse_args(argv)
    try:
        report = json.loads(a.report.read_text(encoding="utf-8"))
        markdown = a.markdown.read_text(encoding="utf-8")
        if a.gate_findings is not None:
            findings = json.loads(a.gate_findings.read_text(encoding="utf-8"))
            report, markdown = with_gate_findings(report, markdown, findings)
        if a.no_pr:
            return run_no_pr(report, markdown=markdown)
        if a.codeowners is None:
            p.error("--codeowners is required without --no-pr")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        api = github_api(os.environ["GITHUB_TOKEN"], os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        return run(
            api,
            os.environ["GITHUB_REPOSITORY"],
            event,
            report,
            markdown=markdown,
            owners=codeowners(a.codeowners.read_text(encoding="utf-8")),
            carry=None if a.repo is None else carry_from(a.repo, a.main),
        )
    except (KeyError, OSError, urllib.error.URLError, ValueError) as exc:
        print(f"::error::grants-widening could not decide: {exc!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
