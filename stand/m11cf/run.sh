#!/usr/bin/env bash
# M11.3/M11.4 live check (reports/M11-cf-stand.md): the dev stand + gxg-telegram-service:m11-4-local, project gxg-m11cf
# so it never touches a running `gxg-stand`. Needs the service image built locally (gxg-telegram-service repository).
#   stand/m11cf/run.sh          up, bootstrap, stand suite (SC/MC/EX/M11: MC-05) with the service registered
#   KEEP=1 stand/m11cf/run.sh   leave it running; `docker compose down -v` with the same two variables removes it
set -euo pipefail
cd "$(dirname "$0")/../.."
export COMPOSE_PROJECT_NAME=gxg-m11cf COMPOSE_FILE=compose.yaml:stand/m11cf/compose.yaml
uv run python stand/init.py
# shellcheck disable=SC2086 # DOCKER_BUILD_FLAGS is a list of flags
docker build -q ${DOCKER_BUILD_FLAGS:-} --build-arg KEEP_TEST_CONTROLS=1 -t gxg-demo:stand -f connectors/demo/Dockerfile . >/dev/null
# shellcheck disable=SC2086
docker build -q ${DOCKER_BUILD_FLAGS:-} -t gxg-telegram:stand -f connectors/telegram/Dockerfile . >/dev/null
uv run python stand/m11cf/setup.py init
docker compose up -d --wait
uv run python stand/bootstrap.py
uv run python stand/m11cf/setup.py bootstrap
mkdir -p stand/secrets/junit
status=0
uv run pytest stand/tests -q -p no:cacheprovider --force-enable-socket -o pythonpath="stand/tests stand gateway/contextforge" \
  --junitxml=stand/secrets/junit/m11cf.xml || status=1
uv run python stand/m11cf/checks.py all || status=1
[ "${KEEP:-}" = 1 ] || docker compose down -v
exit "$status"
