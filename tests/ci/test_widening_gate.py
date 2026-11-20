"""AC-M2-04: `grants-widening` is red while the diff has widening and no code owner added `widening-approved`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "widening_gate.py"
WIDE: dict[str, Any] = {"widening": [{"kind": "grant", "change": "added"}], "narrowing": []}
NARROW: dict[str, Any] = {"widening": [], "narrowing": [{"kind": "grant", "change": "removed"}]}
OWNERS = {"dmitry-v-ko"}


def load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scripts.ci.widening_gate", SCRIPT)  # mutmut keys use this name
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = load_gate()


T_PUSH = "2026-11-20T10:00:00Z"  # head commit B arrives on the PR
T_BEFORE, T_AFTER = "2026-11-20T09:00:00Z", "2026-11-20T11:00:00Z"
SHA, REF = "b" * 40, "claude/x"
HEAD: dict[str, Any] = {"sha": SHA, "ref": REF, "repo": {"id": 1}}


def labeled(
    login: str, event: str = "labeled", label: str = "widening-approved", app: Any = None, at: str = T_AFTER
) -> dict[str, Any]:
    return {
        "event": event,
        "label": {"name": label},
        "actor": {"login": login},
        "performed_via_github_app": app,
        "created_at": at,
    }


def pr_run(at: str = T_PUSH, sha: str = SHA, branch: str = REF, prs: Any = (7,), repo_id: int = 1) -> dict[str, Any]:
    return {
        "head_sha": sha,
        "head_branch": branch,
        "head_repository": {"id": repo_id},
        "created_at": at,
        "pull_requests": [{"number": n} for n in prs],
    }


def test_codeowners_reads_users_not_teams_or_comments() -> None:
    text = "# @ghost in a comment\n/schemas/ @Dmitry-V-Ko @org/team\n/x/ @other # @nope\n"
    assert gate.codeowners(text) == {"dmitry-v-ko", "other"}
    assert gate.codeowners((ROOT / ".github/CODEOWNERS").read_text()) == OWNERS


def test_last_label_event_follows_add_and_remove() -> None:
    a, b = labeled("dmitry-v-ko"), labeled("dmitry-v-ko", "unlabeled")
    assert gate.last_label_event([a]) is a
    assert gate.last_label_event([a, b]) is None
    assert gate.last_label_event([a, b, labeled("mallory")])["actor"]["login"] == "mallory"
    assert gate.last_label_event([labeled("x", label="other")]) is None


@pytest.mark.parametrize(
    ("report", "labels", "event", "ok"),
    [
        (NARROW, set(), None, True),  # narrowing needs nothing
        (WIDE, set(), None, False),  # no label
        (WIDE, {"widening-approved"}, labeled("dmitry-v-ko"), True),
        (WIDE, {"widening-approved"}, labeled("Dmitry-V-Ko"), True),  # logins are case-insensitive
        (WIDE, {"widening-approved"}, labeled("mallory"), False),  # not a code owner
        (WIDE, {"widening-approved"}, labeled("dmitry-v-ko", app={"slug": "claude"}), False),  # added by an app
        (WIDE, {"widening-approved"}, None, False),  # label present, event not found
        (WIDE, {"other"}, labeled("dmitry-v-ko", label="other"), False),
    ],
)
def test_decide(report: dict[str, Any], labels: set[str], event: Any, ok: bool) -> None:
    assert gate.decide(report, labels, event, OWNERS)[0] is ok


class FakeApi:
    def __init__(
        self,
        labels: list[str],
        events: list[dict[str, Any]],
        comments: list[dict[str, Any]],
        runs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.labels, self.events, self.comments = labels, events, comments
        self.runs = [pr_run()] if runs is None else runs
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: Any) -> Any:
        self.calls.append((method, path, body))
        base = path.split("?", 1)[0]
        if method == "GET" and base.endswith("/labels"):
            return [{"name": n} for n in self.labels]
        if method == "GET" and base.endswith("/events"):
            return self.events
        if method == "GET" and base.endswith("/comments"):
            return self.comments
        if method == "GET" and base.endswith("/actions/runs"):
            assert "event=pull_request" in path and f"head_sha={HEAD['sha']}" in path
            return {"workflow_runs": self.runs}
        if method == "DELETE":
            self.labels.remove("widening-approved")
        return None

    def deleted(self) -> bool:
        return ("DELETE", "/repos/o/r/issues/7/labels/widening-approved", None) in self.calls


def run(api: FakeApi, action: str, report: dict[str, Any]) -> int:
    event = {"action": action, "pull_request": {"number": 7, "head": HEAD}}
    result: int = gate.run(api, "o/r", event, report, markdown="<!-- gxg-policy-diff -->\nreport", owners=OWNERS)
    return result


def test_run_passes_with_owner_label_and_posts_comment() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [])
    assert run(api, "labeled", WIDE) == 0
    assert not api.deleted()
    method, path, body = api.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues/7/comments")
    assert "grants-widening: pass" in body["body"]


def test_run_fails_without_label_and_updates_own_comment() -> None:
    mine = {"id": 5, "user": {"login": "github-actions[bot]"}, "body": "<!-- gxg-policy-diff -->\nold"}
    forged = {"id": 6, "user": {"login": "mallory"}, "body": "<!-- gxg-policy-diff -->\nfake"}
    api = FakeApi([], [], [forged, mine])
    assert run(api, "opened", WIDE) == 1
    method, path, body = api.calls[-1]
    assert (method, path) == ("PATCH", "/repos/o/r/issues/comments/5")
    assert "grants-widening: FAIL" in body["body"]


# F2: the owner labelled commit A; the agent pushed B. Whatever event the next run has
# (the synchronize run may never have reached the gate), the old label must not approve B.
@pytest.mark.parametrize("action", ["synchronize", "labeled", "reopened", "unlabeled", "opened"])
def test_label_older_than_head_commit_is_removed_on_any_event(action: str) -> None:
    other = labeled("dmitry-v-ko", label="other", at=T_AFTER)
    api = FakeApi(["widening-approved", "other"], [labeled("dmitry-v-ko", at=T_BEFORE), other], [])
    assert run(api, action, WIDE) == 1
    assert api.deleted()
    assert "removed: the label" in api.calls[-1][2]["body"]


def test_label_after_the_head_commit_counts() -> None:
    # Earliest run for the SHA decides, not the latest (a `labeled` run is newer than the label).
    runs = [pr_run(T_PUSH), pr_run("2026-11-20T12:00:00Z")]
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_AFTER)], [], runs)
    assert run(api, "labeled", WIDE) == 0 and not api.deleted()


def test_force_push_after_the_label_makes_it_stale() -> None:
    # Force-push back to an older SHA whose runs predate the label.
    forced = {"event": "head_ref_force_pushed", "created_at": "2026-11-20T12:00:00Z"}
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_AFTER), forced], [])
    assert run(api, "synchronize", WIDE) == 1 and api.deleted()


def test_runs_of_other_prs_or_branches_do_not_date_the_head() -> None:
    # B was pushed to another branch / PR before the label; on this PR it arrives later.
    runs = [pr_run(T_BEFORE, branch="claude/other"), pr_run(T_BEFORE, prs=(3,)), pr_run(T_PUSH)]
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at="2026-11-20T09:30:00Z")], [], runs)
    assert run(api, "labeled", WIDE) == 1 and api.deleted()


def test_fast_forward_back_to_an_earlier_head_needs_a_new_label() -> None:
    # Cycle 2 finding 2: B ran at 09:00, force-push to A at 09:30, owner labels A at 10:00, fast-forward
    # A -> B at 11:00 (no force-push event). Only the 11:00 run dates B's current visit: the label is stale.
    forced = {"event": "head_ref_force_pushed", "created_at": "2026-11-20T09:30:00Z"}
    events = [forced, labeled("dmitry-v-ko", at="2026-11-20T10:00:00Z")]
    api = FakeApi(["widening-approved"], events, [], [pr_run(T_BEFORE), pr_run(T_AFTER)])
    assert run(api, "synchronize", WIDE) == 1 and api.deleted()
    # Same label, only the post-force-push run: still older than B's arrival.
    api = FakeApi(["widening-approved"], events, [], [pr_run(T_AFTER)])
    assert run(api, "synchronize", WIDE) == 1 and api.deleted()


def test_label_after_the_run_that_followed_the_force_push_counts() -> None:
    forced = {"event": "head_ref_force_pushed", "created_at": "2026-11-20T09:30:00Z"}
    events = [forced, labeled("dmitry-v-ko", at=T_AFTER)]
    api = FakeApi(["widening-approved"], events, [], [pr_run(T_BEFORE), pr_run(T_PUSH)])
    assert run(api, "labeled", WIDE) == 0 and not api.deleted()


def test_runs_from_another_head_repository_are_ignored() -> None:
    # A fork PR run has empty `pull_requests` and may share the branch name: the repository id decides.
    runs = [pr_run(T_BEFORE, prs=(), repo_id=2), pr_run(T_PUSH)]
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at="2026-11-20T09:30:00Z")], [], runs)
    assert run(api, "labeled", WIDE) == 1 and api.deleted()


def test_unknown_head_repository_fails_closed() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [])
    event = {"action": "labeled", "pull_request": {"number": 7, "head": {**HEAD, "repo": None}}}
    assert gate.run(api, "o/r", event, WIDE, markdown="m", owners=OWNERS) == 1 and api.deleted()


def test_unknown_arrival_fails_closed() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [], runs=[pr_run(sha="c" * 40)])
    assert run(api, "labeled", WIDE) == 1 and api.deleted()


def test_label_kept_without_widening() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_BEFORE)], [])
    assert run(api, "synchronize", NARROW) == 0
    assert not api.deleted()


# F1: push / workflow_dispatch on a branch: there is no PR and no label, widening is red.
@pytest.mark.parametrize(("report", "code"), [(WIDE, 1), (NARROW, 0)])
def test_no_pr_mode_fails_on_any_widening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], code: int
) -> None:
    r, md = tmp_path / "r.json", tmp_path / "r.md"
    r.write_text(json.dumps(report))
    md.write_text("<!-- gxg-policy-diff -->\nreport\n")
    for var in ("GITHUB_EVENT_PATH", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)  # no API, no event: the decision is the report alone
    assert gate.main(["--report", str(r), "--markdown", str(md), "--no-pr"]) == code


def test_main_reports_missing_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, md, owners = tmp_path / "r.json", tmp_path / "r.md", tmp_path / "CODEOWNERS"
    report.write_text(json.dumps(WIDE))
    md.write_text("x")
    owners.write_text("* @dmitry-v-ko\n")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert gate.main(["--report", str(report), "--markdown", str(md), "--codeowners", str(owners)]) == 2
