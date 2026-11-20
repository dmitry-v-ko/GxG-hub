#!/usr/bin/env bash
# CI job grants-widening (AC-M2-04, GR-10). Env: EVENT, PR_BASE (pull_request), GITHUB_REF, RUNNER_TEMP.
# pull_request: diff base..head, label gate. main: already merged, nothing to approve.
# Any other ref (push / workflow_dispatch to a branch): diff merge-base(origin/main)..HEAD, red on widening.
set -euo pipefail
tmp="${RUNNER_TEMP:?}"
if [ "$EVENT" = "pull_request" ]; then
  base="$PR_BASE"
elif [ "${GITHUB_REF:?}" = "refs/heads/main" ]; then
  echo "grants-widening: main (event $EVENT): merged policies, nothing to approve"
  exit 0
else
  # No origin/main → no merge-base → this fails, and the check is red: fail closed.
  base=$(git merge-base origin/main HEAD)
fi
git worktree add --detach "$tmp/base" "$base"
uv run gxg-policy diff "$tmp/base/policies" policies --json "$tmp/diff.json" --md "$tmp/diff.md"
if [ "$EVENT" != "pull_request" ]; then
  exec uv run python "$(dirname "$0")/widening_gate.py" --report "$tmp/diff.json" --markdown "$tmp/diff.md" --no-pr
fi
# Owners come from the base commit: a PR cannot make its author a code owner.
git show "$base:.github/CODEOWNERS" > "$tmp/CODEOWNERS" || : > "$tmp/CODEOWNERS"
exec uv run python "$(dirname "$0")/widening_gate.py" --report "$tmp/diff.json" --markdown "$tmp/diff.md" \
  --codeowners "$tmp/CODEOWNERS"
