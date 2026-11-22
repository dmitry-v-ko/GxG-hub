"""AC-M7-13, configuration part (AS-28): each process gets only its own secrets; secrets are never env values."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())  # also from mutmut's copy
COMPOSE = REPO / "approval/deploy/compose.yaml"
# AS-28 «Свои секреты на процесс», for the processes of M7 and M8. A secret not in this table is a finding.
AS28 = {
    "postgres": {"db_superuser_password"},  # the DBA side, not a process of the service
    "migrate": {"db_dsn_owner"},  # password of the owner role: only the migration job
    "api": {"db_dsn_api"},  # its role; payload *public* keys and JWKS are configuration
    "mcp": {"svc_key_mcp"},  # its service JWT key; no database role in use
    "sweeper": {"db_dsn_sweeper"},
    # its role, its service JWT key, the args (not preview) payload keys; signing keys: a directory (below)
    "executor": {"db_dsn_executor", "svc_key_executor", "payload_args_keys"},
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
            if SECRETISH.search(key):
                assert key.endswith("_FILE") and str(value).startswith("/run/secrets/"), (name, key)
            assert not re.search(r"password=|://[^/@\s]+:[^/@\s]+@|-----BEGIN", str(value)), (name, key)


def test_secret_files_are_mounted_where_the_process_reads_them() -> None:
    for name, s in load()["services"].items():
        files = {v for k, v in (s.get("environment") or {}).items() if k != "<<" and str(v).startswith("/run/secrets/")}
        assert files == {f"/run/secrets/{x}" for x in s.get("secrets", [])}, name


def test_internal_processes_have_no_egress() -> None:
    doc = load()
    assert doc["networks"]["internal"]["internal"] is True
    assert doc["networks"]["connectors"]["internal"] is True
    for name, s in doc["services"].items():
        # only the executor reaches the connectors' network (SEC-14)
        assert s["networks"] == (["internal", "connectors"] if name == "executor" else ["internal"]), name
        assert "ports" not in s, name  # nothing is published outside (SEC-13)


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
