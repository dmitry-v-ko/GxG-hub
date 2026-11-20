#!/usr/bin/env bash
# Writes spikes/m4-live/.env with fresh random secrets (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -e .env ] && { echo ".env exists"; exit 0; }
r() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
cat > .env <<E
POSTGRES_PASSWORD=$(r)
JWT_SECRET_KEY=$(r)
AUTH_ENCRYPTION_SECRET=$(r)
IDENTITY_CLAIMS_SECRET=$(r)
PLATFORM_ADMIN_EMAIL=admin@example.com
PLATFORM_ADMIN_PASSWORD=Aa1!$(r)
DEFAULT_USER_PASSWORD=Bb2!$(r)
E
chmod 600 .env
