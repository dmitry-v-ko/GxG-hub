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
  approval/src approval/tests scripts conftest.py tests/ci tests/secrets
uv run mypy tools/gxg-policy/src tools/gxg-policy/tests connectors/demo/src connectors/demo/tests tests/contract
uv run pytest
uv run python scripts/ci/schema_lock.py
(cd connectors/demo && uv sync --locked && uv run pytest -q)
uv run python scripts/ci/prove_schema_guard.py
scripts/ci/gitleaks.sh tree HEAD
scripts/ci/gitleaks.sh range "0000000000000000000000000000000000000000..HEAD"
uv run pytest tests/secrets
scripts/ci/mutmut.sh >/dev/null
echo "check-all: OK"
