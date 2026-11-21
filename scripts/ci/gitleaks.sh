#!/usr/bin/env bash
# Secret scan with pinned gitleaks (AC-M0-04, AC-M0.1-01). Run inside the repository to scan.
#
#   scripts/ci/gitleaks.sh tree [REV]   every file in the tree of REV (default HEAD)
#   scripts/ci/gitleaks.sh range A..B   every commit in A..B; A = all-zero SHA or empty: all of B
#
# Two configs, both must pass: .gitleaks.toml (gitleaks defaults) and .gitleaks-strict.toml
# (AC-M0-04 rules without the default path allowlist). Historical findings: .gitleaksignore.
# All three come from this repository's root, not from the scanned repository. Exit code: 0 clean, non-zero findings or error.
# Plus one check of file names outside gitleaks, because gitleaks skips empty files (owner decision 2026-11-21, AC-M0.1-02).
# GITLEAKS=/path/to/gitleaks uses that binary instead of downloading (it must be $VERSION).
set -euo pipefail

VERSION=8.30.1
SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb  # gitleaks_8.30.1_linux_x64.tar.gz
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IGNORE="$HERE/.gitleaksignore"

install_gitleaks() {
  # shortcut: linux x64 only (CI); elsewhere set GITLEAKS to a local 8.30.1 binary.
  local dir="${XDG_CACHE_HOME:-$HOME/.cache}/gxg/gitleaks-$VERSION"
  if [ ! -x "$dir/gitleaks" ]; then
    local tmp
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/gl.tar.gz" \
      "https://github.com/gitleaks/gitleaks/releases/download/v$VERSION/gitleaks_${VERSION}_linux_x64.tar.gz"
    echo "$SHA256  $tmp/gl.tar.gz" | sha256sum -c --quiet -
    tar -xzf "$tmp/gl.tar.gz" -C "$tmp" gitleaks
    mkdir -p "$dir" && mv "$tmp/gitleaks" "$dir/gitleaks" && rm -rf "$tmp"
  fi
  echo "$dir/gitleaks"
}

GL="${GITLEAKS:-$(install_gitleaks)}"
[ "$("$GL" version)" = "$VERSION" ] || { echo "gitleaks.sh: need gitleaks $VERSION, got $("$GL" version)" >&2; exit 2; }
# --redact: never print the secret; --ignore-gitleaks-allow: an inline "gitleaks:allow" must not hide a finding.
COMMON=(--no-banner --no-color --redact --ignore-gitleaks-allow -v --exit-code 1 --gitleaks-ignore-path "$IGNORE")
ZERO=0000000000000000000000000000000000000000
# The file-name rule of .gitleaks-strict.toml, for empty files: gitleaks skips them (owner decision 2026-11-21, AC-M0.1-02).
NAMES='(?i)(?:^|/)(?!(?-i)\.env\.example$)(?:[^/]*\.(?:pem|key|p12|pfx)|\.env[^/]*|id_(?:rsa|dsa|ecdsa|ed25519))$'

names() {
  local bad
  bad="$("$@" | { grep -zP "$NAMES" || true; } | tr '\0' '\n' | sort -u)" || return 2
  [ -z "$bad" ] || { sed 's/^/gitleaks.sh: secret file name: /' <<<"$bad" >&2; return 1; }
}

scan_log() {
  local rc=0
  for config in "$HERE/.gitleaks.toml" "$HERE/.gitleaks-strict.toml"; do
    # --text: binary and UTF-16 diffs are scanned too; -m: diffs of merge commits.
    "$GL" git "${COMMON[@]}" --config "$config" --log-opts="--text -m --full-history $1" . || rc=1
  done
  return "$rc"
}

case "${1:-}" in
  tree)
    rev="$(git rev-parse --verify "${2:-HEAD}^{commit}")"
    # The tree as one parentless commit: every file is an added file of that commit, so the tree
    # goes through the same `git log --text` path as the range (gitleaks `dir` skips binary files).
    snapshot="$(GIT_AUTHOR_NAME=gitleaks GIT_AUTHOR_EMAIL=ci@example.invalid \
      GIT_COMMITTER_NAME=gitleaks GIT_COMMITTER_EMAIL=ci@example.invalid \
      git commit-tree -m "tree of $rev" "$rev^{tree}")"
    rc=0
    names git ls-tree -r -z --name-only "$rev" || rc=1
    scan_log "$snapshot" || rc=1
    exit "$rc"
    ;;
  range)
    spec="${2:?range A..B}"
    base="${spec%%..*}"
    head="${spec#*..}"
    [ "$spec" != "$base" ] && [ -n "$head" ] || { echo "gitleaks.sh: range must look like A..B" >&2; exit 2; }
    git rev-parse --verify -q "$head^{commit}" >/dev/null || { echo "gitleaks.sh: bad revision $head" >&2; exit 2; }
    if [ -z "$base" ] || [ "$base" = "$ZERO" ]; then
      revs="$head"
    else
      git rev-parse --verify -q "$base^{commit}" >/dev/null || { echo "gitleaks.sh: bad revision $base" >&2; exit 2; }
      revs="$base..$head"
    fi
    rc=0
    names git log -z --format= --name-only --diff-filter=d -m --full-history "$revs" || rc=1
    scan_log "$revs" || rc=1
    exit "$rc"
    ;;
  *)
    echo "usage: $0 tree [REV] | range A..B" >&2
    exit 2
    ;;
esac
