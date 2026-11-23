"""AC-M9-12 on real containers: ui and notifier images (own lockfiles), the compose networks of AS-29, and
connection attempts from inside each container — allowed ones succeed, everything else is refused.

Needs a Docker daemon. Run from the repository root: `uv run pytest approval/deploy/tests`.
Extra `docker build` flags (e.g. a proxy CA secret in a sandbox): DOCKER_BUILD_FLAGS.
"""

from __future__ import annotations

import datetime
import json
import os
import random
import secrets
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.e2e
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
COMPOSE = ROOT / "approval/deploy/compose.yaml"
SECRETS = ("db_superuser_password", "db_dsn_owner", "db_dsn_api", "db_dsn_sweeper", "svc_key_mcp", "db_dsn_ui",
           "ui_session_key", "oidc_client_secret_ui", "preview_keys_ui", "ui_tls_key", "db_dsn_notifier",
           "telegram_bot_token", "db_dsn_decider", "db_dsn_admin")  # fmt: skip
PROBE = (
    "import socket, sys\n"
    "for target in sys.argv[1:]:\n"
    "    host, port = target.rsplit(':', 1)\n"
    "    try:\n"
    "        socket.create_connection((host, int(port)), timeout=3).close(); print(target, 'open')\n"
    "    except OSError as exc:\n"
    "        print(target, 'refused', type(exc).__name__)\n"
)
LISTENING = (
    "import os\n"
    "files = [f for f in ('/proc/net/tcp', '/proc/net/tcp6') if os.path.exists(f)]\n"
    "rows = [l.split() for f in files for l in open(f).readlines()[1:]]\n"
    "print(sum(1 for r in rows if r[3] == '0A' and not r[1].startswith('0B00007F:')))\n"
)  # sockets in state LISTEN, except Docker's embedded DNS on 127.0.0.11 (dockerd's, not the process's)
OUTSIDE = ["1.1.1.1:443", "8.8.8.8:53", "example.com:443", "api.telegram.org:443"]


def sh(*cmd: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    run = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900, check=False)
    if check and run.returncode != 0:
        raise AssertionError(f"{shlex.join(cmd)}\n{run.stdout[-2000:]}\n{run.stderr[-3000:]}")
    return run.stdout


def tls_pair(directory: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    (directory / "config/ui-tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "secrets/ui_tls_key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


@pytest.fixture(scope="module")
def stand(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    flags = shlex.split(os.environ.get("DOCKER_BUILD_FLAGS", ""))
    for name in ("ui", "notifier"):
        sh("docker", "build", "-q", *flags, "-t", f"gxg-approval-{name}:m9-test", "-f",
           str(ROOT / f"approval/{name}/Dockerfile"), str(ROOT))  # fmt: skip
    sh("docker", "build", "-q", *flags, "-t", "gxg-test-oidc:m9", "-f",
       str(ROOT / "approval/deploy/test-oidc.Dockerfile"), str(ROOT))  # fmt: skip
    work = tmp_path_factory.mktemp("stand")
    for d in ("secrets", "config", "bundle", "secrets/webhooks"):
        (work / d).mkdir()
    password = secrets.token_urlsafe(16)
    for name in SECRETS:
        (work / "secrets" / name).write_text("unused")
    (work / "secrets/db_superuser_password").write_text(password)
    for name in ("db_dsn_ui", "db_dsn_notifier"):
        (work / "secrets" / name).write_text(f"host=postgres dbname=postgres user=postgres password={password}")
    (work / "secrets/ui_session_key").write_text(secrets.token_urlsafe(32))
    (work / "secrets/preview_keys_ui").write_text("{}")
    (work / "secrets/telegram_bot_token").write_text("1:stand-test-token")
    tls_pair(work)
    for path in work.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)  # the containers' users read the files
    port = random.randint(20000, 45000)  # noqa: S311 (a test port)
    project = f"gxg-m9-net-{secrets.token_hex(3)}"
    env = os.environ | {
        "GXG_SECRETS_DIR": str(work / "secrets"),
        "GXG_CONFIG_DIR": str(work / "config"),
        "GXG_BUNDLE_HOST_DIR": str(work / "bundle"),
        "GXG_APPROVAL_IMAGE": "unused:latest",  # api, mcp, sweeper are not started here
        "GXG_UI_IMAGE": "gxg-approval-ui:m9-test",
        "GXG_NOTIFIER_IMAGE": "gxg-approval-notifier:m9-test",
        "GXG_BUNDLE_KEYS": "{}",
        "GXG_UI_PUBLIC_URL": f"https://localhost:{port}",
        "GXG_OIDC_ISSUER": "https://oidc:9400",  # checked for https at start, fetched only at login
        "GXG_OIDC_CLIENT_ID": "stand",
        "GXG_UI_PUBLISH": f"127.0.0.1:{port}",
        "GXG_TELEGRAM_API_URL": "http://fake-telegram:8081",
        "GXG_ADMIN_OPERATORS": "oidc:https://oidc:9400:operator",  # admin is not started here
    }
    compose = ["docker", "compose", "-p", project, "-f", str(COMPOSE), "--profile", "test"]
    try:
        sh(
            *compose,
            "up",
            "-d",
            "--no-build",
            "postgres",
            "ui",
            "ingress",
            "notifier",
            "oidc",
            "fake-telegram",
            env=env,
        )
        yield {"compose": compose, "env": env, "port": port}
    finally:
        sh(*compose, "down", "-v", "--remove-orphans", env=env, check=False)


def probe(stand: dict[str, Any], service: str, *targets: str) -> dict[str, str]:
    deadline = time.time() + 60
    while True:
        out = sh(*stand["compose"], "exec", "-T", service, "python", "-c", PROBE, *targets, env=stand["env"])
        result = {line.split()[0]: line.split()[1] for line in out.splitlines()}
        # the servers inside the stand may still be starting: retry until every expected-open one answers
        if time.time() > deadline or all(v == "open" for k, v in result.items() if k in stand.get("expect", ())):
            return result
        time.sleep(1)


def test_images_have_their_own_lockfile_and_tree(stand: dict[str, Any]) -> None:
    """AS-29: separate images, each from its own uv.lock; neither ships the other's or the api's code."""
    for name in ("ui", "notifier"):
        assert (ROOT / f"approval/{name}/uv.lock").exists()
        assert f"approval/{name}/" in (ROOT / f"approval/{name}/Dockerfile").read_text()
    mods = (
        "import importlib.util as u; print([m for m in ('gxg_approval', 'fastmcp', 'alembic', {0}) if u.find_spec(m)])"
    )
    assert sh("docker", "run", "--rm", "gxg-approval-ui:m9-test", "python", "-c",
              mods.format("'gxg_approval_notifier'")).strip() == "[]"  # fmt: skip
    assert sh("docker", "run", "--rm", "gxg-approval-notifier:m9-test", "python", "-c",
              mods.format("'gxg_approval_ui', 'nacl', 'fastapi', 'webauthn'")).strip() == "[]"  # fmt: skip


def test_ui_reaches_only_db_and_oidc(stand: dict[str, Any]) -> None:
    stand["expect"] = ("postgres:5432", "oidc:9400")
    result = probe(stand, "ui", "postgres:5432", "oidc:9400", "fake-telegram:8081", *OUTSIDE)
    assert result == {"postgres:5432": "open", "oidc:9400": "open", "fake-telegram:8081": "refused",
                      **dict.fromkeys(OUTSIDE, "refused")}  # fmt: skip


def test_notifier_reaches_only_db_and_telegram(stand: dict[str, Any]) -> None:
    stand["expect"] = ("postgres:5432", "fake-telegram:8081")
    result = probe(stand, "notifier", "postgres:5432", "fake-telegram:8081", "oidc:9400", "ui:8443", *OUTSIDE)
    assert result == {"postgres:5432": "open", "fake-telegram:8081": "open", "oidc:9400": "refused",
                      "ui:8443": "refused", **dict.fromkeys(OUTSIDE, "refused")}  # fmt: skip


def test_notifier_accepts_no_connection(stand: dict[str, Any]) -> None:
    """AS-29: «входящих нет» — no listening socket in the notifier, no published port."""
    out = sh(*stand["compose"], "exec", "-T", "notifier", "python", "-c", LISTENING, env=stand["env"])
    assert out.strip() == "0"
    ps = json.loads(
        "[" + ",".join(sh(*stand["compose"], "ps", "--format", "json", env=stand["env"]).split("\n")[:-1]) + "]"
    )
    published = {c["Service"]: [p for p in c.get("Publishers") or [] if p.get("PublishedPort")] for c in ps}
    assert {s for s, p in published.items() if p} == {"ingress"}


def test_ui_is_reached_from_outside_over_https_only(stand: dict[str, Any]) -> None:
    url = f"https://127.0.0.1:{stand['port']}/healthz"
    for _ in range(60):
        body = sh("curl", "-sk", "--max-time", "3", url, check=False)
        if body:
            break
        time.sleep(1)
    assert json.loads(body) == {"status": "ok"}
    plain = subprocess.run(["curl", "-s", "--max-time", "3", f"http://127.0.0.1:{stand['port']}/healthz"],
                           capture_output=True, text=True, check=False)  # fmt: skip
    assert '"status"' not in plain.stdout  # plain HTTP gets no page: TLS only


def test_control_egress_is_visible_outside_the_policy(stand: dict[str, Any]) -> None:
    """Positive control: from `ingress` (on the non-internal `edge`) the same address answers, so the refusals
    above come from the networks, not from a sandbox without internet."""
    run = subprocess.run(
        [*stand["compose"], "exec", "-T", "ingress", "socat", "-T3", "/dev/null", "TCP:1.1.1.1:443,connect-timeout=3"],
        env=stand["env"], capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr
