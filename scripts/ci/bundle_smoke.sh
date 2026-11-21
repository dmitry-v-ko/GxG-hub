#!/usr/bin/env bash
# AC-M2-05/06 smoke on the repository policies with a throw-away key: two builds are
# byte-identical (same built_at), verify accepts the result. Run from the repository root.
set -euo pipefail
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
GXG_BUNDLE_SIGNING_KEY=$(uv run python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode())")
export GXG_BUNDLE_SIGNING_KEY
pub=$(uv run python -c "import os; from gxg_policy.bundle import public_key, unb64url; print(public_key(unb64url(os.environ['GXG_BUNDLE_SIGNING_KEY'])))")
for out in a b; do
  uv run gxg-policy build policies/ --out "$tmp/$out" --kid smoke --built-at 2026-01-01T00:00:00Z
done
cmp "$tmp/a/grants-bundle.json" "$tmp/b/grants-bundle.json"
uv run gxg-policy verify "$tmp/a" --kid smoke --public-key "$pub"
