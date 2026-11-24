# ruff: noqa: E501
"""deploy/railway/entrypoint.sh with stub tailscale binaries, and the Railway Dockerfiles."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "deploy/railway/entrypoint.sh"
AUTHKEY = "tskey-auth-MARKER-0123456789"
SERVE = "443 /servers http://127.0.0.1:4444/servers\n443 /health http://127.0.0.1:4444/health\n8443 - http://127.0.0.1:4444\n"


def stubs(tmp_path: Path, *, funnel: str = "No serve config") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "tailscaled").write_text(f'#!/bin/sh\necho "tailscaled $*" >>{tmp_path}/calls\n')
    (bin_dir / "tailscale").write_text(
        f"""#!/bin/sh
echo "tailscale $*" >>{tmp_path}/calls
case "$*" in *"funnel status"*) echo "{funnel}";; esac
case "$*" in *"status --json"*) echo '{{"MagicDNSSuffix": "tail1.ts.net", "CertDomains": ["h.tail1.ts.net"], "Peer": {{"k": {{"DNSName": "other.tail1.ts.net."}}}}, "Self": {{"HostName": "h", "DNSName": "h-2.tail1.ts.net."}}}}';; esac
for a in "$@"; do case "$a" in --auth-key=file:*) cat "${{a#--auth-key=file:}}" >{tmp_path}/key-seen;; esac; done
"""
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def run_entry(
    tmp_path: Path, extra: dict[str, str], cmd: str, *, funnel: str = "No serve config"
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{stubs(tmp_path, funnel=funnel)}:{os.environ['PATH']}",
        "GXG_FILES_DIR": str(tmp_path / "files"),
        "TS_STATE_DIR": str(tmp_path / "ts"),
        "GXG_TS_SELFCHECK": "0",
        **extra,
    }
    return subprocess.run(
        ["sh", str(ENTRY), "sh", "-c", cmd], env=env, capture_output=True, text=True, check=False, timeout=30
    )


def test_secrets_become_files_and_leave_the_environment(tmp_path: Path) -> None:
    dsn = "postgresql://gxg_api:pw-MARKER@db:5432/x"
    keys = '[{"seed": "abc"}]\nsecond line'
    done = run_entry(
        tmp_path,
        {
            "GXG_SECRET__GXG_DB_DSN": dsn,
            "GXG_CONFIG__GXG_EXEC_JWKS": keys,
            "GXG_SECRET__WEBHOOK__signals": "hmac-MARKER",
        },
        'cat "$GXG_DB_DSN_FILE"; echo; cat "$GXG_EXEC_JWKS_FILE"; echo; '
        'cat "$GXG_WEBHOOK_SECRETS_DIR/signals"; echo; env',
    )
    assert done.returncode == 0, done.stderr
    assert dsn in done.stdout and keys in done.stdout and "hmac-MARKER" in done.stdout  # through the files
    env_part = done.stdout.split("hmac-MARKER", 1)[1]
    assert "GXG_SECRET__" not in env_part and "GXG_CONFIG__" not in env_part  # the child does not see the variables
    assert oct((tmp_path / "files/GXG_DB_DSN").stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "calls").exists()  # no TS_HOSTNAME: no tailscale


def test_tailnet_entry_userspace_serve_rules_and_no_funnel(tmp_path: Path) -> None:
    done = run_entry(
        tmp_path,
        {"TS_HOSTNAME": "gxg-gateway", "TS_TAGS": "tag:gateway", "TS_AUTHKEY": AUTHKEY, "GXG_TS_SERVE": SERVE},
        "echo key=${TS_AUTHKEY-unset}",
    )
    assert done.returncode == 0, done.stderr
    assert "key=unset" in done.stdout  # the app never sees the key
    calls = (tmp_path / "calls").read_text()
    assert "tailscaled --tun=userspace-networking" in calls
    assert "--advertise-tags=tag:gateway" in calls and "--advertise-exit-node=false" in calls
    assert "serve --bg --https=443 --set-path=/servers http://127.0.0.1:4444/servers" in calls
    assert "serve --bg --https=8443 http://127.0.0.1:4444" in calls
    assert "serve --bg --https=443 http://127.0.0.1:4444" not in calls  # `/admin` is not mapped on 443
    assert [c for c in calls.splitlines() if " funnel " in c] == [c for c in calls.splitlines() if "funnel status" in c]
    assert AUTHKEY not in calls and AUTHKEY not in done.stdout + done.stderr  # the key goes through a file only
    assert (tmp_path / "key-seen").read_text() == AUTHKEY and not (tmp_path / "ts/authkey").exists()


def test_refuses_to_start_with_funnel_on(tmp_path: Path) -> None:
    done = run_entry(
        tmp_path,
        {"TS_HOSTNAME": "h", "TS_TAGS": "tag:gateway", "TS_AUTHKEY": AUTHKEY, "GXG_TS_SERVE": SERVE},
        "echo started",
        funnel="Funnel on",
    )
    assert done.returncode == 1 and "started" not in done.stdout and "funnel is on" in done.stderr


def test_refuses_to_start_without_the_owners_key(tmp_path: Path) -> None:
    done = run_entry(tmp_path, {"TS_HOSTNAME": "h", "TS_TAGS": "tag:gateway", "GXG_TS_SERVE": SERVE}, "echo started")
    assert done.returncode != 0 and "TS_AUTHKEY is not set" in done.stderr and "started" not in done.stdout


def test_entrypoint_and_images_never_enable_funnel_or_publish_a_port() -> None:
    script = ENTRY.read_text()
    assert not re.search(r"tailscale[^\n]*funnel(?! status)", script.replace("$TS funnel status", ""))
    for name in ("contextforge", "ui", "notifier", "approval"):
        text = (ROOT / f"deploy/railway/{name}.Dockerfile").read_text()
        assert "/opt/gxg/entrypoint.sh" in text and "TS_AUTHKEY" not in text  # the key is never in an image
    assert "tailscale" in (ROOT / "deploy/railway/ui.Dockerfile").read_text()


@pytest.mark.parametrize(("name", "cmd"), [("ui", "gxg-approval-ui"), ("notifier", "gxg-approval-notifier")])
def test_wrapper_dockerfiles_still_contain_the_original_build(name: str, cmd: str) -> None:
    original = (ROOT / f"approval/{name}/Dockerfile").read_text().rstrip("\n")
    wrapper = (ROOT / f"deploy/railway/{name}.Dockerfile").read_text()
    build_stage = original.split("USER gxg")[0]
    # the wrapper drops the optional ca-bundle secret mount (a sandbox-proxy convenience Railway cannot use)
    build_stage = re.sub(r"RUN --mount=type=secret.*?&& uv sync", "RUN uv sync", build_stage, flags=re.DOTALL)
    assert build_stage in wrapper  # the build (and so the lockfile and the base digests) is the one AC-M9-12 tests
    assert wrapper.rstrip().endswith(f'ENTRYPOINT ["/opt/gxg/entrypoint.sh"]\nCMD ["{cmd}"]')


def test_no_railway_dockerfile_declares_a_volume() -> None:
    """Railway rejects the VOLUME instruction ("use Railway Volumes"); the volumes are created by railway_deploy.py."""
    files = sorted((ROOT / "deploy/railway").glob("*.Dockerfile"))
    assert len(files) >= 4
    for f in files:
        assert not re.search(r"^\s*VOLUME\b", f.read_text(), re.MULTILINE | re.IGNORECASE), f.name


def test_missing_owner_variables_stop_the_start_with_a_clear_message(tmp_path: Path) -> None:
    done = run_entry(tmp_path, {"GXG_REQUIRE": "GXG_OIDC_ISSUER GXG_SECRET__X", "GXG_SECRET__X": "set"}, "echo started")
    assert done.returncode == 78 and "started" not in done.stdout
    assert "the owner has to set: GXG_OIDC_ISSUER" in done.stderr and "GXG_SECRET__X" not in done.stderr
    (tmp_path / "again").mkdir()
    ok = run_entry(tmp_path / "again", {"GXG_REQUIRE": "A B", "A": "1", "B": "2"}, "echo started")
    assert ok.returncode == 0 and "started" in ok.stdout


def test_a_second_pass_through_the_entrypoint_only_execs(tmp_path: Path) -> None:
    """Railway may call the entrypoint again with an explicit start command: files stay, tailscale is not restarted."""
    env = {
        "PATH": f"{stubs(tmp_path)}:{os.environ['PATH']}",
        "GXG_FILES_DIR": str(tmp_path / "f"),
        "TS_STATE_DIR": str(tmp_path / "ts"),
        "GXG_TS_SELFCHECK": "0",
    }
    env |= {
        "GXG_SECRET__GXG_DB_DSN": "dsn-MARKER",
        "TS_HOSTNAME": "h",
        "TS_TAGS": "tag:x",
        "TS_AUTHKEY": AUTHKEY,
        "GXG_TS_SERVE": SERVE,
    }
    cmd = ["sh", str(ENTRY), "sh", str(ENTRY), "sh", "-c", 'cat "$GXG_DB_DSN_FILE"']
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0 and done.stdout == "dsn-MARKER", done.stderr
    assert (tmp_path / "calls").read_text().count("tailscaled --tun=userspace-networking") == 1


def test_root_start_takes_the_volume_and_drops_to_the_service_user(tmp_path: Path) -> None:
    """Railway mounts volumes root-owned: as root the entrypoint chowns them and re-runs itself as `gxg`."""
    volume = tmp_path / "vol"
    volume.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    (bin_dir / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && echo 0 || exit 0\n')
    (bin_dir / "chown").write_text(f'#!/bin/sh\necho "chown $*" >>{log}\n')
    (bin_dir / "setpriv").write_text(f'#!/bin/sh\necho "setpriv $*" >>{log}\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GXG_VOLUME_DIRS": str(volume)}
    done = subprocess.run(["sh", str(ENTRY), "true"], env=env, capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0, done.stderr
    calls = log.read_text()
    assert f"chown gxg {volume}" in calls
    assert "setpriv --reuid=gxg --regid=gxg --init-groups" in calls and str(ENTRY) in calls


def test_tailnet_selfcheck_logs_names_and_status_codes_only(tmp_path: Path) -> None:
    bin_dir = stubs(tmp_path)
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\nfor a; do last="$a"; done\necho "$last" >>{tmp_path}/urls\nprintf "code=200"\n'
    )
    (bin_dir / "curl").chmod(0o755)
    done = run_entry(
        tmp_path,
        {
            "TS_HOSTNAME": "h",
            "TS_TAGS": "tag:x",
            "TS_AUTHKEY": AUTHKEY,
            "GXG_TS_SERVE": "443 - http://127.0.0.1:8443",
            "GXG_TS_SELFCHECK": "1",
            "GXG_SELFCHECK_DELAY": "0",
            "GXG_LISTEN": "127.0.0.1:8443",
            "GXG_SECRET__X": "secret-MARKER",
        },
        "true",
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert (
        "tailnet=tail1.ts.net" in out and 'cert_domains=["h.tail1.ts.net"]' in out
    )  # from status --json, not typed in
    assert "selfcheck: app http://127.0.0.1:8443/login code=200" in out
    assert "requested=h.tail1.ts.net actual=h-2.tail1.ts.net" in out and "WARNING the node did not get" in out
    assert (
        "selfcheck: tailnet https://h-2.tail1.ts.net/login code=200" in out
    )  # the name it really got, not the requested one
    assert "--socks5-server=localhost:1055" in (tmp_path / "calls").read_text()
    assert AUTHKEY not in out + done.stderr and "secret-MARKER" not in out + done.stderr
