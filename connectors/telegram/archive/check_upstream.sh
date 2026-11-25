#!/usr/bin/env bash
# The telegram_archive schema contract with gxg_agents_v2 (archive/SOURCE.md, ADR-0004).
#   check_upstream.sh <clone>                  vendored migrations = gxg_agents_v2's at the pinned commit
#   check_upstream.sh --drift <clone> [<ref>]  fail if migrations after the pinned commit (up to <ref>,
#                                              default origin/HEAD after a fetch) touch the archive schema
# TELEGRAM_ARCHIVE_PIN overrides the pinned commit (tests only).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
drift=0
if [ "${1:-}" = --drift ]; then drift=1; shift; fi
upstream="${1:?path to a gxg_agents_v2 clone}"
commit="${TELEGRAM_ARCHIVE_PIN:-$(grep -oE 'commit `[0-9a-f]{40}`' "$here/SOURCE.md" | grep -oE '[0-9a-f]{40}')}"
git -C "$upstream" cat-file -e "$commit" 2>/dev/null || git -C "$upstream" fetch -q --depth=1 origin "$commit"

if [ "$drift" = 1 ]; then
  ref="${2:-}"
  if [ -z "$ref" ]; then
    git -C "$upstream" fetch -q origin
    ref=origin/HEAD
  fi
  # A migration is flagged when its diff names the archive schema (quoted or not, any case), capability_hub.connections, or sets a
  # search_path that includes one of the two schemas (an unqualified table name after it). Other `SET search_path` lines, e.g.
  # a SECURITY DEFINER function's `pg_catalog, public`, are not the archive contract. -z and quotePath=false: names with spaces
  # or non-ASCII characters are checked too. grep reads the whole diff (no -q): with pipefail an early exit would SIGPIPE git diff.
  pat='telegram_archive|"?capability_hub"?[.]"?connections|search_path[^;]*capability_hub'
  hits=$(git -C "$upstream" -c core.quotePath=false diff --name-only -z "$commit" "$ref" -- supabase/migrations |
    while IFS= read -r -d '' f; do
      if git -C "$upstream" diff "$commit" "$ref" -- "$f" | grep -Ei "$pat" >/dev/null; then printf '%s\n' "$f"; fi
    done)
  if [ -n "$hits" ]; then
    echo "archive schema: gxg_agents_v2 $ref changed the telegram_archive contract after $commit:" >&2
    echo "$hits" >&2
    echo "update archive/SOURCE.md and the vendored migrations (README 'Обновление gxg_agents_v2')" >&2
    exit 1
  fi
  echo "archive schema: no telegram_archive migration changes in gxg_agents_v2 $commit..$ref"
  exit 0
fi

for f in 20261105010000_telegram_archive.sql 20261106010000_telegram_archive_semantic.sql; do
  git -C "$upstream" show "$commit:supabase/migrations/$f" | cmp - "$here/$f"
done
echo "archive schema: vendored migrations = gxg_agents_v2 @ $commit"
