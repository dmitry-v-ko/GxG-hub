#!/bin/sh
# Container wrapper for every Hub service on Railway (SEC-28, AS-28, ADR-0003). Usage: entrypoint.sh CMD [ARGS...]
#
# 1. Files from variables. The processes read secrets as files (`<NAME>_FILE`, AS-28); Railway has only variables:
#      GXG_SECRET__<NAME>   -> $GXG_FILES_DIR/<NAME> (0400), exports <NAME>_FILE
#      GXG_CONFIG__<NAME>   -> the same for public configuration
#      GXG_SECRET__WEBHOOK__<path> -> $GXG_FILES_DIR/webhooks/<path>, exports GXG_WEBHOOK_SECRETS_DIR
#      GXG_BUNDLE_B64 (base64 tar.gz of the signed bundle) -> $GXG_FILES_DIR/bundle, exports GXG_BUNDLE_DIR
#    The variables are unset before the process starts; nothing is written outside tmpfs (/dev/shm).
# 2. Tailnet entry (only when TS_HOSTNAME is set): tailscaled in userspace-networking mode, `tailscale up` with
#    the key from TS_AUTHKEY (set by the owner in Railway, never in Git), `tailscale serve` HTTPS rules from
#    GXG_TS_SERVE ("<https-port> <path|-> <target>" per line). Funnel is never enabled; start refuses if it is on.
# Nothing here prints a variable value.
set -eu

# Railway mounts volumes root-owned. Started as root (the images do not set USER), take the volume directories over and
# re-run this script as the unprivileged service user before anything else: no secret file or process ever belongs to root.
if [ "$(id -u)" = 0 ] && id gxg >/dev/null 2>&1; then
  for dir in ${GXG_VOLUME_DIRS:-/var/lib/gxg /var/lib/telegram}; do
    [ -d "$dir" ] && chown gxg "$dir"
  done
  exec setpriv --reuid=gxg --regid=gxg --init-groups "$0" "$@"
fi

# Railway may run this script twice (ENTRYPOINT and an explicit start command): the second pass only execs.
if [ -n "${GXG_ENTRYPOINT_DONE:-}" ]; then exec "$@"; fi
export GXG_ENTRYPOINT_DONE=1

# GXG_REQUIRE: names the owner must set; a clear message instead of a crash deep inside the process. Exit 78 is a
# configuration error: with the restart policy ON_FAILURE and a retry limit the service stops and waits.
missing=""
for name in ${GXG_REQUIRE:-}; do
  [ -n "$(printenv "$name")" ] || missing="$missing $name"
done
if [ -n "$missing" ]; then
  echo "entrypoint: not started, the owner has to set:$missing (docs/runbooks/m13-pilot-deploy.md)" >&2
  exit 78
fi
unset GXG_REQUIRE

FILES="${GXG_FILES_DIR:-/dev/shm/gxg-files}"
umask 077
mkdir -p "$FILES"

materialise() { # prefix
  for var in $(env | sed -n "s/^\($1[A-Za-z0-9_]*\)=.*/\1/p"); do
    name="${var#"$1"}"
    case "$name" in
      WEBHOOK__*)
        rel="${name#WEBHOOK__}"
        mkdir -p "$FILES/webhooks"
        printf '%s' "$(printenv "$var")" >"$FILES/webhooks/$rel"
        export GXG_WEBHOOK_SECRETS_DIR="$FILES/webhooks"
        ;;
      *)
        printf '%s' "$(printenv "$var")" >"$FILES/$name"
        export "${name}_FILE=$FILES/$name"
        ;;
    esac
    unset "$var"
  done
}
materialise GXG_SECRET__
materialise GXG_CONFIG__

if [ -n "${GXG_BUNDLE_B64:-}" ]; then
  mkdir -p "$FILES/bundle"
  printf '%s' "$GXG_BUNDLE_B64" | base64 -d | tar -xz -C "$FILES/bundle"
  export GXG_BUNDLE_DIR="$FILES/bundle"
  unset GXG_BUNDLE_B64
fi

if [ -n "${TS_HOSTNAME:-}" ]; then
  : "${TS_AUTHKEY:?TS_AUTHKEY is not set: the owner sets it in Railway (docs/runbooks/m13-pilot-deploy.md)}"
  : "${TS_TAGS:?TS_TAGS is not set}"
  : "${GXG_TS_SERVE:?GXG_TS_SERVE is not set}"
  TSDIR="${TS_STATE_DIR:-/dev/shm/tailscale}"
  mkdir -p "$TSDIR"
  printf '%s' "$TS_AUTHKEY" >"$TSDIR/authkey"
  unset TS_AUTHKEY
  tailscaled --tun=userspace-networking --state=mem: --socket="$TSDIR/tailscaled.sock" >/dev/null 2>&1 &
  TS="tailscale --socket=$TSDIR/tailscaled.sock"
  i=0
  until $TS version >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 50 ] || { echo "entrypoint: tailscaled did not start" >&2; exit 1; }
    sleep 0.2
  done
  $TS up --auth-key="file:$TSDIR/authkey" --hostname="$TS_HOSTNAME" --advertise-tags="$TS_TAGS" \
    --accept-dns=false --accept-routes=false --advertise-exit-node=false >/dev/null
  rm -f "$TSDIR/authkey"
  printf '%s\n' "$GXG_TS_SERVE" | while read -r port path target; do
    [ -n "$port" ] || continue
    if [ "$path" = "-" ]; then
      $TS serve --bg --https="$port" "$target" >/dev/null
    else
      $TS serve --bg --https="$port" --set-path="$path" "$target" >/dev/null
    fi
  done
  if $TS funnel status 2>&1 | grep -qi 'funnel on'; then
    echo "entrypoint: funnel is on, refusing to start (SEC-27)" >&2
    exit 1
  fi
fi

exec "$@"
