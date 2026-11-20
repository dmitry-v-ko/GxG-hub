"""AC-M1-09 gate: scripts/ci/check_coverage.py fails when any module is below 90 % branch coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/check_coverage.py"
MODULES = ("exec_token", "svc_jwt", "grants", "canonical", "identity", "_jwt")


def run(tmp_path: Path, branches: dict[str, tuple[int, int]]) -> int:
    files = {
        f"packages/gxg-connector-kit/src/gxg_connector_kit/{m}.py": {
            "summary": {"covered_branches": c, "num_branches": t}
        }
        for m, (c, t) in branches.items()
    }
    report = tmp_path / "cov.json"
    report.write_text(json.dumps({"files": files}))
    return subprocess.run([sys.executable, str(SCRIPT), str(report)], check=False).returncode


def test_all_modules_at_threshold_pass(tmp_path: Path) -> None:
    assert run(tmp_path, {m: (9, 10) for m in MODULES}) == 0


def test_one_module_below_threshold_fails(tmp_path: Path) -> None:
    assert run(tmp_path, {**{m: (10, 10) for m in MODULES}, "grants": (89, 100)}) == 1


def test_missing_module_fails(tmp_path: Path) -> None:
    assert run(tmp_path, {m: (10, 10) for m in MODULES if m != "identity"}) == 1


def test_module_without_branches_passes(tmp_path: Path) -> None:
    assert run(tmp_path, {**{m: (10, 10) for m in MODULES}, "identity": (0, 0)}) == 0
