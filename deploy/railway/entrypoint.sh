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
# GXG_RUN_AS: the user to become (default `gxg`; a numeric uid for an image whose user has no name, ContextForge: 10001).
run_as="${GXG_RUN_AS:-gxg}"
if [ "$(id -u)" = 0 ]; then
  case "$run_as" in
    *[!0-9]*) groups="--init-groups"; known=$(id "$run_as" >/dev/null 2>&1 && echo yes || true) ;;
    *) groups="--clear-groups"; known=yes ;;
  esac
  if [ -n "${known:-}" ]; then
    for dir in ${GXG_VOLUME_DIRS:-/var/lib/gxg /var/lib/telegram}; do
      [ -d "$dir" ] && chown "$run_as" "$dir"
    done
    if [ -z "${GXG_DROP_PYTHON:-}" ] && command -v setpriv >/dev/null 2>&1; then
      exec setpriv --reuid="$run_as" --regid="$run_as" $groups "$0" "$@"
    fi
    # an image without util-linux (ContextForge): the same drop with python
    py="$(command -v python3 || echo /app/.venv/bin/python)"
    exec "$py" -c 'import os, pwd, sys
user = sys.argv[1]
try:
    entry = pwd.getpwnam(user)
    uid, gid = entry.pw_uid, entry.pw_gid
except KeyError:
    uid = gid = int(user)
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
os.execv(sys.argv[2], sys.argv[2:])' "$run_as" "$0" "$@"
  fi
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
  SELFCHECK="${GXG_TS_SELFCHECK:-1}"
  socks=""
  [ "$SELFCHECK" = 1 ] && socks="--socks5-server=localhost:1055" # loopback only: the self-check reaches its own tailnet name through it
  # GXG_TS_STATE: a directory on the service's volume. The node (and so its name) then survives restarts and deploys;
  # without it the state is in memory and every start registers a new node (`<name>-2`, `-3`, ...).
  state="mem:"
  have_state=""
  if [ -n "${GXG_TS_STATE:-}" ]; then
    mkdir -p "$GXG_TS_STATE"
    chmod 700 "$GXG_TS_STATE"
    state="$GXG_TS_STATE/tailscaled.state"
    [ -s "$state" ] && have_state=1
  fi
  tailscaled --tun=userspace-networking --state="$state" --socket="$TSDIR/tailscaled.sock" $socks >/dev/null 2>&1 &
  TS="tailscale --socket=$TSDIR/tailscaled.sock"
  i=0
  until $TS version >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 50 ] || { echo "entrypoint: tailscaled did not start" >&2; exit 1; }
    sleep 0.2
  done
  up_flags="--hostname=$TS_HOSTNAME --advertise-tags=$TS_TAGS --accept-dns=false --accept-routes=false --advertise-exit-node=false"
  joined=""
  if [ -n "$have_state" ]; then # known node: no key needed; if the control plane forgot it, fall back to the key
    # shellcheck disable=SC2086
    $TS up --timeout=30s $up_flags >/dev/null 2>&1 && joined=1 && echo "entrypoint: rejoined the tailnet with the saved node state"
  fi
  if [ -z "$joined" ]; then
    # shellcheck disable=SC2086
    $TS up --auth-key="file:$TSDIR/authkey" $up_flags >/dev/null
  fi
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

  # Self-check (log only: tailnet names, serve rules, HTTP status codes; no variable value, no response body).
  if [ "$SELFCHECK" = 1 ]; then
    echo "selfcheck: tailscale status"
    $TS status --peers=false 2>&1 || true
    echo "selfcheck: tailscale serve status"
    $TS serve status 2>&1 || true
    json="$($TS status --json 2>/dev/null || true)"
    suffix="$(printf '%s' "$json" | sed -n 's/.*"MagicDNSSuffix": *"\([^"]*\)".*/\1/p' | head -n 1)"
    # an empty CertDomains means HTTPS certificates are not enabled for the tailnet: `serve --https` cannot terminate TLS
    echo "selfcheck: tailnet=${suffix:-unknown} cert_domains=$(printf '%s' "$json" | tr -d '\n ' | sed -n 's/.*"CertDomains":\(\[[^]]*\]\).*/\1/p')"
    # The name the node really got: a taken name makes tailscale register `<name>-2`, `-3`, ... (an old ephemeral node is still
    # listed), and then the URL the owner uses points at the old node, not at this one.
    self_dns="$(printf '%s' "$json" | tr -d '\n' | sed 's/.*"Self": *//' | grep -o '"DNSName": *"[^"]*"' | head -n 1 | sed 's/.*: *"\(.*\)"/\1/; s/\.$//')"
    echo "selfcheck: requested=$TS_HOSTNAME.${suffix:-?} actual=${self_dns:-unknown}"
    case "${self_dns:-}" in "$TS_HOSTNAME.$suffix") ;; *) echo "selfcheck: WARNING the node did not get the requested name: remove the stale machine from the tailnet and redeploy" ;; esac
    (
      sleep "${GXG_SELFCHECK_DELAY:-15}"
      port="${GXG_LISTEN##*:}"
      [ -n "${GXG_LISTEN:-}" ] || port="${PORT:-}"
      paths="${GXG_SELFCHECK_PATHS:-/healthz /login}"
      if ! command -v curl >/dev/null 2>&1; then echo "selfcheck: curl is not installed in this image"; exit 0; fi
      for path in $paths; do
        res="$(curl -sS -o /dev/null -m 10 -w 'code=%{http_code}' "http://127.0.0.1:$port$path" 2>&1)" || true
        echo "selfcheck: app http://127.0.0.1:$port$path $res"
      done
      for path in $paths; do
        res="$(curl -sS -o /dev/null -m 20 --socks5-hostname localhost:1055 -w 'code=%{http_code}' "https://${self_dns:-$TS_HOSTNAME.$suffix}$path" 2>&1)" || true
        echo "selfcheck: tailnet https://${self_dns:-$TS_HOSTNAME.$suffix}$path $res"
      done
    ) &
  fi
fi

exec "$@"
