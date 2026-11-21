"""AC-M2-04: `grants-widening` is red while the diff has widening and no code owner added `widening-approved`."""

from __future__ import annotations

import importlib.util
import json
import re
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
        assert path.startswith("/repos/o/r/"), path
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

    def fake_run(api: Any, repo: str, ev: Any, report: Any, *, markdown: str, owners: set[str]) -> int:
        assert api is the_api
        seen["run"] = (repo, ev, report, markdown, owners)
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
    # The module docstring is the description, kept verbatim (RawDescriptionHelpFormatter).
    assert "Usage (both modes also take --gate-paths FILE):\n  widening_gate.py --report diff.json" in out


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


def test_changed_gate_paths_count_as_widening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    # Cycle 3 finding 1: a PR that edits the gate needs the label even with no policy widening.
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    r, md, paths = tmp_path / "r.json", tmp_path / "r.md", tmp_path / "p.txt"
    r.write_text(json.dumps(NARROW))
    md.write_text("<!-- m -->\nx\n")
    args = ["--report", str(r), "--markdown", str(md), "--no-pr", "--gate-paths", str(paths)]
    paths.write_text("")
    assert gate.main(args) == 0
    paths.write_text("scripts/ci/grants_widening.sh\n\nuv.lock\n")
    assert gate.main(args) == 1
    assert "2 widening change(s) outside a pull request" in capsys.readouterr().out
    text = summary.read_text()
    assert "### Gate files changed (2, need the `widening-approved` label)" in text
    assert "| `scripts/ci/grants_widening.sh` |\n| `uv.lock` |\n" in text
    report, markdown = gate.with_gate_paths(NARROW, "m", ["a b"])
    assert report["widening"] == [{"tenant": None, "kind": "gate_path", "change": "changed", "path": "a b"}]
    assert report["narrowing"] == NARROW["narrowing"] and NARROW["widening"] == []
    assert markdown.startswith("m\n### Gate files")
    assert gate.with_gate_paths(NARROW, "m", []) == (NARROW, "m")
