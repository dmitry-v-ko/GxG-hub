#!/usr/bin/env bash
# The vendored telegram_archive migrations equal gxg_agents_v2's at the pinned commit (archive/SOURCE.md).
#   connectors/telegram/archive/check_upstream.sh /path/to/gxg_agents_v2
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
upstream="${1:?path to a gxg_agents_v2 clone}"
commit=$(grep -oE 'commit `[0-9a-f]{40}`' "$here/SOURCE.md" | grep -oE '[0-9a-f]{40}')
git -C "$upstream" cat-file -e "$commit" 2>/dev/null || git -C "$upstream" fetch -q --depth=1 origin "$commit"
for f in 20261105010000_telegram_archive.sql 20261106010000_telegram_archive_semantic.sql; do
  git -C "$upstream" show "$commit:supabase/migrations/$f" | cmp - "$here/$f"
done
echo "archive schema: vendored migrations = gxg_agents_v2 @ $commit"
