#!/usr/bin/env bash
# Local equivalent of .github/workflows/ci.yml. Run from the repository root.
set -euo pipefail
cd "$(dirname "$0")/../.."

uv lock --check
(cd connectors/demo && uv lock --check)
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict packages/gxg-connector-kit/src packages/gxg-connector-kit/tests \
  approval/src approval/tests scripts tests/ci
uv run mypy --strict conftest.py  # separate run: two conftest.py modules clash in one mypy run
uv run mypy tools/gxg-policy/src tools/gxg-policy/tests connectors/demo/src connectors/demo/tests tests/contract
uv run pytest
uv run python scripts/ci/schema_lock.py
uv run pytest packages/gxg-connector-kit/tests -q --cov=gxg_connector_kit --cov-branch --cov-report=json:coverage-kit.json
uv run python scripts/ci/check_coverage.py coverage-kit.json
(cd connectors/demo && uv sync --locked && uv run pytest -q)
uv run python scripts/ci/prove_schema_guard.py
uv run python scripts/ci/check_secrets.py --tree HEAD
echo "check-all: OK"
