#!/usr/bin/env python3
"""DoD-4 for M2, non-Python part: each mutation of `grants_widening.sh` and `ci.yml` must turn a test red.

Python (gxg_policy, widening_gate.py) is covered by mutmut (`scripts/ci/mutmut.sh`). mutmut mutates Python
only, and no maintained mutation tool exists for bash or for GitHub workflow YAML, so these two files keep this
hand list (reports/plans/M2-cycle-3.md, DoD-6). Applies one mutation at a time, runs the tests, restores the file.
Run from the repository root: `uv run python scripts/ci/mutate_m2.py [> reports/mutations/M2-cycle-N.txt]`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOB = "scripts/ci/grants_widening.sh"
CI = ".github/workflows/ci.yml"
TESTS = ["tests/ci/test_grants_widening_job.py", "tests/ci/test_ci_workflow.py"]
UV = "      - uses: astral-sh/setup-uv@1db35dd8eee11c5894872e0b383ae6c406cf738d # v10.4.0\n      - run: uv sync\n"

# (file, exact text, replacement, what the mutation breaks)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (JOB, "main=refs/remotes/origin/main", "main=origin/main", "a tag origin/main shadows the branch"),
    (JOB, 'head="${PR_HEAD:?}"', "head=HEAD", "PR run diffs the checkout, not the PR head"),
    (JOB, 'base=$(git merge-base "$main" "$head")', 'base="$head"', "head diffed against itself"),
    (JOB, 'base=$(git merge-base "$main" "$head")', 'base=$(git rev-parse "$head^")', "diff vs the PR base, not main"),
    (JOB, 'elif [ "${GITHUB_REF:?}" = "refs/heads/main" ]; then', "elif true; then", "every branch run passes"),
    (JOB, 'if [ "$EVENT" != "pull_request" ]; then\n  exec', "if false; then\n  exec", "push run uses PR mode"),
    (JOB, 'git show "$main:.github/CODEOWNERS"', 'git show "$head:.github/CODEOWNERS"', "PR names its own owners"),
    (CI, "reopened, edited, labeled", "reopened, labeled", "base change does not re-run the gate"),
    (CI, "PR_HEAD: ${{ github.event.pull_request.head.sha }}\n        run: bash scripts/ci/grants_widening.sh",
     "PR_HEAD: ${{ github.event.pull_request.base.sha }}\n        run: bash scripts/ci/grants_widening.sh",
     "gate diffs the base commit"),
    (CI, "github.event_name == 'pull_request' && github.run_id || github.ref", "github.ref", "PR runs cancelled"),
    (CI, "      actions: read ", "      actions: none ", "gate cannot read workflow runs"),
    (CI, "needs: [lint, types, tests, schema-guard, secrets, policy, grants-widening]",
     "needs: [lint, types, tests, schema-guard, secrets, policy]", "ci-ok ignores the gate"),
    (CI, "(github.event_name == 'push' || github.event_name == 'schedule')", "(github.event_name == 'push')",
     "bundle not re-signed on schedule"),
    (CI, f"          fetch-depth: 0\n          persist-credentials: false\n{UV}      - name: Policy diff",
     f"          persist-credentials: false\n{UV}      - name: Policy diff", "gate checkout without history"),
]  # fmt: skip


def run_tests() -> bool:
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *TESTS]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=False).returncode == 0


def main() -> int:
    if not run_tests():
        print("baseline: tests are red, nothing to measure")
        return 2
    survived = 0
    for file, old, new, what in MUTATIONS:
        path = ROOT / file
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            print(f"STALE     {file}: {what} (text found {text.count(old)} times)")
            survived += 1
            continue
        try:
            path.write_text(text.replace(old, new), encoding="utf-8")
            green = run_tests()
        finally:
            path.write_text(text, encoding="utf-8")
        survived += green
        print(f"{'SURVIVED' if green else 'killed  '}  {file}: {what}")
    print(f"{len(MUTATIONS) - survived}/{len(MUTATIONS)} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
