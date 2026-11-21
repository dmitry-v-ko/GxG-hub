"""Workflow invariants that a local test can check: GR-13a schedule, F1/F2 wiring of grants-widening."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
WF: dict[Any, Any] = yaml.safe_load(CI.read_text())
ON = WF[True]  # PyYAML reads the key `on` as a boolean


def cron_hours(field: str) -> list[int]:
    if field == "*":
        return list(range(24))
    if field.startswith("*/"):
        return list(range(0, 24, int(field[2:])))
    return sorted(int(h) for h in field.split(","))


def test_cron_hours_parser() -> None:
    assert cron_hours("*/6") == [0, 6, 12, 18]
    assert cron_hours("3,15") == [3, 15]


def test_gr_13a_bundle_is_resigned_at_least_every_12_hours() -> None:
    gaps = []
    for entry in ON["schedule"]:
        minute, hour, dom, month, dow = entry["cron"].split()
        assert (dom, month, dow) == ("*", "*", "*") and minute.isdigit()
        hours = cron_hours(hour)
        gaps.append(max((b - a) % 24 or 24 for a, b in zip(hours, hours[1:] + hours[:1], strict=True)))
    assert gaps and min(gaps) <= 12
    cond = WF["jobs"]["bundle"]["if"]
    assert "'schedule'" in cond and "refs/heads/main" in cond


def test_grants_widening_runs_its_gate_on_every_event() -> None:
    # F1: no step that passes without a decision; the job script decides for every event.
    steps = WF["jobs"]["grants-widening"]["steps"]
    gate = [s for s in steps if "grants_widening.sh" in s.get("run", "")]
    assert len(gate) == 1 and "if" not in gate[0]
    assert all("if" not in s for s in steps)
    assert WF["jobs"]["grants-widening"]["permissions"]["actions"] == "read"
    assert "grants-widening" in WF["jobs"]["ci-ok"]["needs"]


def test_pull_request_runs_are_never_cancelled() -> None:
    # F2: every head commit gets a run that reaches the gate.
    conc = WF["concurrency"]
    assert "github.run_id" in conc["group"] and "event_name == 'pull_request'" in conc["group"]
    assert "github.event_name != 'pull_request'" in conc["cancel-in-progress"]


def test_base_change_reruns_the_gate_and_the_event_base_is_not_used() -> None:
    # Cycle 2 finding 1: changing the PR base sends `edited`; the job diffs against main, not base.sha.
    assert "edited" in ON["pull_request"]["types"]
    gate = next(s for s in WF["jobs"]["grants-widening"]["steps"] if "grants_widening.sh" in s.get("run", ""))
    assert gate["env"]["PR_HEAD"] == "${{ github.event.pull_request.head.sha }}"
    assert "PR_BASE" not in gate["env"]


def test_grants_widening_checkout_has_history() -> None:
    # merge-base with main needs the full history and refs/remotes/origin/main.
    checkout = WF["jobs"]["grants-widening"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["fetch-depth"] == 0


def test_grants_widening_runs_no_pr_code() -> None:
    # Cycle 3 finding 1: the gate comes from main; no step installs or runs the PR's project.
    steps = WF["jobs"]["grants-widening"]["steps"]
    assert [s.get("uses", "").split("@")[0] for s in steps] == ["actions/checkout", "astral-sh/setup-uv", ""]
    run = steps[2]["run"]
    assert "uv " not in run and 'git worktree add --detach "$RUNNER_TEMP/gate" refs/remotes/origin/main' in run
