#!/usr/bin/env bash
# M5 end to end on the dev stand (AC-M5-01, AC-M5-04): init, build, up, bootstrap, the stage B suite through
# ContextForge; then every container restarted, and then recreated (same volumes), each time the same suite
# with no setup in between. Writes reports/conformance/M5.json and M6.json (M6: the KB wrapper on the same
# stand). Needs Docker with compose and uv.
#   stand/run.sh            (extra `docker build` flags, e.g. a proxy CA secret: DOCKER_BUILD_FLAGS)
#   KEEP=1 stand/run.sh     leave the stand running afterwards
set -euo pipefail
cd "$(dirname "$0")/.."
out=stand/secrets/junit
uv run python stand/init.py
# shellcheck disable=SC2086 # DOCKER_BUILD_FLAGS is a list of flags
docker build -q ${DOCKER_BUILD_FLAGS:-} --build-arg KEEP_TEST_CONTROLS=1 -t gxg-demo:stand \
  -f connectors/demo/Dockerfile . > /dev/null
# shellcheck disable=SC2086
docker build -q ${DOCKER_BUILD_FLAGS:-} -t gxg-kb:stand -f connectors/kb/Dockerfile . > /dev/null  # M6
docker compose up -d --wait
uv run python stand/bootstrap.py
mkdir -p "$out"
suite() {  # suite <label>: the live tests (they need sockets: ContextForge on 127.0.0.1:4444, docker)
  uv run pytest stand/tests -q -p no:cacheprovider --force-enable-socket \
    -o pythonpath="stand/tests stand gateway/contextforge" --junitxml="$out/$1.xml" || true
}
suite fresh
docker compose restart                     # AC-M5-04: all containers, no setup afterwards
docker compose up -d --wait > /dev/null    # only waits for health: nothing to recreate
suite after-restart
docker compose down                        # containers removed, volumes and stand/secrets kept
docker compose up -d --wait
suite after-recreate
[ "${KEEP:-}" = 1 ] || docker compose down
runs=(fresh="$out/fresh.xml" after-restart="$out/after-restart.xml" after-recreate="$out/after-recreate.xml")
rc=0  # both reports are written; any red run fails the script
uv run python stand/report.py --exclude stand.tests.test_kb "${runs[@]}" || rc=1  # M5: its own tests only
uv run python stand/report.py --milestone M6 "${runs[@]}" || rc=1  # M6: the M5 suite (regression) + the KB tests
exit "$rc"
