# Notify bot (M9), its own image and lockfile (AS-29). Build from the repository root:
#   docker build -f approval/notifier/Dockerfile .
# The fake Bot API (approval/notifier/testing) is not in the image.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_FROZEN=1
WORKDIR /app/approval/notifier
COPY approval/notifier/pyproject.toml approval/notifier/uv.lock ./
COPY approval/notifier/src src
RUN --mount=type=secret,id=ca-bundle,required=false \
    if [ -f /run/secrets/ca-bundle ]; then export SSL_CERT_FILE=/run/secrets/ca-bundle; fi \
    && uv sync --no-dev --no-editable

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=build /app/approval/notifier/.venv /app/approval/notifier/.venv
RUN useradd --system --uid 10003 gxg
USER gxg
ENV PATH=/app/approval/notifier/.venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["gxg-approval-notifier"]
