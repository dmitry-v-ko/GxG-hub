#!/bin/sh
# Init step of the stand's telegram-archive database: the reader role's password from the environment
# (stand/init.py puts it in .env), never in a file of the repository (SEC-16).
set -eu
psql -v ON_ERROR_STOP=1 -v pw="$TELEGRAM_ARCHIVE_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
ALTER ROLE gxg_telegram_reader PASSWORD :'pw';
SQL
