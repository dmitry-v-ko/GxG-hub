"""AC-M1.1-02: check-connectors passes on `demo` and fails on each broken copy, each on its own check.

A broken copy is `connectors/demo` with one patch from broken/demo/ applied (`git apply`); the check that must turn
red is in CASES. The suite runs as `python -m gxg_connector_check.cli` in a child process: the copy's package
shadows the installed `demo_connector` there (the plugin puts the copy's src/ first).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = next(p for p in Path(__file__).resolve().parents if (p / "connectors" / "demo").is_dir())
DEMO = REPO / "connectors" / "demo"
BROKEN = Path(__file__).with_name("broken")
CASES = {  # patch -> (the check that catches it, every check that fails: an exact set, so nothing fails by accident)
    # a call without identity runs as a default principal
    "01-no-identity": ("SC-02", {"SC-02", "SC-04", "MCC-05"}),
    # X-Tenant-Id overrides the verified tenant
    "02-tenant-from-header": ("SC-03", {"SC-03"}),
    # the named resource is looked up (in every tenant) before the identity check
    "03-resource-before-identity": ("SC-04", {"SC-04", "SC-10"}),
    # the backend is called before anything is known to be granted
    "04-provider-before-grants": ("SC-10", {"SC-10"}),
    # a hidden resource is distinguishable from a missing one (GR-17)
    "05-hidden-is-not-not-found": ("SC-05", {"SC-05"}),
    # errors as plain text without the envelope: every check that reads an error code fails too
    "06-error-outside-envelope": (
        "MCC-05",
        {"MCC-05", "MC-02", "MC-03", "MCC-09", "SC-02", "SC-03", "SC-04", "SC-05", "SC-10"},
    ),
    # a search without max_results, `limit` up to 100000
    "07-unbounded-output": ("MC-02", {"MC-02"}),
    # 429 is not retryable
    "08-rate-limit-not-retryable": ("MC-03", {"MC-03"}),
    # the provider credential in the audit line
    "09-secret-in-audit": ("SC-09", {"SC-09"}),
    # /readyz is 200 without a valid bundle
    "10-always-ready": ("MCC-09", {"MCC-09"}),
    # cycle 2 (critic M1.1-cycle-1): the harness itself, lists, logs, readiness
    "11-calls-always-zero": ("SC-10", {"SC-10"}),  # the harness counts no provider call: positive control
    "12-failures-only-crash": ("MC-03", {"MC-03"}),  # the harness hides the provider's failure modes
    "13-hidden-board-dropped": ("SC-05", {"SC-05"}),  # a named board without a grant is silently dropped
    "14-empty-is-not-found": ("MC-02", {"MC-02"}),  # an empty search is an error
    "15-cursor-never": ("MC-02", {"MC-02"}),  # a cut page has no next_cursor
    "16-cursor-unbound": ("MC-02", {"MC-02"}),  # another principal can use the cursor
    "17-query-in-audit": ("SC-09", {"SC-09"}),  # the caller's query in the audit line
    "18-backend-always-ready": ("MCC-09", {"MCC-09"}),  # /readyz ignores the backend
}
FIRST_TEN = sorted(CASES)[:10]
pytestmark = pytest.mark.e2e  # child processes: mutmut's trampolines do not reach them


def copy_of(connector: Path, tmp: Path) -> Path:
    """The connector in a repository-shaped tree: its manifest refers to ../../schemas/search-result.schema.json."""
    dest = tmp / "connectors" / connector.name
    shutil.copytree(connector, dest, ignore=shutil.ignore_patterns(".venv", "mutants", "__pycache__", ".pytest_cache"))
    (tmp / "schemas").mkdir()
    shutil.copy(REPO / "schemas" / "search-result.schema.json", tmp / "schemas")
    return dest


def check(root: Path, tmp: Path) -> tuple[int, dict[str, dict[str, str]], str]:
    report = tmp / "report.json"
    cmd = [sys.executable, "-m", "gxg_connector_check.cli", "--connector", str(root), "--report", str(report)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False, cwd=tmp)
    return proc.returncode, json.loads(report.read_text(encoding="utf-8")), proc.stdout + proc.stderr


def test_demo_passes(tmp_path: Path) -> None:
    code, report, out = check(DEMO, tmp_path)
    assert code == 0, out
    assert {k for k, v in report.items() if v["status"] == "passed"} == {c for c, _ in CASES.values()} | {"MC-01"}
    assert len(CASES) == 18
    assert all(v["status"] in ("passed", "not_applicable") for v in report.values()), report


def test_every_broken_copy_has_its_own_check() -> None:
    """AC-M1.1-02: ten copies, each on its own check; the later ones add positive controls of those checks."""
    assert len({CASES[n][0] for n in FIRST_TEN}) == 10
    assert sorted(p.stem for p in (BROKEN / "demo").glob("*.patch")) == sorted(CASES)


def test_an_intact_copy_passes(tmp_path: Path) -> None:
    """The copy itself is not what makes the broken copies fail."""
    code, _, out = check(copy_of(DEMO, tmp_path), tmp_path)
    assert code == 0, out


@pytest.mark.parametrize("name", sorted(CASES))
def test_broken_copy_fails_on_its_check(name: str, tmp_path: Path) -> None:
    root = copy_of(DEMO, tmp_path)
    subprocess.run(["git", "apply", str(BROKEN / "demo" / f"{name}.patch")], cwd=root, check=True, timeout=60)
    code, report, out = check(root, tmp_path)
    own, expected = CASES[name]
    failed = {k for k, v in report.items() if v["status"] == "failed"}
    assert code == 1 and own in failed, f"{name}: {own} did not catch it\n{out}"
    assert failed == expected, (
        f"{name}: failed {sorted(failed)}, expected {sorted(expected)}\n{json.dumps(report, indent=1)}"
    )
