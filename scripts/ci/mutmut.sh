#!/usr/bin/env bash
# DoD-4: mutmut on the modules with the key conditions of every milestone, one command:
#   scripts/ci/mutmut.sh [REPORT_FILE [ALL_RESULTS_FILE]]
# Root ([tool.mutmut] in pyproject.toml): M0.1 (AC-M0.1-03) scripts/ci/ and conftest.py, M2 widening_gate.py.
# tools/gxg-policy (its own [tool.mutmut]): M2 gxg_policy {diff,lint,bundle,load}; mutmut maps src/ paths
# to module names, so it runs from that directory. connectors/demo (M3, its own lockfile and [tool.mutmut]):
# tools.py, backend.py. connectors/telegram (M11, the same layout): tools.py, server.py. gateway/contextforge (M5, setup.cfg): gxg_identity/core.py. packages/gxg-connector-kit
# (M1, its own [tool.mutmut]; in this loop since M5): _jwt, exec_token, svc_jwt, grants, canonical, cf_identity.
# approval/ui, approval/notifier (M9, own lockfiles and [tool.mutmut]; run with the root environment, where their
# DB tests find the Approval Service fixtures): ui app, stepup, render, config, cli; notifier bot (its cli.py is
# the two endless loops of the process, run by the compose stand).
# approval (its own [tool.mutmut]): M7 policy, transitions, quotas, intents (T1), api, mcp. Its DB tests need one
# PostgreSQL for all mutants: a container without network, reached over a Unix socket (needs Docker).
# mutmut exits 0 with survivors, so this script fails on any survivor, untested or timed-out mutant not
# listed in scripts/ci/mutmut-equivalent.txt (each with a reason; the list has no M0.1 mutant, so every
# M0.1 mutant must be killed), and on any listed mutant that is killed now (stale list).
# MUTMUT_PACKAGES (env): which package directories to run, default all; the SQL driver always runs.
# REPORT_FILE: summary (default stdout). ALL_RESULTS_FILE: every mutant with its status, sorted.
set -euo pipefail
cd "$(dirname "$0")/../.."
report="${1:-/dev/stdout}"
all="${2:-/dev/null}"
equivalent=scripts/ci/mutmut-equivalent.txt
tmp=$(mktemp -d)
pgsock=$(mktemp -d)
chmod 777 "$pgsock"
pg=$(docker run -d --rm --network none -e POSTGRES_USER=test -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$pgsock:/var/run/postgresql" postgres:16.6-alpine)
trap 'docker stop "$pg" > /dev/null 2>&1 || true' EXIT
for _ in $(seq 240); do docker logs "$pg" 2>&1 | grep -q "PostgreSQL init process complete" && break; sleep 0.5; done
export GXG_TEST_PG_SOCKET_DIR="$pgsock"
root=$(pwd)
# MUTMUT_PACKAGES: space-separated subset of these directories (DoD-1: a milestone runs the packages it changes;
# every package at integration and before main). Default: all.
for dir in ${MUTMUT_PACKAGES:-tools/gxg-policy connectors/demo connectors/telegram gateway/contextforge packages/gxg-connector-kit approval \
  approval/ui approval/notifier .}; do
  (
    cd "$dir"
    case "$dir" in approval/ui | approval/notifier) uvr=(uv run --quiet --project "$root") ;; *) uvr=(uv run --quiet) ;; esac
    rm -rf mutants  # no stale results from an earlier run
    "${uvr[@]}" mutmut run > "$tmp/run.log" 2>&1 || { cat "$tmp/run.log"; exit 1; }
    "${uvr[@]}" mutmut export-cicd-stats > /dev/null
    echo "== $dir: $(tr -d ' \n' < mutants/mutmut-cicd-stats.json)"
    "${uvr[@]}" mutmut results 2> /dev/null
    "${uvr[@]}" mutmut results --all true 2> /dev/null | sed 's/^ *//' >> "$tmp/all.txt"
  ) >> "$tmp/results.txt"
done
# M7 SQL (migration PL/pgSQL, SQL constants of intents.py): mutmut does not mutate SQL text.
sql_ok=true
uv run --quiet python scripts/ci/mutate_m7_sql.py > "$tmp/sql.txt" 2>&1 || sql_ok=false
sort "$tmp/all.txt" > "$all"
grep -E '^\s+\S+: ' "$tmp/results.txt" | sed -E 's/^\s+//; s/: .*//' | sort > "$tmp/left.txt" || true
{ grep -oE "^[^ #]+" "$equivalent" || true; } | sort > "$tmp/allowed.txt"
if [ -n "${MUTMUT_PACKAGES:-}" ]; then  # a subset: the list's entries of the packages not run are not stale
  sed 's/: .*//' "$tmp/all.txt" | sort | comm -12 "$tmp/allowed.txt" - > "$tmp/allowed-run.txt"
  mv "$tmp/allowed-run.txt" "$tmp/allowed.txt"
fi
unexplained=$(comm -23 "$tmp/left.txt" "$tmp/allowed.txt")
stale=$(comm -13 "$tmp/left.txt" "$tmp/allowed.txt")
{
  grep '^== ' "$tmp/results.txt"
  echo "not killed: $(wc -l < "$tmp/left.txt"), listed as equivalent: $(comm -12 "$tmp/left.txt" "$tmp/allowed.txt" | wc -l)"
  grep -F -f "$tmp/left.txt" "$equivalent" || true
  [ -z "$stale" ] || printf 'listed but killed now (remove from the list):\n%s\n' "$stale"
  echo "== M7 SQL mutations (scripts/ci/mutate_m7_sql.py)"
  cat "$tmp/sql.txt"
  if [ -n "$unexplained" ]; then
    printf 'SURVIVED, not equivalent:\n%s\n' "$unexplained"
  else
    echo "mutmut: OK, no surviving non-equivalent mutants"
  fi
  $sql_ok && echo "SQL mutations: OK" || echo "SQL mutations: FAILED (see above)"
} | tee "$report"
[ -z "$unexplained" ] && [ -z "$stale" ] && $sql_ok
