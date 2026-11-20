"""AC-M2-01 (validate on the repo examples) and AC-M2-02 (lint finds every prepared error)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gxg_policy.lint import lint
from gxg_policy.load import load

HERE = Path(__file__).parent
REPO = HERE.parents[2]
BAD = sorted(p for p in (HERE / "bad").iterdir() if p.is_dir())
GOOD = HERE / "good"
NOW = datetime(2026, 11, 20, tzinfo=UTC)


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "gxg_policy.cli", *args], capture_output=True, text=True, check=False)


def manifests(case: Path) -> list[Path]:
    return [case / "manifest.yaml"] if (case / "manifest.yaml").exists() else []


def test_ac_m2_01_validate_repo_policies() -> None:
    r = cli("validate", str(REPO / "policies"))
    assert r.returncode == 0, r.stderr
    assert load(REPO / "policies").findings == []


def test_repo_policies_lint_clean() -> None:
    r = cli("lint", str(REPO / "policies"))
    assert r.returncode == 0, r.stderr
    assert "0 error(s)" in r.stdout


def test_good_fixture_is_clean() -> None:
    assert lint(GOOD / "policies", manifests(GOOD), NOW).findings == []


def test_ac_m2_02_has_all_required_cases() -> None:
    codes = {json.loads((c / "expected.json").read_text())["code"] for c in BAD}
    assert len(BAD) >= 12
    required = {
        "dangling-subject", "foreign-principal", "rid-domain", "forbidden-mask", "duplicate-id",
        "auto-approve-high-risk", "approver-role", "approvers-empty-intersection", "ttl-exceeds-default", "schema",
    }  # fmt: skip
    assert required <= codes


@pytest.mark.parametrize("case", BAD, ids=[c.name for c in BAD])
def test_ac_m2_02_lint_finds_error(case: Path) -> None:
    exp = json.loads((case / "expected.json").read_text())
    errors = lint(case / "policies", manifests(case), NOW).errors
    # Exactly the one planted error: the fixture differs from the good set in one place.
    assert [(e.file, e.path, e.code) for e in errors] == [(exp["file"], exp["path"], exp["code"])]
    args = ["lint", str(case / "policies")] + [a for m in manifests(case) for a in ("--manifest", str(m))]
    r = cli(*args)
    assert r.returncode == 1
    assert f"{exp['file']}: {exp['path'] or '<root>'}: [{exp['code']}]" in r.stderr


def _copy_good(tmp_path: Path) -> Path:
    root = tmp_path / "policies"
    shutil.copytree(GOOD / "policies", root)
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_expiry_warnings_do_not_fail(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    _edit(root / "tenants/t-alpha.yaml", "    resources: [kb:kb-main/collection/public]\n",
          "    resources: [kb:kb-main/collection/public]\n    expires_at: 2026-11-25T00:00:00Z\n")  # fmt: skip
    pol = lint(root, manifests(GOOD), NOW)
    assert [(f.code, f.level) for f in pol.findings] == [("grant-expiring", "warning")]
    pol = lint(root, manifests(GOOD), datetime(2027, 1, 1, tzinfo=UTC))
    assert [(f.code, f.level) for f in pol.findings] == [("grant-expired", "warning")]


def test_timestamp_without_offset_is_an_error(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    _edit(root / "tenants/t-alpha.yaml", "    resources: [kb:kb-main/collection/public]\n",
          "    resources: [kb:kb-main/collection/public]\n    expires_at: 2027-01-01T00:00:00\n")  # fmt: skip
    assert [(f.path, f.code) for f in load(root).errors] == [("grants[0].expires_at", "schema")]


def test_approval_without_grants_file(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    _edit(root / "approval/t-alpha.yaml", "tenant: t-alpha", "tenant: t-beta")
    assert [f.code for f in lint(root, manifests(GOOD), NOW).errors] == ["no-grants-file"]


def test_disjoint_rules_need_no_common_approver(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    text = (root / "approval/t-alpha.yaml").read_text()
    text += (
        "  - id: wire\n    match:\n      tools: [demo.wire.propose]\n    decision: require_approval\n"
        "    approvers: ['oidc:https://idp.example.invalid:2']\n"
    )
    (root / "approval/t-alpha.yaml").write_text(text)
    second = "    - id: oidc:https://idp.example.invalid:2\n      roles: [approver]\n"
    _edit(root / "tenants/t-alpha.yaml", "      roles: [approver]\n", "      roles: [approver]\n" + second)
    assert lint(root, manifests(GOOD), NOW).findings == []
    # Same rule matched by effect class instead: demo.message.propose is external_effect -> overlap.
    _edit(root / "approval/t-alpha.yaml", "tools: [demo.wire.propose]", "effect_classes: [external_effect]")
    assert [f.code for f in lint(root, manifests(GOOD), NOW).errors] == ["approvers-empty-intersection"]


def test_foreign_principal_in_group_and_unknown_file(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    _edit(root / "tenants/t-alpha.yaml", "researchers: [agent:t-alpha:hermes]",
          "researchers: [agent:t-alpha:hermes, agent:t-beta:x]")  # fmt: skip
    (root / "stray.yaml").write_text("a: 1\n")
    found = {(f.path, f.code) for f in lint(root, manifests(GOOD), NOW).errors}
    assert found == {("groups.researchers[1]", "foreign-principal"), ("", "unknown-file")}


def test_two_platform_files_and_duplicate_tenant(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    shutil.copy(root / "platform/registry.yaml", root / "platform/registry.example.yaml")
    shutil.copy(root / "tenants/t-alpha.yaml", root / "tenants/copy.yaml")
    assert sorted(f.code for f in load(root).errors) == ["duplicate-id", "duplicate-id"]


def test_unknown_tool_in_auto_approve_is_a_warning() -> None:
    pol = lint(GOOD / "policies", [], NOW)
    assert {(f.code, f.level) for f in pol.findings} == {("unknown-tool", "warning")}
