"""AC-M2-04: `grants-widening` is red while the diff has widening and no code owner added `widening-approved`."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "widening_gate.py"
WIDE: dict[str, Any] = {"widening": [{"kind": "grant", "change": "added"}], "narrowing": []}
NARROW: dict[str, Any] = {"widening": [], "narrowing": [{"kind": "grant", "change": "removed"}]}
OWNERS = {"dmitry-v-ko"}
GATE_FINDING: dict[str, Any] = {
    "tenant": None, "kind": "gate", "rule": "gate.self", "path": "scripts/ci/grants_widening.sh",
    "detail": "gate file changed (abc)",
}  # fmt: skip


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
        branch_runs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.labels, self.events, self.comments = labels, events, comments
        self.runs = [pr_run()] if runs is None else runs
        self.branch_runs = branch_runs or []
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: Any) -> Any:
        assert path.startswith("/repos/o/r/"), path
        self.calls.append((method, path, body))
        base = path.split("?", 1)[0]
        if method == "GET" and base.endswith("/labels"):
            return [{"name": n} for n in self.labels]
        if method == "GET" and base.endswith("/events"):
            return self.events
        if method == "GET" and base.endswith("/comments"):
            return self.comments
        if method == "GET" and base.endswith("/actions/runs") and "branch=" in path:
            assert path.endswith(f"?event=pull_request&branch={REF.replace('/', '%2F')}&per_page=100"), path
            return {"workflow_runs": self.branch_runs}
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


# Exact outputs and I/O (DoD-4: mutmut kills message, pagination and wiring mutants).
@pytest.mark.parametrize(
    ("report", "labels", "event", "owners", "msg"),
    [
        (NARROW, set(), None, OWNERS, "no widening in the policy diff"),
        (WIDE, set(), None, OWNERS, "1 widening change(s): a code owner must add the `widening-approved` label"),
        (
            WIDE,
            {"widening-approved"},
            None,
            OWNERS,
            "`widening-approved` is on the PR but its `labeled` event was not found",
        ),
        (
            WIDE,
            {"widening-approved"},
            labeled("x", app={"slug": "claude"}),
            OWNERS,
            "`widening-approved` was added via the GitHub App 'claude'; the owner must add it by hand",
        ),
        (
            WIDE,
            {"widening-approved"},
            labeled("Mallory"),
            OWNERS,
            "`widening-approved` was added by @mallory, who is not in CODEOWNERS (['dmitry-v-ko'])",
        ),
        (
            WIDE,
            {"widening-approved"},
            labeled("Dmitry-V-Ko"),
            OWNERS,
            "1 widening change(s) approved by @dmitry-v-ko (`widening-approved`)",
        ),
        # An event without an actor login is nobody, whatever the owners are called.
        (
            WIDE,
            {"widening-approved"},
            {**labeled("x"), "actor": {}},
            {"none", "xxxx"},
            "`widening-approved` was added by @, who is not in CODEOWNERS (['none', 'xxxx'])",
        ),
    ],
)
def test_decide_messages(report: dict[str, Any], labels: set[str], event: Any, owners: set[str], msg: str) -> None:
    assert gate.decide(report, labels, event, owners)[1] == msg


def test_codeowners_comment_starts_at_the_first_hash() -> None:
    assert gate.codeowners("/x @a # @b # @c\n") == {"a"}


def test_paged_reads_until_a_short_page_and_stops_after_99() -> None:
    calls: list[str] = []

    def api(method: str, path: str, body: Any) -> Any:
        calls.append(path)
        return [{"n": len(calls)}] * (100 if len(calls) == 1 else 3)

    assert gate.paged(api, "/x") == [{"n": 1}] * 100 + [{"n": 2}] * 3
    assert calls == ["/x?per_page=100&page=1", "/x?per_page=100&page=2"]
    calls.clear()

    def full(method: str, path: str, body: Any) -> Any:
        calls.append(path)
        return [{}] * 100

    assert len(gate.paged(full, "/y")) == 9900
    assert calls[-1] == "/y?per_page=100&page=99"


def test_comments_without_user_or_body_are_skipped() -> None:
    api = FakeApi([], [], [{"id": 1}, {"id": 2, "user": {"login": "github-actions[bot]"}}])
    gate.upsert_comment(api, "/repos/o/r/issues/7", "new", "<!-- m -->")
    assert api.calls[-1] == ("POST", "/repos/o/r/issues/7/comments", {"body": "new"})


def test_comment_marker_is_the_first_markdown_line_only() -> None:
    mine = {"id": 5, "user": {"login": "github-actions[bot]"}, "body": "<!-- gxg-policy-diff -->\nold"}
    other = {"id": 4, "user": {"login": "github-actions[bot]"}, "body": "<!-- other -->\nx"}
    api = FakeApi([], [], [other, mine])
    event = {"action": "opened", "pull_request": {"number": 7, "head": HEAD}}
    gate.run(api, "o/r", event, NARROW, markdown="<!-- gxg-policy-diff -->\nline 2\nline 3", owners=OWNERS)
    assert api.calls[-1][:2] == ("PATCH", "/repos/o/r/issues/comments/5")
    assert api.calls[-1][2]["body"] == (
        "<!-- gxg-policy-diff -->\nline 2\nline 3\n**grants-widening: pass** — no widening in the policy diff\n"
    )


def test_missing_workflow_runs_fails_closed() -> None:
    assert gate.head_arrival(lambda m, p, b: {}, "o/r", {"number": 7, "head": HEAD}, []) is None


def test_run_at_the_force_push_instant_does_not_date_the_head() -> None:
    forced = {"event": "head_ref_force_pushed", "created_at": T_PUSH}
    api = FakeApi([], [], [], [pr_run(T_PUSH)])
    assert gate.head_arrival(api, "o/r", {"number": 7, "head": HEAD}, [forced]) is None
    assert api.calls[0][1] == f"/repos/o/r/actions/runs?event=pull_request&head_sha={SHA}&per_page=100"


def test_label_at_the_arrival_instant_is_stale() -> None:
    why = gate.stale(labeled("dmitry-v-ko", at=T_PUSH), gate._ts(T_PUSH))
    assert why == f"the label ({T_PUSH}) is older than the head commit on the PR (2026-11-20T10:00:00+00:00)"
    assert gate.stale(labeled("dmitry-v-ko"), None) == "the arrival time of the head commit is unknown"


def test_removed_label_note_and_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("before\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_BEFORE)], [])
    assert run(api, "synchronize", WIDE) == 1
    note = (
        f"`widening-approved` was removed: the label ({T_BEFORE}) is older than the head commit on the PR"
        " (2026-11-20T10:00:00+00:00). Review the new commits and approve again.\n\n"
    )
    msg = "1 widening change(s): a code owner must add the `widening-approved` label"
    body = f"<!-- gxg-policy-diff -->\nreport\n**grants-widening: FAIL** — {note}{msg}\n"
    assert api.calls[-1][2]["body"] == body
    assert summary.read_text(encoding="utf-8") == "before\n" + body
    assert capsys.readouterr().out == f"::error::{note}{msg}\n"


def test_passing_run_prints_without_error_prefix(capsys: Any) -> None:
    api = FakeApi([], [], [])
    assert run(api, "opened", NARROW) == 0
    assert capsys.readouterr().out == "no widening in the policy diff\n"


def test_comment_failure_is_a_warning_not_a_decision(capsys: Any) -> None:
    def api(method: str, path: str, body: Any) -> Any:
        if path.endswith("/comments") or "/comments?" in path:
            raise urllib.error.HTTPError(path, 403, "forbidden", Message(), None)
        return []

    event = {"action": "opened", "pull_request": {"number": 7, "head": HEAD}}
    assert gate.run(api, "o/r", event, NARROW, markdown="m", owners=OWNERS) == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning::could not post the diff comment: HTTP Error 403: forbidden\n")


@pytest.mark.parametrize(
    ("report", "code", "out"),
    [
        (NARROW, 0, "no widening against the merge-base with main\n"),
        (
            WIDE,
            1,
            "::error::1 widening change(s) outside a pull request: widening is approved only on a PR"
            " with the `widening-approved` label from a code owner\n",
        ),
    ],
)
def test_no_pr_output(  # noqa: PLR0917 - pytest fixtures and parameters
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, report: dict[str, Any], code: int, out: str
) -> None:
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    r, md = tmp_path / "r.json", tmp_path / "r.md"
    r.write_text(json.dumps(report), encoding="utf-8")
    md.write_text("<!-- m -->\nёжик\n", encoding="utf-8")
    assert gate.main(["--report", str(r), "--markdown", str(md), "--no-pr"]) == code
    assert capsys.readouterr().out == out
    verdict = "pass" if code == 0 else "FAIL"
    assert (
        summary.read_text(encoding="utf-8")
        == f"<!-- m -->\nёжик\n\n**grants-widening: {verdict}** — {out.removeprefix('::error::')}"
    )


def test_main_pr_mode_wires_environment_files_and_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    files = {"r.json": json.dumps(WIDE), "r.md": "<!-- m -->\nёж", "CODEOWNERS": "* @Owner-Ü\n"}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    event = {"action": "opened", "pull_request": {"number": 7, "head": HEAD}, "note": "ё"}
    (tmp_path / "event.json").write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    seen: dict[str, Any] = {}

    the_api = FakeApi([], [], [])

    def fake_api(token: str, base: str) -> Any:
        seen["api"] = (token, base)
        return the_api

    def fake_run(api: Any, repo: str, ev: Any, report: Any, *, markdown: str, owners: set[str], carry: Any) -> int:
        assert api is the_api
        seen["run"] = (repo, ev, report, markdown, owners)
        seen["carry"] = carry
        return 5

    monkeypatch.setattr(gate, "github_api", fake_api)
    monkeypatch.setattr(gate, "run", fake_run)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "event.json"))
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    args = ["--report", str(tmp_path / "r.json"), "--markdown", str(tmp_path / "r.md")]
    assert gate.main([*args, "--codeowners", str(tmp_path / "CODEOWNERS")]) == 5
    assert seen["api"] == ("tok", "https://api.github.com")
    assert seen["run"] == ("o/r", event, WIDE, "<!-- m -->\nёж", {"owner-ü"})
    assert seen["carry"] is None  # without --repo an older label never carries over
    gate.main([*args, "--codeowners", str(tmp_path / "CODEOWNERS"), "--repo", str(tmp_path)])
    assert callable(seen["carry"])
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.invalid/api/v3")
    gate.main([*args, "--codeowners", str(tmp_path / "CODEOWNERS")])
    assert seen["api"] == ("tok", "https://ghe.example.invalid/api/v3")
    # Without --codeowners and without --no-pr: usage error.
    with pytest.raises(SystemExit) as exc:
        gate.main(args)
    assert exc.value.code == 2
    assert "error: --codeowners is required without --no-pr" in capsys.readouterr().err
    for missing in ("--report", "--markdown"):
        i = args.index(missing)
        with pytest.raises(SystemExit):
            gate.main(args[:i] + args[i + 2 :] + ["--no-pr"])


def test_main_decision_error_is_exit_2(tmp_path: Path, capsys: Any) -> None:
    assert gate.main(["--report", str(tmp_path / "absent.json"), "--markdown", str(tmp_path / "x"), "--no-pr"]) == 2
    assert capsys.readouterr().out.startswith("::error::grants-widening could not decide: FileNotFoundError(")


def test_help_lists_modes(capsys: Any) -> None:
    with pytest.raises(SystemExit):
        gate.main(["--help"])
    out = capsys.readouterr().out
    assert re.search(r"  CODEOWNERS of the PR base commit \(PR mode\)\n", out)
    assert re.search(r"  not a pull_request event: any widening fails\n", out)
    assert re.search(r"  gate_diff.py JSON: each finding counts as widening\n", out)
    # The module docstring is the description, kept verbatim (RawDescriptionHelpFormatter).
    assert "Usage (both modes also take --gate-findings FILE; PR mode also --repo DIR --main REF):\n" in out


class FakeResponse:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


def test_github_api_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[urllib.request.Request, Any]] = []

    def urlopen(req: urllib.request.Request, timeout: Any = None) -> FakeResponse:
        sent.append((req, timeout))
        return FakeResponse(b'{"ok": 1}' if req.get_method() == "GET" else b"")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    api = gate.github_api("tok", "https://api.example.invalid")
    assert api("GET", "/repos/o/r", None) == {"ok": 1}
    assert api("POST", "/repos/o/r/issues/7/comments", {"body": "ё"}) is None
    (get, t1), (post, t2) = sent
    assert (get.full_url, get.get_method(), get.data, t1) == ("https://api.example.invalid/repos/o/r", "GET", None, 30)
    assert dict(get.header_items()) == {
        "Authorization": "Bearer tok",
        "Accept": "application/vnd.github+json",
        "X-github-api-version": "2022-11-28",
    }
    assert (post.get_method(), post.data, t2) == ("POST", json.dumps({"body": "ё"}).encode(), 30)
    api("PATCH", "/x", {"a": 1})
    api("DELETE", "/y", None)
    assert [r.get_method() for r, _ in sent[2:]] == ["PATCH", "DELETE"]


def test_gate_findings_count_as_widening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    # A PR with a control change (gate_diff.py) needs the label even with no policy widening.
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    r, md, found = tmp_path / "r.json", tmp_path / "r.md", tmp_path / "f.json"
    r.write_text(json.dumps(NARROW))
    md.write_text("<!-- m -->\nx\n")
    args = ["--report", str(r), "--markdown", str(md), "--no-pr", "--gate-findings", str(found)]
    found.write_text("[]")
    assert gate.main(args) == 0
    two = [GATE_FINDING, {**GATE_FINDING, "rule": "deps.new", "path": "uv.lock", "detail": "a|b`c"}]
    found.write_text(json.dumps(two))
    assert gate.main(args) == 1
    assert "2 widening change(s) outside a pull request" in capsys.readouterr().out
    text = summary.read_text()
    assert "### Control changes (2, need the `widening-approved` label)" in text
    assert "| gate.self | `scripts/ci/grants_widening.sh` | gate file changed (abc) |\n" in text
    assert "| deps.new | `uv.lock` | a\\|b'c |\n" in text
    report, markdown = gate.with_gate_findings(NARROW, "m", [GATE_FINDING])
    assert report["widening"] == [GATE_FINDING]
    assert report["narrowing"] == NARROW["narrowing"] and NARROW["widening"] == []
    assert markdown.startswith("m\n### Control changes")
    assert gate.with_gate_findings(NARROW, "m", []) == (NARROW, "m")


# Owner decision 2026-11-26: a merge of main does not reset the label. An older label still covers the head
# when the head has no widening that the head at the time of the label (A) did not have.
SHA_A = "a" * 40
FIND_1 = {"kind": "gate", "rule": "lint.exception", "path": "a.py", "detail": "x  # noqa"}
FIND_2 = {"kind": "gate", "rule": "secrets.new", "path": ".github/workflows/ci.yml", "detail": "NEW"}


def carry_run(
    approved: list[dict[str, Any]] | None, head: list[dict[str, Any]], branch_runs: list[dict[str, Any]]
) -> tuple[int, FakeApi, list[str]]:
    asked: list[str] = []

    def carry(sha: str) -> list[dict[str, Any]] | None:
        asked.append(sha)
        return approved

    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_BEFORE)], [], branch_runs=branch_runs)
    event = {"action": "synchronize", "pull_request": {"number": 7, "head": HEAD}}
    report = {"widening": head, "narrowing": []}
    code = gate.run(api, "o/r", event, report, markdown="<!-- m -->\nr", owners=OWNERS, carry=carry)
    return code, api, asked


A_RUN = [pr_run("2026-11-20T08:00:00Z", sha=SHA_A)]


def test_merge_of_main_keeps_the_label() -> None:
    code, api, asked = carry_run([FIND_1, FIND_2], [FIND_2, FIND_1], A_RUN)
    assert code == 0 and not api.deleted() and asked == [SHA_A]
    body = api.calls[-1][2]["body"]
    assert f"`widening-approved` from {SHA_A[:12]} still covers the head: no new widening since the label." in body
    assert "approved by @dmitry-v-ko" in body


def test_new_widening_after_the_label_removes_it() -> None:
    code, api, _ = carry_run([FIND_1], [FIND_1, FIND_2], A_RUN)
    assert code == 1 and api.deleted()
    body = api.calls[-1][2]["body"]
    assert f"and 1 widening change(s) since the label on {SHA_A[:12]}: NEW. Review the new commits" in body


def test_a_second_equal_finding_is_new() -> None:
    assert carry_run([FIND_1], [FIND_1, FIND_1], A_RUN)[0] == 1
    assert gate.uncovered([FIND_1, FIND_1, FIND_2], [FIND_1, FIND_2]) == [FIND_1]


def test_less_widening_than_at_the_label_is_covered() -> None:
    assert carry_run([FIND_1, FIND_2], [FIND_1], A_RUN)[0] == 0


def test_unknown_widening_of_the_labelled_head_fails_closed() -> None:
    code, api, _ = carry_run(None, [FIND_1], A_RUN)
    assert code == 1 and api.deleted()
    assert (
        f"the widening of {SHA_A[:12]} (the head at the time of the label) could not be computed"
        in (api.calls[-1][2]["body"])
    )


def test_unknown_head_at_the_label_fails_closed() -> None:
    # Only runs after the label, or of other branches, PRs or head repositories: no head at the label time.
    runs = [
        pr_run(T_PUSH, sha=SHA_A),
        pr_run("2026-11-20T08:00:00Z", sha=SHA_A, branch="claude/other"),
        pr_run("2026-11-20T08:00:00Z", sha=SHA_A, prs=(3,)),
        pr_run("2026-11-20T08:00:00Z", sha=SHA_A, repo_id=2),
    ]
    code, api, asked = carry_run([FIND_1], [FIND_1], runs)
    assert code == 1 and api.deleted() and asked == []
    assert "the head commit at the time of the label is unknown" in api.calls[-1][2]["body"]


def test_head_at_takes_the_latest_run_not_after_the_label() -> None:
    runs = [
        pr_run("2026-11-20T07:00:00Z", sha="1" * 40),
        pr_run("2026-11-20T08:30:00Z", sha="2" * 40),
        pr_run(T_BEFORE, sha="3" * 40),  # at the label instant: the label saw it
        pr_run(T_PUSH, sha="4" * 40),
        pr_run("2026-11-20T08:45:00Z", sha="5" * 40, prs=()),  # a run without PR links still counts
    ]
    api = FakeApi([], [], [], branch_runs=runs)
    pr = {"number": 7, "head": HEAD}
    assert gate.head_at(api, "o/r", pr, gate._ts(T_BEFORE)) == "3" * 40
    assert gate.head_at(api, "o/r", pr, gate._ts("2026-11-20T08:40:00Z")) == "2" * 40
    assert gate.head_at(api, "o/r", {"number": 7, "head": {**HEAD, "repo": None}}, gate._ts(T_BEFORE)) is None


def test_without_carry_an_older_label_is_removed_as_before() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko", at=T_BEFORE)], [], branch_runs=A_RUN)
    assert run(api, "synchronize", WIDE) == 1 and api.deleted()


def test_carry_from_computes_the_widening_of_a_past_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end on a real repo: the labelled head A, a merge of main, then a new exception."""

    def g(*args: str) -> str:
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}  # fmt: skip
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def put(name: str, text: str, msg: str) -> str:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        g("add", "-A")
        g("commit", "-qm", msg)
        return g("rev-parse", "HEAD")

    repo = tmp_path / "repo"
    repo.mkdir()
    g("init", "-q", "-b", "main")
    put("policies/README", "p\n", "base")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    g("checkout", "-qb", "claude/x")
    a = put("a.py", "x = 1  # noqa: E501\n", "A")
    g("checkout", "-q", "main")
    put("lib.py", "y = 1  # noqa: E741\n", "main moves on")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    g("checkout", "-q", "claude/x")
    g("merge", "-q", "--no-edit", "main")
    merged = g("rev-parse", "HEAD")
    later = put("b.py", "z = 1  # type: ignore\n", "new exception")
    (tmp_path / "runner").mkdir()
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    carry = gate.carry_from(repo, "refs/remotes/origin/main")
    approved = carry(a)
    assert approved is not None
    assert [(f["rule"], f["path"]) for f in approved] == [("lint.exception", "a.py")]
    assert gate.uncovered(carry(merged) or [], approved) == []
    new = gate.uncovered(carry(later) or [], approved)
    assert [(f["rule"], f["path"]) for f in new] == [("lint.exception", "b.py")]
    assert carry("f" * 40) is None  # a SHA the checkout does not have: unknown, fail closed
    assert (tmp_path / "runner" / f"carry-{a}" / "head" / "a.py").exists()


# Exact outputs of the carry-over and the control-change table (DoD-4).
def test_control_change_table_exact() -> None:
    two = [GATE_FINDING, {**GATE_FINDING, "rule": "lint.config", "path": "a|b", "detail": "line\n`two`"}]
    report, markdown = gate.with_gate_findings(NARROW, "m", two)
    assert report == {"widening": two, "narrowing": NARROW["narrowing"]}
    assert markdown == (
        "m\n### Control changes (2, need the `widening-approved` label)\n\n"
        "| rule | path | detail |\n| --- | --- | --- |\n"
        "| gate.self | `scripts/ci/grants_widening.sh` | gate file changed (abc) |\n"
        "| lint.config | `a\\|b` | line 'two' |\n"
    )


def test_a_finding_is_the_same_whatever_its_key_order() -> None:
    assert gate.uncovered([{"b": 1, "a": 2}], [{"a": 2, "b": 1}]) == []


def test_carried_reason_lists_up_to_five_new_findings() -> None:
    new = [{"detail": f"d{i}"} for i in range(4)] + [{"path": "p"}, {"kind": "k"}, {"detail": "d6"}]
    code, api, _ = carry_run([], new, A_RUN)
    assert code == 1
    assert (
        f"and 7 widening change(s) since the label on {SHA_A[:12]}: d0; d1; d2; d3; p. Review"
        in (api.calls[-1][2]["body"])
    )
    code, api, _ = carry_run([], [{"kind": "k"}], [])
    assert "`widening-approved` was removed: the label (2026-11-20T09:00:00Z) is older than the head commit on the PR" \
        " (2026-11-20T10:00:00+00:00), and the head commit at the time of the label is unknown. Review" in (
        api.calls[-1][2]["body"]
    )  # fmt: skip


def test_carry_warns_when_the_widening_is_not_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    carry = gate.carry_from(tmp_path / "absent", "refs/remotes/origin/main")
    assert carry("f" * 40) is None
    out = capsys.readouterr().out
    assert out == f"::warning::widening of {'f' * 40} not computed: FileNotFoundError(2, 'No such file or directory')\n"


def test_head_at_without_runs_and_with_a_tie() -> None:
    pr = {"number": 7, "head": HEAD}
    assert gate.head_at(lambda m, p, b: {}, "o/r", pr, gate._ts(T_AFTER)) is None
    # Two runs at the same instant: the first listed (GitHub lists the newest first) wins.
    api = FakeApi([], [], [], branch_runs=[pr_run(T_PUSH, sha="1" * 40), pr_run(T_PUSH, sha="2" * 40)])
    assert gate.head_at(api, "o/r", pr, gate._ts(T_AFTER)) == "1" * 40


def test_main_passes_repo_and_main_to_the_carry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    files = {"r.json": json.dumps(WIDE), "r.md": "m", "CODEOWNERS": "* @o\n", "event.json": "{}"}
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    seen: list[Any] = []

    def carry_from(repo: Path, main: str) -> str:
        seen.append((repo, main))
        return "carry"

    def run(*args: Any, carry: Any, **kw: Any) -> int:
        seen.append(carry)
        return 0

    monkeypatch.setattr(gate, "carry_from", carry_from)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "github_api", lambda token, base: None)
    for var, value in (("GITHUB_EVENT_PATH", str(tmp_path / "event.json")), ("GITHUB_TOKEN", "t"),
                       ("GITHUB_REPOSITORY", "o/r")):  # fmt: skip
        monkeypatch.setenv(var, value)
    args = ["--report", str(tmp_path / "r.json"), "--markdown", str(tmp_path / "r.md")]
    args += ["--codeowners", str(tmp_path / "CODEOWNERS"), "--repo", "checkout"]
    assert gate.main(args) == 0
    assert seen == [(Path("checkout"), "refs/remotes/origin/main"), "carry"]
    seen.clear()
    gate.main([*args, "--main", "refs/x"])
    assert seen == [(Path("checkout"), "refs/x"), "carry"]
    with pytest.raises(SystemExit):
        gate.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--repo REPO checkout with the PR history (PR mode): lets an older label carry over" in out
    assert "--main MAIN main ref for --repo" in out
