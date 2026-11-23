"""scripts/ci/mutmut.sh as check-all.sh calls it (no ALL_RESULTS_FILE argument), with `uv` stubbed in PATH.

The stub plays mutmut for one directory: one survivor, which is listed in scripts/ci/mutmut-equivalent.txt. The
script must find it in the list (M1.1 cycle 1 read the run mutants from /dev/null and failed every such call).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LISTED = "gxg_connector_check.bundle.x_write__mutmut_13"  # an M1.1 equivalent of the list
STUB = """#!/usr/bin/env bash
# uv run --quiet mutmut <command> ...
shift 3
case "$1" in
  run) mkdir -p mutants ;;
  export-cicd-stats) echo '{"killed": 1, "survived": 1}' > mutants/mutmut-cicd-stats.json ;;
  results)
    if [ "${2:-}" = "--all" ]; then echo "    gxg_connector_check.bundle.x_b64__mutmut_1: killed"; fi
    echo "    $SURVIVOR: survived" ;;
esac
"""

pytestmark = pytest.mark.e2e


def run(tmp_path: Path, survivor: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "uv").write_text(STUB, encoding="utf-8")
    (tmp_path / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SURVIVOR": survivor,
           "MUTMUT_DIRS": "tools/gxg-connector-check"}  # fmt: skip
    return subprocess.run([str(REPO / "scripts" / "ci" / "mutmut.sh"), str(tmp_path / "report.txt")], env=env,
                          capture_output=True, text=True, timeout=120, check=False)  # fmt: skip


def test_a_listed_survivor_passes_without_the_second_argument(tmp_path: Path) -> None:
    proc = run(tmp_path, LISTED)
    report = (tmp_path / "report.txt").read_text(encoding="utf-8")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not killed: 1, listed as equivalent: 1" in report and "mutmut: OK" in report


def test_an_unlisted_survivor_fails(tmp_path: Path) -> None:
    proc = run(tmp_path, "gxg_connector_check.bundle.x_write__mutmut_99")
    assert proc.returncode == 1
    assert "SURVIVED, not equivalent:\ngxg_connector_check.bundle.x_write__mutmut_99" in proc.stdout
