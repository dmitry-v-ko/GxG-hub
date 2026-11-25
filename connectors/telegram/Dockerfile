# Production image of the Telegram connector (stage D, read-only, M11).
# Build from the repository root: docker build -f connectors/telegram/Dockerfile .
# The kit comes in by path source (connectors/telegram/pyproject.toml); versions from connectors/telegram/uv.lock.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_FROZEN=1
WORKDIR /app
COPY packages/gxg-connector-kit packages/gxg-connector-kit
COPY schemas/search-result.schema.json schemas/
COPY connectors/telegram connectors/telegram
WORKDIR /app/connectors/telegram
# Optional secret `ca-bundle`: a TLS-intercepting proxy's CA for the download only, never stored in a layer.
RUN --mount=type=secret,id=ca-bundle,required=false \
    if [ -f /run/secrets/ca-bundle ]; then export SSL_CERT_FILE=/run/secrets/ca-bundle; fi \
    && uv sync --no-dev

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=build /app /app
RUN useradd --system --uid 10001 telegram && mkdir -p /var/lib/telegram && chown telegram /var/lib/telegram
USER telegram
ENV PATH=/app/connectors/telegram/.venv/bin:$PATH PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/var/lib/telegram"]
CMD ["python", "-m", "telegram_connector"]
