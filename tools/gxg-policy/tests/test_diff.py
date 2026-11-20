"""AC-M2-03: diff marks added (subject, action, resource) pairs and new auto_approve as widening."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gxg_policy.diff import _Diff, diff, markdown

HERE = Path(__file__).parent
CASES = sorted(p for p in (HERE / "diff").iterdir() if p.is_dir())


def test_at_least_six_fixture_pairs() -> None:
    assert len(CASES) >= 6


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_ac_m2_03_report_matches_golden(case: Path) -> None:
    assert diff(case / "base", case / "head") == json.loads((case / "expected.json").read_text())


def test_ac_m2_03_core_classification() -> None:
    added = diff(HERE / "diff/01-grant-resource-added/base", HERE / "diff/01-grant-resource-added/head")
    assert [(e["kind"], e["change"], e["resource"]) for e in added["widening"]] == [
        ("grant", "added", "kb:kb-main/collection/sales")
    ]
    assert added["narrowing"] == []
    removed = diff(HERE / "diff/01-grant-resource-added/head", HERE / "diff/01-grant-resource-added/base")
    assert removed["widening"] == [] and [e["change"] for e in removed["narrowing"]] == ["removed"]
    auto = diff(HERE / "diff/04-auto-approve-added/base", HERE / "diff/04-auto-approve-added/head")
    assert {"rule": "board-auto", "change": "added", "decision": "auto_approve"}.items() <= auto["widening"][1].items()


def _changes(case: str) -> dict[str, set[tuple[str, str]]]:
    r = diff(HERE / "diff" / case / "base", HERE / "diff" / case / "head")
    return {side: {(e["rule"], e["change"]) for e in r[side]} for side in ("widening", "narrowing")}


def test_group_membership_in_approval_rules_is_widening() -> None:
    # Only groups changed: a new member of an auto_approve requester group and of an approvers group.
    assert _changes("07-approval-groups") == {
        "widening": {("tg-partner-auto", "requesters_added"), ("crm-two-person", "approvers_added")},
        "narrowing": set(),
    }


def test_direct_rule_edits() -> None:
    assert _changes("08-approval-match-approvers") == {
        "widening": {("kb-auto", "match_changed"), ("msg", "approvers_added")},
        "narrowing": {("hermes-auto", "requesters_removed")},
    }


def test_markdown_golden() -> None:
    case = HERE / "diff/06-mixed"
    assert markdown(diff(case / "base", case / "head")) == (case / "expected.md").read_text()


def test_missing_base_means_everything_is_new(tmp_path: Path) -> None:
    report = diff(tmp_path / "absent", HERE / "good/policies")
    assert report["widening"] and all(e["change"] in ("added", "tenant_added") for e in report["widening"])


def test_cli_writes_reports_and_rejects_invalid_input(tmp_path: Path) -> None:
    case = CASES[0]
    out, md = tmp_path / "d.json", tmp_path / "d.md"
    cmd = [sys.executable, "-m", "gxg_policy.cli", "diff", str(case / "base"), str(case / "head")]
    r = subprocess.run([*cmd, "--json", str(out), "--md", str(md)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text()) == json.loads((case / "expected.json").read_text())
    assert "**widening: 1**" in md.read_text()
    bad = tmp_path / "bad"
    shutil.copytree(case / "head", bad)
    (bad / "tenants/t-alpha.yaml").write_text("tenant: t-alpha\n")
    r = subprocess.run([*cmd[:-1], str(bad)], capture_output=True, text=True, check=False)
    assert r.returncode == 2 and "invalid policies" in r.stderr


def test_star_means_everyone_in_approvers_and_requesters() -> None:
    d = _Diff()
    d.sets({"rule": "r"}, "approvers", ["*"], ["oidc:x"])  # any approver -> one: narrowing only
    d.sets({"rule": "s"}, "approvers", ["oidc:x"], ["*"])  # one -> any: widening only
    assert [(e["rule"], e["change"]) for e in d.widening] == [("s", "approvers_added")]
    assert [(e["rule"], e["change"]) for e in d.narrowing] == [("r", "approvers_removed")]
