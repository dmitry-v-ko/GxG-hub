# Railway wrapper of approval/notifier/Dockerfile: the same build, plus entrypoint.sh (secrets as files).
# The optional ca-bundle secret mount of the original is dropped (Railway has no TLS-intercepting proxy to trust).
# tests/deploy checks that the original text is still contained here. Build context: the repository root.
ARG TAILSCALE_IMAGE=tailscale/tailscale:v1.84.2
FROM ${TAILSCALE_IMAGE} AS tailscale

# Notify bot (M9), its own image and lockfile (AS-29). Build from the repository root:
#   docker build -f approval/notifier/Dockerfile .
# The fake Bot API (approval/notifier/testing) is not in the image. M10: the kit (bundle, webhook signature).
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_FROZEN=1
WORKDIR /app
COPY packages/gxg-connector-kit packages/gxg-connector-kit
WORKDIR /app/approval/notifier
COPY approval/notifier/pyproject.toml approval/notifier/uv.lock ./
COPY approval/notifier/src src
RUN uv sync --no-dev --no-editable

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=build /app/approval/notifier/.venv /app/approval/notifier/.venv
RUN useradd --system --uid 10003 gxg && mkdir -p /var/lib/gxg && chown gxg /var/lib/gxg

COPY deploy/railway/entrypoint.sh /opt/gxg/entrypoint.sh
USER gxg
ENV PATH=/app/approval/notifier/.venv/bin:$PATH PYTHONUNBUFFERED=1
ENTRYPOINT ["/opt/gxg/entrypoint.sh"]
CMD ["gxg-approval-notifier"]
