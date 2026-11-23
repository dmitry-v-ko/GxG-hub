"""AC-M7-13, configuration part (AS-28): each process gets only its own secrets; secrets are never env values."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())  # also from mutmut's copy
COMPOSE = REPO / "approval/deploy/compose.yaml"
# AS-28 «Свои секреты на процесс», for the processes of M7, M8 and M9. A secret not in this table is a finding.
AS28 = {
    "postgres": {"db_superuser_password"},  # the DBA side, not a process of the service
    "migrate": {"db_dsn_owner"},  # password of the owner role: only the migration job
    "api": {"db_dsn_api"},  # its role; payload *public* keys and JWKS are configuration
    "mcp": {"svc_key_mcp"},  # its service JWT key; no database role in use
    "sweeper": {"db_dsn_sweeper"},
    # its role, its service JWT key, the args (not preview) payload keys; signing keys: a directory (below)
    "executor": {"db_dsn_executor", "svc_key_executor", "payload_args_keys"},
    # M9: OIDC client secret, session key, preview key (decrypt) and its TLS key — only the ui
    "ui": {"db_dsn_ui", "ui_session_key", "oidc_client_secret_ui", "preview_keys_ui", "ui_tls_key"},
    "notifier": {"db_dsn_notifier", "telegram_bot_token"},  # bot token only here; webhook secrets: a directory
    "decider": {"db_dsn_decider"},  # M10: its role only; it holds no key (the passkeys' public keys are rows)
    "admin": {"db_dsn_admin"},  # M10: its role; the operator's token comes per request
    "admin-ingress": set(),
    "ingress": set(),
    "oidc-egress": set(),
    "telegram-egress": set(),
    "oidc": set(),  # test profile
    "fake-telegram": set(),  # test profile (it mounts the test token as a plain file)
}
# AS-29: networks per service. Every network but `edge` (inbound) and `egress` (outbound relays) is internal.
NETWORKS = {
    "postgres": {"internal", "ui_db", "notifier_db"},
    "migrate": {"internal"},
    "api": {"internal"},
    "mcp": {"internal"},
    "sweeper": {"internal"},
    "executor": {"internal", "connectors"},  # only the executor reaches the connectors (SEC-14)
    "ui": {"ui_ingress", "ui_db", "ui_oidc", "ui_decider"},
    "decider": {"internal", "ui_decider"},  # AS-30: reached by the ui only, no tailnet
    "admin": {"internal", "admin_ingress", "admin_oidc"},
    "admin-ingress": {"edge", "admin_ingress"},
    "ingress": {"edge", "ui_ingress"},
    "notifier": {"notifier_db", "notifier_telegram", "notifier_webhooks"},
    "oidc-egress": {"egress", "ui_oidc", "admin_oidc"},
    "telegram-egress": {"egress", "notifier_telegram"},
    "oidc": {"ui_oidc"},
    "fake-telegram": {"notifier_telegram"},
}
SECRETISH = re.compile(r"(?i)(password|passwd|secret|token|private|dsn|seed)")


def load() -> dict[str, Any]:
    doc = yaml.safe_load(COMPOSE.read_text("utf-8"))
    assert isinstance(doc, dict)
    return doc


def test_every_process_gets_exactly_its_secrets() -> None:
    services = load()["services"]
    assert {name: set(s.get("secrets", [])) for name, s in services.items()} == AS28


def test_each_secret_belongs_to_one_process() -> None:
    owners: dict[str, list[str]] = {}
    for name, s in load()["services"].items():
        for secret in s.get("secrets", []):
            owners.setdefault(secret, []).append(name)
    assert {k: v for k, v in owners.items() if len(v) > 1} == {}
    assert set(owners) == set(load()["secrets"])


def test_no_secret_value_in_environment() -> None:
    for name, s in load()["services"].items():
        for key, value in (s.get("environment") or {}).items():
            if key == "<<":
                continue
            if SECRETISH.search(key):  # a secret file, or a directory of them (GXG_WEBHOOK_SECRETS_DIR)
                assert key.endswith(("_FILE", "_DIR")) and str(value).startswith("/run/secrets/"), (name, key)
            assert not re.search(r"password=|://[^/@\s]+:[^/@\s]+@|-----BEGIN", str(value)), (name, key)


def test_secret_files_are_mounted_where_the_process_reads_them() -> None:
    for name, s in load()["services"].items():
        env = s.get("environment") or {}
        files = {
            v for k, v in env.items() if k != "<<" and not k.endswith("_DIR") and str(v).startswith("/run/secrets/")
        }
        assert files == {f"/run/secrets/{x}" for x in s.get("secrets", [])}, name
        for k, v in env.items():  # a secret directory is a read-only mount of its own
            if k.endswith("_DIR") and str(v).startswith("/run/secrets/"):
                assert any(str(m).endswith(f":{v}:ro") for m in s.get("volumes", [])), (name, k)


def test_internal_processes_have_no_egress() -> None:
    """AS-29, SEC-13 (AC-M9-12, configuration part; the stand test proves it with real containers)."""
    doc = load()
    assert {n: (c or {}).get("internal") is True for n, c in doc["networks"].items()} == {
        n: n not in ("edge", "egress") for n in doc["networks"]
    }
    assert {name: set(s["networks"]) for name, s in doc["services"].items()} == NETWORKS
    # the ui's HTTPS; the admin API on the host loopback only (tailscale serve, SEC-13)
    assert {name for name, s in doc["services"].items() if "ports" in s} == {"ingress", "admin-ingress"}
    assert doc["services"]["admin-ingress"]["ports"] == ["127.0.0.1:${GXG_ADMIN_PUBLISH:-8092}:8092"]
    assert doc["services"]["ingress"]["command"] == ["TCP-LISTEN:8443,fork,reuseaddr", "TCP:ui:8443"]
    assert doc["services"]["telegram-egress"]["command"][-1] == "TCP:api.telegram.org:443"
    for name in ("oidc", "fake-telegram", "oidc-egress", "telegram-egress"):
        assert doc["services"][name]["profiles"] in (["test"], ["egress"]), name


def test_db_tests_gate_ci_ok() -> None:
    """The real-PostgreSQL tests (AC-M7-02, 04, 06…13) are a required part of CI."""
    jobs = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text("utf-8"))["jobs"]
    assert "approval-db" in jobs["ci-ok"]["needs"]
    assert any(s.get("run") == "uv run pytest approval/tests_db" for s in jobs["approval-db"]["steps"])


def test_executor_signing_keys_come_from_a_directory_only_it_mounts() -> None:
    """AS-10 without restart: rotation replaces the file atomically, which a single-file mount would not show."""
    services = load()["services"]
    path = services["executor"]["environment"]["GXG_EXEC_SIGNING_KEYS_FILE"]
    mounts = {m.split(":")[1]: m for m in services["executor"]["volumes"]}
    directory = str(Path(path).parent)
    assert mounts[directory] == f"./exec-keys:{directory}:ro"
    others = [m for name, s in services.items() if name != "executor" for m in s.get("volumes", [])]
    assert not [m for m in others if m.startswith("./exec-keys")]  # AS-28: nobody else sees the seeds
    jwks = services["api"]["environment"]["GXG_EXEC_JWKS_FILE"]
    assert any(m == f"./config:{Path(jwks).parent}:ro" for m in services["api"]["volumes"])  # a directory too


def test_host_secret_files_are_ignored_by_git() -> None:
    """The operator writes the seeds and secrets next to compose.yaml (README): `git add -A` must not take them."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (export or build context): nothing to commit by mistake")
    for path in ("approval/deploy/exec-keys/signing-keys.json", "approval/deploy/secrets/svc_key_executor"):
        r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path], check=False)
        assert r.returncode == 0, path
