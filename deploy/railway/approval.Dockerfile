# Railway image of the Approval Service processes api, mcp, executor, sweeper, decider and the jobs migrate, db-init.
# `admin` is a tailnet entry (tag:approval-admin), so the image carries tailscale; the other processes never start it
# (the entrypoint runs tailscaled only when TS_HOSTNAME is set). Build context: the repository root. One image, the
# process is the start command (`gxg-approval <process>`).
ARG TAILSCALE_IMAGE=tailscale/tailscale:v1.84.2
FROM ${TAILSCALE_IMAGE} AS tailscale

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
COPY approval/deploy/roles.sql /opt/gxg/roles.sql
COPY deploy/railway/db_init.py /opt/gxg/db_init.py
COPY deploy/railway/entrypoint.sh /opt/gxg/entrypoint.sh
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscale /usr/local/bin/
RUN useradd --system --uid 10003 gxg && mkdir -p /var/lib/gxg && chown gxg /var/lib/gxg
USER gxg
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
VOLUME ["/var/lib/gxg"]
ENTRYPOINT ["/opt/gxg/entrypoint.sh"]
CMD ["gxg-approval", "api"]
