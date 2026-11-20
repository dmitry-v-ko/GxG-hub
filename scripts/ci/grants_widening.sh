#!/usr/bin/env bash
# CI job grants-widening (AC-M2-04, GR-10). Env: EVENT, PR_HEAD (pull_request), GITHUB_REF, RUNNER_TEMP.
# main: already merged, nothing to approve. Any other run diffs merge-base(main, H)..H, H = PR head or HEAD:
# always against main, never against the PR's own base (it can be another branch, and it can change).
# pull_request: label gate. push / workflow_dispatch to a branch: red on any widening.
set -euo pipefail
tmp="${RUNNER_TEMP:?}"
main=refs/remotes/origin/main  # full ref: a tag named origin/main would win over the short name
if [ "$EVENT" = "pull_request" ]; then
  head="${PR_HEAD:?}"
elif [ "${GITHUB_REF:?}" = "refs/heads/main" ]; then
  echo "grants-widening: main (event $EVENT): merged policies, nothing to approve"
  exit 0
else
  head=$(git rev-parse HEAD)
fi
# No origin/main → no merge-base → this fails, and the check is red: fail closed.
base=$(git merge-base "$main" "$head")
git worktree add --detach "$tmp/base" "$base"
git worktree add --detach "$tmp/head" "$head"
uv run gxg-policy diff "$tmp/base/policies" "$tmp/head/policies" --json "$tmp/diff.json" --md "$tmp/diff.md"
if [ "$EVENT" != "pull_request" ]; then
  exec uv run python "$(dirname "$0")/widening_gate.py" --report "$tmp/diff.json" --markdown "$tmp/diff.md" --no-pr
fi
# Owners come from main: a PR cannot make its author a code owner.
git show "$main:.github/CODEOWNERS" > "$tmp/CODEOWNERS" || : > "$tmp/CODEOWNERS"
exec uv run python "$(dirname "$0")/widening_gate.py" --report "$tmp/diff.json" --markdown "$tmp/diff.md" \
  --codeowners "$tmp/CODEOWNERS"
