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
    spec = importlib.util.spec_from_file_location("widening_gate_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = load_gate()


def labeled(login: str, event: str = "labeled", label: str = "widening-approved", app: Any = None) -> dict[str, Any]:
    return {"event": event, "label": {"name": label}, "actor": {"login": login}, "performed_via_github_app": app}


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
    def __init__(self, labels: list[str], events: list[dict[str, Any]], comments: list[dict[str, Any]]) -> None:
        self.labels, self.events, self.comments = labels, events, comments
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
        if method == "DELETE":
            self.labels.remove("widening-approved")
        return None


def run(api: FakeApi, action: str, report: dict[str, Any]) -> int:
    event = {"action": action, "pull_request": {"number": 7}}
    result: int = gate.run(api, "o/r", event, report, markdown="<!-- gxg-policy-diff -->\nreport", owners=OWNERS)
    return result


def test_run_passes_with_owner_label_and_posts_comment() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [])
    assert run(api, "labeled", WIDE) == 0
    method, path, body = api.calls[-1]
    assert (method, path) == ("POST", "/repos/o/r/issues/7/comments")
    assert "grants-widening: pass" in body["body"]


def test_run_fails_without_label_and_updates_own_comment() -> None:
    mine = {"id": 5, "user": {"login": "github-actions[bot]"}, "body": "<!-- gxg-policy-diff --> old"}
    forged = {"id": 6, "user": {"login": "mallory"}, "body": "<!-- gxg-policy-diff --> fake"}
    api = FakeApi([], [], [forged, mine])
    assert run(api, "opened", WIDE) == 1
    method, path, body = api.calls[-1]
    assert (method, path) == ("PATCH", "/repos/o/r/issues/comments/5")
    assert "grants-widening: FAIL" in body["body"]


def test_new_push_with_widening_removes_the_approval() -> None:
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [])
    assert run(api, "synchronize", WIDE) == 1
    assert ("DELETE", "/repos/o/r/issues/7/labels/widening-approved", None) in api.calls
    # Without widening a new push keeps the label and passes.
    api = FakeApi(["widening-approved"], [labeled("dmitry-v-ko")], [])
    assert run(api, "synchronize", NARROW) == 0
    assert not [c for c in api.calls if c[0] == "DELETE"]


def test_main_reports_missing_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, md, owners = tmp_path / "r.json", tmp_path / "r.md", tmp_path / "CODEOWNERS"
    report.write_text(json.dumps(WIDE))
    md.write_text("x")
    owners.write_text("* @dmitry-v-ko\n")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert gate.main(["--report", str(report), "--markdown", str(md), "--codeowners", str(owners)]) == 2
