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
  approval/src approval/tests scripts tests/secrets
# separate run: two conftest.py modules clash in one mypy run; tests/ci imports the root conftest
uv run mypy --strict conftest.py tests/ci
uv run mypy tools/gxg-policy/src tools/gxg-policy/tests connectors/demo/src connectors/demo/tests tests/contract
MYPYPATH=stand/tests:stand:gateway/contextforge uv run mypy stand gateway/contextforge
uv run pytest
uv run python scripts/ci/schema_lock.py
uv run pytest packages/gxg-connector-kit/tests -q --cov=gxg_connector_kit --cov-branch --cov-report=json:coverage-kit.json
uv run python scripts/ci/check_coverage.py coverage-kit.json
(cd connectors/demo && uv sync --locked && uv run pytest -q)
uv run python scripts/ci/prove_schema_guard.py
scripts/ci/gitleaks.sh tree HEAD
scripts/ci/gitleaks.sh range "0000000000000000000000000000000000000000..HEAD"
uv run pytest tests/secrets
uv run gxg-policy validate policies/
uv run gxg-policy lint policies/
bash scripts/ci/bundle_smoke.sh
connectors/demo/docker_smoke.sh  # needs a Docker daemon; extra build flags: DOCKER_BUILD_FLAGS
stand/run.sh  # M5 dev stand end to end (needs a Docker daemon); extra build flags: DOCKER_BUILD_FLAGS
scripts/ci/mutmut.sh >/dev/null
echo "check-all: OK"
