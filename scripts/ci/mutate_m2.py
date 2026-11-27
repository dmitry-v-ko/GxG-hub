#!/usr/bin/env python3
"""DoD-4 for M2, non-Python part: each mutation of `grants_widening.sh` and `ci.yml` must turn a test red.

Python (gxg_policy, widening_gate.py, gate_diff.py) is covered by mutmut (`scripts/ci/mutmut.sh`). mutmut mutates Python
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
UV = "      - uses: astral-sh/setup-uv@1db35dd8eee11c5894872e0b383ae6c406cf738d # v10.4.0\n"

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
    (CI, "PR_HEAD: ${{ github.event.pull_request.head.sha }}\n        run: |",
     "PR_HEAD: ${{ github.event.pull_request.base.sha }}\n        run: |", "gate diffs the base commit"),
    (CI, '          bash "$gate"\n', "          bash scripts/ci/grants_widening.sh\n", "PR copy of the gate runs"),
    (CI, 'if [ ! -f "$gate" ]; then', "if true; then", "PR copy of the gate always runs"),
    (CI, f"{UV}      # No `uv sync`", f"{UV}      - run: uv sync\n      # No `uv sync`", "PR project installed"),
    (JOB, '--repo . --base "$base" --head "$head"', '--repo . --base "$head" --head "$head"',
     "control changes diffed against the head itself"),
    (JOB, '"$gate/scripts/ci/gate_diff.py"', "scripts/ci/gate_diff.py", "PR's gate_diff.py runs"),
    (JOB, ' --repo . --main "$main"', "", "an older label never carries over a merge of main"),
    (JOB, ' --head-connectors "$tmp/head/connectors"', ' --head-connectors "$tmp/base/connectors"',
     "manifest edits are not compared"),
    (JOB, '--gate-findings "$tmp/gate-findings.json")', ")", "control changes not passed to the gate"),
    (JOB, 'check=(uv run --locked --project "$gate" python', "check=(uv run --locked --project . python",
     "widening_gate.py runs in the PR's environment"),
    (JOB, 'uv run --locked --project "$gate" gxg-policy', "uv run --locked gxg-policy", "PR's gxg-policy runs"),
    (JOB, '"$gate/scripts/ci/widening_gate.py"', "scripts/ci/widening_gate.py", "PR's widening_gate.py runs"),
    (CI, "github.event_name == 'pull_request' && github.run_id || github.ref", "github.ref", "PR runs cancelled"),
    (CI, "      actions: read ", "      actions: none ", "gate cannot read workflow runs"),
    (CI, "demo-image, m5-stand, grants-widening]", "demo-image, m5-stand]", "ci-ok ignores the gate"),
    (CI, "      || (github.event_name == 'schedule' && github.event.schedule == '41 */6 * * *'))",
     "      )", "bundle not re-signed on schedule"),
    (CI, f"          fetch-depth: 0\n          persist-credentials: false\n{UV}      # No",
     f"          persist-credentials: false\n{UV}      # No", "gate checkout without history"),
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
