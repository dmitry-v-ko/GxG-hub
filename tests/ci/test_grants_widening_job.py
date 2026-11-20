"""F1 (AC-M2-04): outside pull_request the grants-widening job fails closed on widening.

Runs scripts/ci/grants_widening.sh in a throw-away git repo: main has the base policies, the branch
has the head policies, `origin/main` is a remote-tracking ref as actions/checkout(fetch-depth: 0) leaves it.
"""

from __future__ import annotations

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


def job(repo: Path, event: str, ref: str) -> subprocess.CompletedProcess[str]:
    shim = repo.parent / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "uv").write_text(UV_SHIM)
    (shim / "uv").chmod(0o755)
    runner = repo.parent / f"runner-{event}-{ref.rsplit('/', 1)[-1]}"
    runner.mkdir()
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "EVENT": event, "GITHUB_REF": ref}
    env |= {"RUNNER_TEMP": str(runner), "PR_BASE": ""}
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
