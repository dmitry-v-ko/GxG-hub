"""AC-M10-05: the alert rules load in Prometheus without errors (promtool check, then a real Prometheus with the
deployment config), and their unit tests pass — among them an outcome_unknown older than 1 h raising the alert.

Needs a Docker daemon (the Prometheus image, pinned by digest).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
RULES = ROOT / "approval/deploy/prometheus"
IMAGE = "prom/prometheus:v3.5.0@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996"


def promtool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{RULES}:/r:ro", "-w", "/r", "--entrypoint",
         "promtool", IMAGE, *args], capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip


def test_rules_are_valid() -> None:
    run = promtool("check", "rules", "rules.yaml")
    assert run.returncode == 0 and "SUCCESS: 13 rules found" in run.stdout, run.stdout + run.stderr


def test_rule_unit_tests_pass() -> None:
    run = promtool("test", "rules", "rules.test.yaml")
    assert run.returncode == 0 and "SUCCESS" in run.stdout, run.stdout + run.stderr


def test_prometheus_loads_the_rules() -> None:
    name = f"gxg-prom-{time.monotonic_ns()}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--network", "none", "-v", f"{RULES}:/etc/prometheus:ro",
         IMAGE, "--config.file=/etc/prometheus/prometheus.yml"], check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    try:
        doc: dict[str, object] = {}
        for _ in range(60):
            out = subprocess.run(["docker", "exec", name, "wget", "-qO-", "http://127.0.0.1:9090/api/v1/rules"],
                                 capture_output=True, text=True, timeout=30, check=False)  # fmt: skip
            if out.returncode == 0:
                doc = json.loads(out.stdout)
                break
            time.sleep(1)
        groups = doc["data"]["groups"]  # type: ignore[index]
        rules = [r for g in groups for r in g["rules"]]
        assert len(rules) == 13 and all(r["health"] in ("ok", "unknown") for r in rules), rules
        assert {r["name"] for r in rules} >= {"GxgOutcomeUnknownOverAnHour", "GxgSwitchChanged", "GxgWebhookDead"}
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=60, check=False)
