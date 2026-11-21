#!/usr/bin/env bash
# DoD-4: mutmut on the variant C verifier (echo/verify_jws.py, config in setup.cfg).
# Fails unless every mutant is killed or listed below as equivalent.
#   ./mutmut.sh [REPORT_FILE]
set -euo pipefail
cd "$(dirname "$0")"
EQUIVALENT=(
  # json.dumps(ensure_ascii=None): falsy, the same as ensure_ascii=False
  "echo.verify_jws.x_canonical__mutmut_4"
)
m() { uv run --quiet --with mutmut==3.8.1 mutmut "$@" 2>/dev/null; }
rm -rf mutants  # no stale results from an earlier run
m run >/dev/null
m results --all true | sed 's/^ *//' | sort > "${1:-/dev/stdout}"
left="$(m results | sed 's/^ *//' | grep -v -F -x -f <(printf '%s: survived\n' "${EQUIVALENT[@]}") || true)"
if [ -n "$left" ]; then
  echo "mutmut: mutants not killed and not equivalent:" >&2
  echo "$left" >&2
  exit 1
fi
echo "mutmut: all non-equivalent mutants killed" >&2
