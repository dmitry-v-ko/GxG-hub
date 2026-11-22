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
  changed=$(git -C "$upstream" diff --name-only "$commit" "$ref" -- supabase/migrations)
  hits=""
  for f in $changed; do
    # grep reads the whole diff (no -q): with pipefail an early exit would SIGPIPE git diff on a big file and hide it.
    # search_path: a migration may name the archive tables unqualified after SET search_path (reviewed by hand).
    if git -C "$upstream" diff "$commit" "$ref" -- "$f" | grep -E 'telegram_archive|capability_hub\.connections|search_path' >/dev/null; then
      hits="$hits $f"
    fi
  done
  if [ -n "$hits" ]; then
    echo "archive schema: gxg_agents_v2 $ref changed the telegram_archive contract after $commit:$hits" >&2
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
