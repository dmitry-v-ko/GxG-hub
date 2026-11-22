# Production image of the reference connector `demo`: MCP on 8080; with DEMO_APPROVAL_URL also the internal
# execute/reconcile port 8081, which only the executor may reach (SEC-14).
# Build from the repository root: docker build -f connectors/demo/Dockerfile .
# The kit comes in by path source (connectors/demo/pyproject.toml); versions from connectors/demo/uv.lock.
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_FROZEN=1
WORKDIR /app
COPY packages/gxg-connector-kit packages/gxg-connector-kit
COPY schemas/search-result.schema.json schemas/
COPY connectors/demo connectors/demo
WORKDIR /app/connectors/demo
# The dev stand (compose.yaml) builds with KEEP_TEST_CONTROLS=1; the default production image drops them.
ARG KEEP_TEST_CONTROLS=0
# Optional secret `ca-bundle`: a TLS-intercepting proxy's CA for the download only, never stored in a layer.
RUN --mount=type=secret,id=ca-bundle,required=false \
    if [ -f /run/secrets/ca-bundle ]; then export SSL_CERT_FILE=/run/secrets/ca-bundle; fi \
    && uv sync --no-dev \
    # test controls are not shipped in the production image (demo-connector-v1, fake provider)
    && if [ "$KEEP_TEST_CONTROLS" != 1 ]; then rm src/demo_connector/testctl.py; fi

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
COPY --from=build /app /app
RUN useradd --system --uid 10001 demo && mkdir -p /var/lib/demo && chown demo /var/lib/demo
USER demo
ENV PATH=/app/connectors/demo/.venv/bin:$PATH PYTHONUNBUFFERED=1
EXPOSE 8080 8081
VOLUME ["/var/lib/demo"]
CMD ["python", "-m", "demo_connector"]
