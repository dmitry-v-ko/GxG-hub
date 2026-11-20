#!/usr/bin/env bash
# DoD-4 for M2: mutmut on the modules with the milestone's key conditions, one command:
#   bash scripts/ci/mutmut.sh [report.txt]
# gxg_policy {diff,lint,bundle,load} runs from tools/gxg-policy (mutmut maps src/ paths to module names),
# scripts/ci/widening_gate.py from the root. mutmut exits 0 with survivors, so this script fails on any
# survivor, untested or timed-out mutant not listed in scripts/ci/mutmut-equivalent.txt (each with a reason).
set -euo pipefail
cd "$(dirname "$0")/../.."
report="${1:-/dev/stdout}"
equivalent=scripts/ci/mutmut-equivalent.txt
tmp=$(mktemp -d)
for dir in tools/gxg-policy .; do
  (
    cd "$dir"
    rm -rf mutants
    uv run --quiet mutmut run > "$tmp/run.log" 2>&1 || { cat "$tmp/run.log"; exit 1; }
    uv run --quiet mutmut export-cicd-stats > /dev/null
    echo "== $dir: $(tr -d ' \n' < mutants/mutmut-cicd-stats.json)"
    uv run --quiet mutmut results 2> /dev/null
  ) >> "$tmp/results.txt"
done
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
