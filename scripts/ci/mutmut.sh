#!/usr/bin/env bash
# DoD-4 / AC-M0.1-03: mutation testing of the M0 key-condition modules with mutmut
# (what and how: [tool.mutmut] in pyproject.toml). Fails unless every mutant is killed.
#   scripts/ci/mutmut.sh [REPORT_FILE]
set -euo pipefail
cd "$(dirname "$0")/../.."
rm -rf mutants  # no stale results from an earlier run
uv run mutmut run
uv run mutmut results --all true | sed 's/^ *//' | sort > "${1:-/dev/stdout}"
left="$(uv run mutmut results)"  # every mutant that is not killed: survived, no tests, timeout, ...
if [ -n "$left" ]; then
  echo "mutmut: mutants not killed:" >&2
  echo "$left" >&2
  exit 1
fi
echo "mutmut: all mutants killed"
