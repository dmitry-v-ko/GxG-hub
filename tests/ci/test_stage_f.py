"""AC-M14-06: the stage F mapper covers every id of the Conformance Matrix, and only with tests that exist."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scripts.ci.stage_f", ROOT / "scripts" / "ci" / "stage_f.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


F = load()
IDS = F.matrix_ids(F.MATRIX.read_text("utf-8"))


def test_the_matrix_has_the_expected_prefixes() -> None:
    prefixes = ("SC", "EX", "MC", "SI", "OPS")
    assert {i.split("-")[0] for i in IDS} == set(prefixes)
    assert [sum(i.startswith(p + "-") for i in IDS) for p in prefixes] == [10, 25, 5, 3, 7]


def test_every_matrix_id_is_mapped_and_every_mapped_test_exists() -> None:
    assert F.problems(IDS, F.mapping()) == []


def test_an_unmapped_matrix_id_a_stray_mapping_and_a_missing_test_are_reported() -> None:
    entries = F.mapping()
    entries.pop("SC-01")
    entries["ZZ-01"] = {"tests": ["stand/tests/test_sc.py::test_nope"]}
    bad = "\n".join(F.problems(IDS, entries))
    assert "SC-01: in the matrix, no mapping" in bad
    assert "ZZ-01: mapped, not in the matrix" in bad
    assert "test_nope: no such top-level test function" in bad


def test_an_open_id_is_never_passed_and_has_a_reason() -> None:
    for i, e in F.OPEN.items():
        assert e["status"] in ("manual", "not_run") and e["reason"], i
    rows = {r["id"]: r for r in F.build(F.mapping(), {})}
    assert all(rows[i]["status"] != "passed" for i in F.OPEN)


def test_status_of_a_test_list() -> None:
    stand, unit = "stand/tests/test_sc.py::t", "approval/tests/test_x.py::t"
    assert F.judge([stand, unit], {stand: [True], unit: [True, True]})[0] == "passed"
    assert F.judge([unit], {unit: [True, False]})[0] == "failed"
    assert F.judge([stand], {})[0] == "not_run"  # no stand JUnit given
    assert F.judge([unit], {})[0] == "failed"  # a non-stand test must come back from pytest
    assert F.judge([stand, unit], {unit: [True]})[0] == "not_run"


def test_mc05_follows_its_evidence_file(tmp_path: Path) -> None:
    (tmp_path / "ev.md").write_text("status: passed\n", "utf-8")
    assert F.build({"MC-05": {"evidence": "ev.md"}}, {}, tmp_path)[0]["status"] == "passed"
    (tmp_path / "ev.md").write_text("status: failed\n", "utf-8")
    assert F.build({"MC-05": {"evidence": "ev.md"}}, {}, tmp_path)[0]["status"] == "failed"
