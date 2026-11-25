# Planned cron job `bundle-reissue` (deploy/railway/services.yaml `planned`, NOT deployed): re-signs the grants bundle
# every few hours with scripts/deploy/pilot_bundle.py --deploy. Build context: the repository root. It carries the
# policy tooling and the pilot policies, never a key: the signing key is read from `bundle-signer` through the Railway
# API, in memory. RAILWAY_TOKEN is the only secret of this job.
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
WORKDIR /srv
COPY scripts/deploy scripts/deploy
COPY deploy/railway/services.yaml deploy/railway/services.yaml
COPY policies/pilot policies/pilot
RUN touch scripts/__init__.py scripts/deploy/__init__.py && useradd --system --uid 10004 reissue
USER reissue
# no .git in a Railway build: the bundle version is RAILWAY_GIT_COMMIT_SHA (pilot_bundle.py)
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["python", "scripts/deploy/pilot_bundle.py", "--deploy"]
