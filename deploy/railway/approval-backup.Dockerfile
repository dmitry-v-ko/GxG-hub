# Railway image of the daily Approval backup (M14, OPS-03, RET-04, docs/runbooks/approval-backup.md): the Approval Python
# environment (gxg_approval: RET-01 purge after a restore), pg_dump/pg_restore 16 from the PostgreSQL apt repository (the
# server is postgres 16, a client of another major version refuses to dump it) and scripts/ops/approval_backup.py.
# Build context: the repository root. One cron service; the backups live on its Railway Volume.
ARG TAILSCALE_IMAGE=tailscale/tailscale:v1.84.2
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_FROZEN=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/gxg-connector-kit packages/gxg-connector-kit
COPY approval approval
COPY tools/gxg-policy tools/gxg-policy
COPY tools/gxg-connector-check tools/gxg-connector-check
RUN uv sync --no-dev --no-editable --package gxg-approval

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=build /app/.venv /app/.venv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY approval/deploy/roles.sql /opt/gxg/roles.sql
COPY scripts/ops/approval_backup.py /opt/gxg/approval_backup.py
COPY scripts/ops/approval_restore_drill.py /opt/gxg/approval_restore_drill.py
COPY deploy/railway/entrypoint.sh /opt/gxg/entrypoint.sh
RUN useradd --system --uid 10003 gxg && mkdir -p /var/backups/gxg-approval && chown gxg /var/backups/gxg-approval
# Railway mounts the volume root-owned: the entrypoint chowns GXG_VOLUME_DIRS and drops to gxg (setpriv) first.
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 GXG_VOLUME_DIRS=/var/backups/gxg-approval
ENTRYPOINT ["/opt/gxg/entrypoint.sh"]
