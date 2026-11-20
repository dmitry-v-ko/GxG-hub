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

ROOT = Path(__file__).resolve().parents[2]
JOB = ROOT / "scripts" / "ci" / "grants_widening.sh"
CASE = ROOT / "tools" / "gxg-policy" / "tests" / "diff" / "01-grant-resource-added"
# `uv run X` → the current interpreter, so the test does not depend on uv or a project in the temp repo.
UV_SHIM = f"""#!/bin/sh
shift
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


def job(
    repo: Path, event: str, ref: str, pr_head: str = "", runner: Path | None = None
) -> subprocess.CompletedProcess[str]:
    shim = repo.parent / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "uv").write_text(UV_SHIM)
    (shim / "uv").chmod(0o755)
    runner = runner or repo.parent / f"runner-{event}-{ref.rsplit('/', 1)[-1]}"
    runner.mkdir()
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "EVENT": event, "GITHUB_REF": ref}
    env |= {"RUNNER_TEMP": str(runner), "PR_HEAD": pr_head}
    env.pop("GITHUB_STEP_SUMMARY", None)
    return subprocess.run(["bash", str(JOB)], cwd=repo, env=env, capture_output=True, text=True, check=False)


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
