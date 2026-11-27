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
from typing import Any

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
        {
            "MCC-05",
            "MC-02",
            "MC-03",
            "MCC-09",
            "SC-02",
            "SC-03",
            "SC-04",
            "SC-05",
            "SC-10",
            "EX-16",
        },  # EX-16: propose too
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
    # the harness counts no provider call: positive control (SC-10; EX-04, EX-11 count the provider sends of demo M8)
    "11-calls-always-zero": ("SC-10", {"SC-10", "EX-04", "EX-11"}),
    "12-failures-only-crash": ("MC-03", {"MC-03"}),  # the harness hides the provider's failure modes
    "13-hidden-board-dropped": ("SC-05", {"SC-05"}),  # a named board without a grant is silently dropped
    "14-empty-is-not-found": ("MC-02", {"MC-02"}),  # an empty search is an error
    "15-cursor-never": ("MC-02", {"MC-02"}),  # a cut page has no next_cursor
    "16-cursor-unbound": ("MC-02", {"MC-02"}),  # another principal can use the cursor
    "17-query-in-audit": ("SC-09", {"SC-09"}),  # the caller's query in the audit line
    "18-backend-always-ready": ("MCC-09", {"MCC-09"}),  # /readyz ignores the backend
    "19-query-logged-on-error": ("SC-09", {"SC-09"}),  # the query in a log line on a provider error only
}
FIRST_TEN = sorted(CASES)[:10]
EX = {"EX-01", "EX-02", "EX-03", "EX-04", "EX-05", "EX-11", "EX-14", "EX-16"}
pytestmark = pytest.mark.e2e  # child processes: mutmut's trampolines do not reach them


def copy_of(connector: Path, tmp: Path) -> Path:
    """The connector in a repository-shaped tree: its manifest refers to ../../schemas/search-result.schema.json."""
    dest = tmp / "connectors" / connector.name
    shutil.copytree(connector, dest, ignore=shutil.ignore_patterns(".venv", "mutants", "__pycache__", ".pytest_cache"))
    (tmp / "schemas").mkdir()
    shutil.copy(REPO / "schemas" / "search-result.schema.json", tmp / "schemas")
    return dest


def check(root: Path, tmp: Path) -> tuple[int, dict[str, dict[str, Any]], str]:
    report = tmp / "report.json"
    cmd = [sys.executable, "-m", "gxg_connector_check.cli", "--connector", str(root), "--report", str(report)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False, cwd=tmp)
    return proc.returncode, json.loads(report.read_text(encoding="utf-8")), proc.stdout + proc.stderr


def test_demo_passes(tmp_path: Path) -> None:
    code, report, out = check(DEMO, tmp_path)
    assert code == 0, out
    # demo has effects since M8 (demo.message.propose): the EX checks apply to it and pass
    expected = {c for c, _ in CASES.values()} | {"MC-01"} | EX
    assert {k for k, v in report.items() if v["status"] == "passed"} == expected
    assert len(CASES) == 19
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


def test_operator_only_tool(tmp_path: Path) -> None:
    """M6 (kb.collections.list): `Call.caller="operator"` runs the tool as Harness.operator; demo with an operator
    variant of `demo.boards.list` passes, and the same tool answering the reader is red on SC-10 only."""
    root = copy_of(DEMO, tmp_path)
    subprocess.run(["git", "apply", str(BROKEN / "operator" / "base.patch")], cwd=root, check=True, timeout=60)
    code, report, out = check(root, tmp_path)
    assert code == 0, out
    assert report["SC-10"]["checks"] == [
        "test_grants_filter_and_no_provider_call_without_grants[asyncio]",
        "test_operator_only_tools_answer_nobody_else[asyncio]",
    ]
    subprocess.run(
        ["git", "apply", str(BROKEN / "operator" / "answers-reader.patch")], cwd=root, check=True, timeout=60
    )
    code, report, out = check(root, tmp_path)
    failed = {k for k, v in report.items() if v["status"] == "failed"}
    assert code == 1 and failed == {"SC-10"}, (failed, out[-3000:])
    assert any("operator-only tool" in e for e in report["SC-10"]["errors"]), report["SC-10"]


@pytest.mark.parametrize(
    ("name", "failed"),
    [
        ("demo.ext.describe", set()),  # a registry read (MCC-11): no provider call, no resource, one card for all
        ("demo.ext.search", set()),
        ("demo.ext.call", {"SC-03", "SC-10"}),  # runs an entry: the rules of an ordinary tool (MCC-11 п.4)
        ("demo.entry.get", {"SC-03", "SC-10"}),  # an ordinary tool answering the same without the provider
    ],
)
def test_registry_reads_are_exempt_only_by_name(name: str, failed: set[str], tmp_path: Path) -> None:
    """`<domain>.ext.search`/`.describe` return registry entries: SC-03 and SC-10 do not ask them for a provider
    call, resource ids or a per-tenant difference. The same tool under any other name is red on both."""
    root = copy_of(DEMO, tmp_path)
    subprocess.run(["git", "apply", str(BROKEN / "registry" / "base.patch")], cwd=root, check=True, timeout=60)
    for rel in ("gxg-manifest.yaml", "src/demo_connector/tools.py", "src/demo_connector/conformance.py"):
        path = root / rel
        path.write_text(path.read_text(encoding="utf-8").replace("demo.ext.describe", name), encoding="utf-8")
    code, report, out = check(root, tmp_path)
    assert {k for k, v in report.items() if v["status"] == "failed"} == failed, out[-3000:]
    assert code == (1 if failed else 0), out[-3000:]
