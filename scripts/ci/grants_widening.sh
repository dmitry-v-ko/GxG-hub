#!/usr/bin/env bash
# CI job grants-widening (AC-M2-04, GR-10). Env: EVENT, PR_HEAD (pull_request), GITHUB_REF, RUNNER_TEMP.
# ci.yml runs this file from a worktree of main, and gxg-policy/widening_gate.py come from the same worktree
# (`uv run --project "$gate"`): a PR cannot change the code that judges it. The checkout (cwd) is only read by git.
# main: already merged, nothing to approve. Any other run diffs merge-base(main, H)..H, H = PR head or HEAD:
# always against main, never against the PR's own base (it can be another branch, and it can change).
# Policies and connector manifests go to gxg-policy diff; every other control change (CI grants, switched-off checks,
# exception lists, schemas, packages, the gate itself) to gate_diff.py. Each finding of either counts as widening.
# pull_request: label gate. push / workflow_dispatch to a branch: red on any widening.
set -euo pipefail
tmp="${RUNNER_TEMP:?}"
gate=$(cd "$(dirname "$0")/../.." && pwd)
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
uv run --locked --project "$gate" gxg-policy diff "$tmp/base/policies" "$tmp/head/policies" \
  --base-connectors "$tmp/base/connectors" --head-connectors "$tmp/head/connectors" \
  --json "$tmp/diff.json" --md "$tmp/diff.md"
uv run --locked --project "$gate" python "$gate/scripts/ci/gate_diff.py" \
  --repo . --base "$base" --head "$head" --json "$tmp/gate-findings.json"
check=(uv run --locked --project "$gate" python "$gate/scripts/ci/widening_gate.py"
  --report "$tmp/diff.json" --markdown "$tmp/diff.md" --gate-findings "$tmp/gate-findings.json")
if [ "$EVENT" != "pull_request" ]; then
  exec "${check[@]}" --no-pr
fi
# Owners come from main: a PR cannot make its author a code owner.
git show "$main:.github/CODEOWNERS" > "$tmp/CODEOWNERS" || : > "$tmp/CODEOWNERS"
# --repo: an older label still covers the head if the head has no widening the labelled commit did not have.
exec "${check[@]}" --codeowners "$tmp/CODEOWNERS" --repo . --main "$main"
