#!/usr/bin/env bash
# DoD-4: mutmut on the modules with the key conditions of every milestone, one command:
#   scripts/ci/mutmut.sh [REPORT_FILE [ALL_RESULTS_FILE]]
# Root ([tool.mutmut] in pyproject.toml): M0.1 (AC-M0.1-03) scripts/ci/ and conftest.py, M2 widening_gate.py.
# tools/gxg-policy (its own [tool.mutmut]): M2 gxg_policy {diff,lint,bundle,load}; mutmut maps src/ paths
# to module names, so it runs from that directory. gxg-connector-kit (M1) has its own section and command.
# mutmut exits 0 with survivors, so this script fails on any survivor, untested or timed-out mutant not
# listed in scripts/ci/mutmut-equivalent.txt (each with a reason; the list has no M0.1 mutant, so every
# M0.1 mutant must be killed), and on any listed mutant that is killed now (stale list).
# REPORT_FILE: summary (default stdout). ALL_RESULTS_FILE: every mutant with its status, sorted.
set -euo pipefail
cd "$(dirname "$0")/../.."
report="${1:-/dev/stdout}"
all="${2:-/dev/null}"
equivalent=scripts/ci/mutmut-equivalent.txt
tmp=$(mktemp -d)
for dir in tools/gxg-policy .; do
  (
    cd "$dir"
    rm -rf mutants  # no stale results from an earlier run
    uv run --quiet mutmut run > "$tmp/run.log" 2>&1 || { cat "$tmp/run.log"; exit 1; }
    uv run --quiet mutmut export-cicd-stats > /dev/null
    echo "== $dir: $(tr -d ' \n' < mutants/mutmut-cicd-stats.json)"
    uv run --quiet mutmut results 2> /dev/null
    uv run --quiet mutmut results --all true 2> /dev/null | sed 's/^ *//' >> "$tmp/all.txt"
  ) >> "$tmp/results.txt"
done
sort "$tmp/all.txt" > "$all"
grep -E '^\s+\S+: ' "$tmp/results.txt" | sed -E 's/^\s+//; s/: .*//' | sort > "$tmp/left.txt" || true
{ grep -oE "^[^ #]+" "$equivalent" || true; } | sort > "$tmp/allowed.txt"
unexplained=$(comm -23 "$tmp/left.txt" "$tmp/allowed.txt")
stale=$(comm -13 "$tmp/left.txt" "$tmp/allowed.txt")
{
  grep '^== ' "$tmp/results.txt"
  echo "not killed: $(wc -l < "$tmp/left.txt"), listed as equivalent: $(comm -12 "$tmp/left.txt" "$tmp/allowed.txt" | wc -l)"
  grep -F -f "$tmp/left.txt" "$equivalent" || true
  [ -z "$stale" ] || printf 'listed but killed now (remove from the list):\n%s\n' "$stale"
  if [ -n "$unexplained" ]; then
    printf 'SURVIVED, not equivalent:\n%s\n' "$unexplained"
  else
    echo "mutmut: OK, no surviving non-equivalent mutants"
  fi
} | tee "$report"
[ -z "$unexplained" ] && [ -z "$stale" ]
