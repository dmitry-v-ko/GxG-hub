"""AC-M2-04: the grants-widening job diffs against main; outside pull_request it fails closed on widening.

Runs scripts/ci/grants_widening.sh in a throw-away git repo: main has the base policies, the branch
has the head policies, `origin/main` is a remote-tracking ref as actions/checkout(fetch-depth: 0) leaves it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "scripts" / "ci" / "grants_widening.sh"
CASE = ROOT / "tools" / "gxg-policy" / "tests" / "diff" / "01-grant-resource-added"
MANIFESTS = ROOT / "tools" / "gxg-policy" / "tests" / "diff" / "10-manifest-tools"
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
STEP = next(s for s in WORKFLOW["jobs"]["grants-widening"]["steps"] if "grants_widening.sh" in s.get("run", ""))
GATE_FILES = ("scripts/ci/grants_widening.sh", "scripts/ci/widening_gate.py")
# `uv run [--locked] [--project DIR] X` → the current interpreter, so the test needs neither uv nor a project.
UV_SHIM = f"""#!/bin/sh
echo "$@" >> "$RUNNER_TEMP/uv-calls"
shift
while [ "$1" = --locked ] || [ "$1" = --project ]; do [ "$1" = --project ] && shift; shift; done
[ "$1" = gxg-policy ] && {{ shift; exec "{sys.executable}" -m gxg_policy.cli "$@"; }}
shift
exec "{sys.executable}" "$@"
"""


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def commit_policies(repo: Path, src: Path, msg: str) -> None:
    shutil.rmtree(repo / "policies", ignore_errors=True)
    shutil.copytree(src, repo / "policies")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_policies(r, CASE / "base", "base")
    git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(r, "checkout", "-qb", "claude/x")
    return r


def job(  # noqa: PLR0917 - test helper, keyword use at every call site but the first three
    repo: Path, event: str, ref: str, pr_head: str = "", runner: Path | None = None, script: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs the job script from this repo (= main's copy), or `script` (bash text) in the temp repo."""
    shim = repo.parent / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "uv").write_text(UV_SHIM)
    (shim / "uv").chmod(0o755)
    runner = runner or repo.parent / f"runner-{event}-{ref.rsplit('/', 1)[-1]}"
    runner.mkdir()
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "EVENT": event, "GITHUB_REF": ref}
    env |= {"RUNNER_TEMP": str(runner), "PR_HEAD": pr_head}
    env.pop("GITHUB_STEP_SUMMARY", None)
    cmd = ["bash", str(JOB)] if script is None else ["bash", "-c", script]
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_branch_with_widening_is_red_without_a_pr(repo: Path, event: str) -> None:
    commit_policies(repo, CASE / "head", "widen")
    r = job(repo, event, "refs/heads/claude/x")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "outside a pull request" in r.stdout


def test_diff_is_against_the_merge_base_not_the_branch_tip(repo: Path) -> None:
    # main moves on after the branch point with the same widening: the branch still widens vs its merge-base.
    commit_policies(repo, CASE / "head", "widen")
    git(repo, "checkout", "-q", "main")
    (repo / "README").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "unrelated")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "claude/x")
    assert job(repo, "push", "refs/heads/claude/x").returncode == 1


def test_branch_with_narrowing_only_is_green(repo: Path) -> None:
    git(repo, "checkout", "-q", "main")
    commit_policies(repo, CASE / "head", "main has more")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-qb", "claude/narrow")
    commit_policies(repo, CASE / "base", "narrow")
    r = job(repo, "push", "refs/heads/claude/narrow")
    assert r.returncode == 0, r.stdout + r.stderr


def test_main_needs_no_approval(repo: Path) -> None:
    commit_policies(repo, CASE / "head", "widen")
    assert job(repo, "push", "refs/heads/main").returncode == 0


def test_without_origin_main_it_fails_closed(repo: Path) -> None:
    commit_policies(repo, CASE / "head", "widen")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert job(repo, "push", "refs/heads/claude/x").returncode != 0


def test_tag_named_origin_main_does_not_shadow_the_branch(repo: Path) -> None:
    # Cycle 2 finding 3: `origin/main` resolves to refs/tags/origin/main first; the job uses the full ref.
    commit_policies(repo, CASE / "head", "widen")
    git(repo, "tag", "origin/main", "HEAD")
    r = job(repo, "workflow_dispatch", "refs/heads/claude/x")
    assert r.returncode == 1, r.stdout + r.stderr


def test_pull_request_into_another_branch_is_diffed_against_main(repo: Path, tmp_path: Path) -> None:
    # Cycle 2 finding 1: widening W on claude/base, harmless N on top, PR N -> claude/base, then the base
    # is changed to main. The PR run must see W: the diff is merge-base(main, head)..head, not base..head.
    commit_policies(repo, CASE / "head", "W: widen")
    git(repo, "checkout", "-qb", "claude/w")
    (repo / "README").write_text("n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "N")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "--detach")  # PR runs on a detached checkout
    runner = tmp_path / "runner-pr"
    r = job(repo, "pull_request", "refs/pull/7/merge", pr_head=head, runner=runner)
    report = json.loads((runner / "diff.json").read_text())
    assert len(report["widening"]) == 1, report
    # The label gate then needs the GitHub API (none here): the run is red either way.
    assert r.returncode != 0, r.stdout + r.stderr


def test_pull_request_diff_uses_the_head_commit_not_the_checkout(repo: Path, tmp_path: Path) -> None:
    # The checked-out merge commit is not what is diffed: PR_HEAD is.
    commit_policies(repo, CASE / "head", "widen")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    runner = tmp_path / "runner-pr"
    job(repo, "pull_request", "refs/pull/7/merge", pr_head=head, runner=runner)
    assert len(json.loads((runner / "diff.json").read_text())["widening"]) == 1


def test_code_owners_come_from_main_not_from_the_pr(repo: Path, tmp_path: Path) -> None:
    git(repo, "checkout", "-q", "main")
    (repo / ".github").mkdir()
    (repo / ".github/CODEOWNERS").write_text("* @owner\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "owners")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-qb", "claude/own")
    (repo / ".github/CODEOWNERS").write_text("* @agent\n")
    commit_policies(repo, CASE / "head", "widen and own")
    runner = tmp_path / "runner-pr"
    job(repo, "pull_request", "refs/pull/7/merge", pr_head=git(repo, "rev-parse", "HEAD"), runner=runner)
    assert (runner / "CODEOWNERS").read_text() == "* @owner\n"


def commit_files(repo: Path, files: dict[str, str], msg: str) -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


@pytest.fixture
def gated(repo: Path) -> Path:
    """main also holds the real gate files, as after M2 is merged."""
    git(repo, "checkout", "-q", "main")
    commit_files(repo, {f: (ROOT / f).read_text() for f in GATE_FILES}, "gate on main")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-qB", "claude/x")
    return repo


def step(repo: Path, runner: Path) -> subprocess.CompletedProcess[str]:
    """The ci.yml step itself, as a workflow_dispatch on claude/x."""
    return job(repo, "workflow_dispatch", "refs/heads/claude/x", runner=runner, script=STEP["run"])


def test_pr_that_disables_its_gate_and_widens_is_red(gated: Path, tmp_path: Path) -> None:
    # Cycle 3 finding 1: the PR copy says `exit 0`; ci.yml runs main's copy, which sees the widening.
    commit_files(gated, {"scripts/ci/grants_widening.sh": "exit 0\n"}, "disable the gate")
    commit_policies(gated, CASE / "head", "widen")
    runner = tmp_path / "runner-step"
    r = step(gated, runner)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "2 widening change(s) outside a pull request" in r.stdout
    assert (runner / "gate-paths.txt").read_text() == "scripts/ci/grants_widening.sh\n"
    # gxg-policy and widening_gate.py run from main's worktree with main's lockfile.
    calls = (runner / "uv-calls").read_text().splitlines()
    assert len(calls) == 2 and all(c.startswith(f"run --locked --project {runner}/gate ") for c in calls), calls
    assert calls[1].split()[5] == f"{runner}/gate/scripts/ci/widening_gate.py"


@pytest.mark.parametrize(
    "path", ["scripts/ci/widening_gate.py", "tools/gxg-policy/src/gxg_policy/diff.py", "uv.lock", "pyproject.toml",
             ".github/workflows/ci.yml", "schemas/x.json"]
)  # fmt: skip
def test_gate_edit_alone_needs_the_label(gated: Path, tmp_path: Path, path: str) -> None:
    commit_files(gated, {path: "# changed\n"}, "edit the gate")
    r = step(gated, tmp_path / "runner-step")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "1 widening change(s)" in r.stdout


def test_unrelated_edit_is_green_with_the_gate_from_main(gated: Path, tmp_path: Path) -> None:
    commit_files(gated, {"README.md": "x\n", "scripts/other.sh": "x\n"}, "docs")
    r = step(gated, tmp_path / "runner-step")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bootstrap" not in r.stdout + r.stderr


def test_without_a_gate_on_main_the_pr_copy_runs(repo: Path, tmp_path: Path) -> None:
    # Bootstrap: main has no gate yet. The PR copy runs and its own gate files count as widening.
    commit_files(repo, {f: (ROOT / f).read_text() for f in GATE_FILES}, "add the gate")
    r = step(repo, tmp_path / "runner-step")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "bootstrap" in r.stdout and "2 widening change(s)" in r.stdout


def test_manifest_weakening_is_widening(repo: Path, tmp_path: Path) -> None:
    # Cycle 3 finding 2: same policies, a tool's effect class lowered in connectors/*/gxg-manifest.yaml.
    git(repo, "checkout", "-q", "main")
    shutil.copytree(MANIFESTS / "base-connectors", repo / "connectors")
    commit_files(repo, {}, "manifests")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-qB", "claude/x")
    shutil.rmtree(repo / "connectors")
    shutil.copytree(MANIFESTS / "head-connectors", repo / "connectors")
    commit_files(repo, {}, "weaken")
    runner = tmp_path / "runner-m"
    r = job(repo, "push", "refs/heads/claude/x", runner=runner)
    assert r.returncode == 1, r.stdout + r.stderr
    report = json.loads((runner / "diff.json").read_text())
    assert report == json.loads((MANIFESTS / "expected.json").read_text())
