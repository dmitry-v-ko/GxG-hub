#!/usr/bin/env bash
# Local equivalent of .github/workflows/ci.yml. Run from the repository root.
set -euo pipefail
cd "$(dirname "$0")/../.."

uv lock --check
(cd connectors/demo && uv lock --check)
(cd approval/ui && uv lock --check)  # AS-29: own lockfiles
(cd approval/notifier && uv lock --check)
(cd connectors/telegram && uv lock --check)
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict packages/gxg-connector-kit/src packages/gxg-connector-kit/tests \
  approval/src approval/tests scripts tests/secrets
# separate run: two conftest.py modules clash in one mypy run; tests/ci imports the root conftest
uv run mypy --strict conftest.py tests/ci
uv run mypy --strict approval/tests_db
uv run mypy --strict approval/ui/src approval/ui/tests approval/notifier/src approval/notifier/testing approval/deploy/tests
uv run mypy --strict approval/ui/tests_db
uv run mypy --strict approval/notifier/tests_db
uv run mypy tools/gxg-policy/src tools/gxg-policy/tests connectors/demo/src connectors/demo/tests \
  connectors/telegram/src connectors/telegram/tests tests/contract
MYPYPATH=stand/tests:stand:gateway/contextforge:connectors/telegram/archive uv run mypy --exclude mutants stand gateway/contextforge
uv run pytest
uv run pytest approval/tests_db  # needs Docker (M7)
uv run pytest approval/ui/tests_db approval/notifier/tests_db  # M9; Docker, node + Playwright Chromium (E2E)
uv run pytest approval/deploy/tests  # AC-M9-12: images and networks; extra build flags: DOCKER_BUILD_FLAGS
uv run python scripts/ci/schema_lock.py
uv run pytest packages/gxg-connector-kit/tests -q --cov=gxg_connector_kit --cov-branch --cov-report=json:coverage-kit.json
uv run python scripts/ci/check_coverage.py coverage-kit.json
(cd connectors/demo && uv sync --locked && uv run pytest -q)
(cd connectors/telegram && uv sync --locked && uv run pytest -q)
# ADR-0004: the telegram_archive schema contract against gxg_agents_v2 (pinned commit and newer migrations).
# Needs a clone (GXG_AGENTS_V2, default ../gxg_agents_v2); UPSTREAM_DRIFT=skip only where no clone can exist.
if [ "${UPSTREAM_DRIFT:-}" != skip ]; then
  upstream="${GXG_AGENTS_V2:-../gxg_agents_v2}"
  git -C "$upstream" rev-parse --git-dir >/dev/null 2>&1 || { echo "check-all: no gxg_agents_v2 clone at $upstream (GXG_AGENTS_V2)" >&2; exit 1; }
  connectors/telegram/archive/check_upstream.sh "$upstream"
  connectors/telegram/archive/check_upstream.sh --drift "$upstream"
fi
uv run python scripts/ci/prove_schema_guard.py
scripts/ci/gitleaks.sh tree HEAD
scripts/ci/gitleaks.sh range "0000000000000000000000000000000000000000..HEAD"
uv run pytest tests/secrets
uv run gxg-policy validate policies/
uv run gxg-policy lint policies/
bash scripts/ci/bundle_smoke.sh
connectors/demo/docker_smoke.sh  # needs a Docker daemon; extra build flags: DOCKER_BUILD_FLAGS
stand/run.sh  # M5 + M11 dev stand end to end (needs a Docker daemon); extra build flags: DOCKER_BUILD_FLAGS
scripts/ci/mutmut.sh >/dev/null
echo "check-all: OK"
