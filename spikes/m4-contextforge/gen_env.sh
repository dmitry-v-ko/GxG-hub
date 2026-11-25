#!/usr/bin/env bash
# Generates throwaway test secrets into .env (git-ignored). Never commit .env.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { echo ".env exists, keeping it"; exit 0; }
r() { python3 -c 'import secrets;print(secrets.token_urlsafe(32))'; }
umask 077
cat > .env <<E
POSTGRES_PASSWORD=$(r)
JWT_SECRET_KEY=$(r)
AUTH_ENCRYPTION_SECRET=$(r)
IDENTITY_CLAIMS_SECRET=$(r)
PLATFORM_ADMIN_EMAIL=admin@spike.example.com
PLATFORM_ADMIN_PASSWORD=$(r)Aa1!
DEFAULT_USER_PASSWORD=$(r)Aa1!
BASIC_AUTH_PASSWORD=$(r)Aa1!
E
echo "wrote .env"
