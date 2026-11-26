"""AC-M2-01 (validate on the repo examples) and AC-M2-02 (lint finds every prepared error)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gxg_policy import lint as lint_module
from gxg_policy.lint import _Linter, agent_tenant, default_manifests, expand, lint, rid_overlap, seconds
from gxg_policy.load import REPO_ROOT, load, parse_yaml

HERE = Path(__file__).parent
REPO = REPO_ROOT
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


# DoD-4 (mutmut): full messages, paths and levels of every fixture, and the pure helpers as tables.
GOLDEN = HERE / "lint-golden.json"
GOLDEN_CASES = [*BAD, GOOD, HERE / "lint-all"]


def _findings(case: Path) -> list[str]:
    return [f"{f.level}: {f}" for f in lint(case / "policies", manifests(case), NOW).findings]


def test_lint_findings_match_golden() -> None:
    got = {c.name: _findings(c) for c in GOLDEN_CASES}
    if os.environ.get("GXG_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_load_reports_every_broken_file_and_goes_on(tmp_path: Path) -> None:
    absent = [(f.file, f.path, f.code, f.message) for f in load(tmp_path / "absent").findings]
    assert absent == [(str(tmp_path / "absent"), "", "io", "not a directory")]
    root = _copy_good(tmp_path)
    (root / "stray.yaml").write_text("a: 1\n")
    (root / "other/x.yaml").parent.mkdir()
    (root / "other/x.yaml").write_text("a: 1\n")
    (root / "approval/a-broken.yaml").write_text("rules: [\n  - x\n")
    (root / "approval/b-utf16.yaml").write_bytes("tenant: t".encode("utf-16"))
    (root / "tenants/b-naive.yaml").write_text(
        (root / "tenants/t-alpha.yaml").read_text().replace("tenant: t-alpha", "tenant: t-naive")
        + "  - { id: late, subjects: [agent:t-naive:x], actions: [read], resources: [kb:kb-main/collection/p],"
        " expires_at: 2027-01-01T00:00:00 }\n"
    )
    shutil.copy(root / "platform/registry.yaml", root / "platform/second.yaml")
    shutil.copy(root / "tenants/t-alpha.yaml", root / "tenants/z-copy.yaml")
    pol = load(root)
    got = [str(f) for f in pol.findings]
    assert got[0].startswith("approval/a-broken.yaml: <root>: [yaml] ") and "\n" not in got[0]
    assert got[1].startswith("approval/b-utf16.yaml: <root>: [yaml] ")
    assert got[2:] == [
        "other/x.yaml: <root>: [unknown-file] expected tenants/, approval/ or platform/",
        "platform/second.yaml: <root>: [duplicate-id] second platform registry (platform/registry.yaml)",
        "stray.yaml: <root>: [unknown-file] expected tenants/, approval/ or platform/",
        "tenants/b-naive.yaml: grants[1].expires_at: [schema] '2027-01-01T00:00:00' is not a 'date-time'",
        "tenants/z-copy.yaml: tenant: [duplicate-id] tenant 't-alpha' also in tenants/t-alpha.yaml",
    ]
    # Files after the broken ones were still read.
    assert sorted(pol.tenants) == ["t-alpha"] and sorted(pol.approval) == ["t-alpha"]
    assert pol.platform is not None and pol.platform[0] == "platform/registry.yaml"
    assert [f.path for f in pol.findings if f.code == "duplicate-id"] == ["", "tenant"]


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ({}, {}, True),
        ({"tools": ["demo.message.propose"]}, {"tools": ["demo.message.propose", "x.y.z"]}, True),
        ({"tools": ["demo.message.propose"]}, {"tools": ["demo.wire.propose"]}, False),
        ({"effect_classes": ["high_risk"]}, {"effect_classes": ["high_risk"]}, True),
        ({"effect_classes": ["high_risk"]}, {"effect_classes": ["external_effect"]}, False),
        ({"tools": ["demo.wire.propose"]}, {"effect_classes": ["external_effect"]}, False),  # known class, disjoint
        ({"effect_classes": ["external_effect"]}, {"tools": ["demo.wire.propose"]}, False),  # both orders
        ({"tools": ["demo.wire.propose"]}, {"effect_classes": ["high_risk"]}, True),
        ({"tools": ["demo.wire.propose", "x.y.z"]}, {"effect_classes": ["external_effect"]}, True),  # unknown tool
        ({"resources": ["kb:c/t/a"]}, {"resources": ["kb:c/t/b"]}, False),
        ({"resources": ["kb:c/t/a", "kb:c/t/b"]}, {"resources": ["kb:c/t/b"]}, True),
        ({"resources": ["kb:c/t/*"]}, {"resources": ["kb:c/t/b"]}, True),
        ({"requesters": ["agent:t:a"]}, {"requesters": ["agent:t:b"]}, False),
        ({"requesters": ["group:g"]}, {"requesters": ["agent:t:b"]}, True),
        ({"requesters": ["agent:t:a"], "tools": ["demo.message.propose"]}, {"tools": ["demo.message.propose"]}, True),
        ({"resources": ["kb:c/t/a"], "requesters": ["agent:t:a"]}, {"requesters": ["agent:t:a"]}, True),
    ],
)
def test_may_overlap(a: dict[str, Any], b: dict[str, Any], overlap: bool) -> None:
    pol = load(GOOD / "policies")
    pol.manifests["demo"] = parse_yaml((GOOD / "manifest.yaml").read_text())
    linter = _Linter(pol, NOW)
    groups = {"g": ["agent:t:b"]}
    assert linter.may_overlap(a, b, groups) is overlap
    assert linter.may_overlap(b, a, groups) is overlap


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("kb:c/t/a", "kb:c/t/a", True),
        ("kb:c/t/a", "kb:c/t/ab", False),
        ("kb:c/t/*", "kb:c/t/a", True),
        ("kb:c/t/*", "kb:c/u/a", False),
        ("kb:c/t/*", "kb:c/t/*", True),
        ("kb:c/t*", "kb:c/tx", False),  # only a `/*` suffix is a mask
        ("kb:c/t/*", "kb:c/tx/a", False),  # the mask keeps its `/`
    ],
)
def test_rid_overlap(a: str, b: str, overlap: bool) -> None:
    assert rid_overlap(a, b) is overlap and rid_overlap(b, a) is overlap


def test_small_helpers() -> None:
    assert [seconds(d) for d in ("5m", "2h", "3d", "15m")] == [300, 7200, 259200, 900]
    with pytest.raises(KeyError):
        seconds("5s")
    assert expand(["group:g", "agent:x", "group:none", "agent:y", "agent:x"], {"g": ["agent:y", "agent:z"]}) == [
        "agent:y",
        "agent:z",
        "agent:x",
    ]
    assert [agent_tenant(p) for p in ("agent:t:a", "agent:t:a:b", "agent:t", "oidc:t:a")] == ["t", "t", None, None]
    assert default_manifests() == sorted(REPO.glob("connectors/*/gxg-manifest.yaml"))


def test_default_manifests_are_the_connector_manifests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("b", "a"):
        (tmp_path / "connectors" / name).mkdir(parents=True)
        (tmp_path / "connectors" / name / "gxg-manifest.yaml").write_text("x")
    (tmp_path / "connectors/a/other.yaml").write_text("x")
    monkeypatch.setattr(lint_module, "REPO_ROOT", tmp_path)
    assert default_manifests() == [
        tmp_path / "connectors/a/gxg-manifest.yaml",
        tmp_path / "connectors/b/gxg-manifest.yaml",
    ]


def test_every_bad_manifest_is_reported(tmp_path: Path) -> None:
    a, b, c = tmp_path / "a.yaml", tmp_path / "b.yaml", tmp_path / "c.yaml"
    a.write_text("connector: [\n  x\n")
    b.write_text("connector: demo\nversion: 1\n")
    c.write_text((GOOD / "manifest.yaml").read_text())
    pol = lint(GOOD / "policies", [a, b, c, c], NOW)
    got = [str(f) for f in pol.errors]
    with pytest.raises(yaml.YAMLError) as exc:
        parse_yaml(a.read_text())
    assert got[0] == f"{a}: <root>: [manifest] " + str(exc.value).replace("\n", " ")
    assert all(x.startswith(f"{b}: ") and "[schema]" in x for x in got[1:-1])
    assert got[-1] == f"{c}: connector: [duplicate-id] second manifest for 'demo'"
    assert list(pol.manifests) == ["demo"]


def test_registry_without_webhooks(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    reg = root / "platform/registry.yaml"
    reg.write_text(reg.read_text().split("webhooks:")[0])
    assert "webhooks" not in parse_yaml(reg.read_text())
    assert lint(root, manifests(GOOD), NOW).findings == []


def test_nested_files_belong_to_their_top_directory(tmp_path: Path) -> None:
    root = _copy_good(tmp_path)
    (root / "tenants/sub").mkdir()
    shutil.move(root / "tenants/t-alpha.yaml", root / "tenants/sub/t-alpha.yaml")
    pol = load(root)
    assert pol.findings == [] and pol.tenants["t-alpha"][0] == "tenants/sub/t-alpha.yaml"


def test_same_tool_name_in_two_manifests_is_an_error(tmp_path: Path) -> None:
    # Cycle 3 finding 3: AP-02 and AS-26 look a tool up by name; two classes for one name must not pass.
    other = tmp_path / "other.yaml"
    other.write_text((GOOD / "manifest.yaml").read_text().replace("connector: demo", "connector: other"))
    pol = lint(GOOD / "policies", [GOOD / "manifest.yaml", other], NOW)
    assert [(f.file, f.path, f.code) for f in pol.errors] == [
        (str(other), "tools[0].name", "duplicate-tool"),
        (str(other), "tools[1].name", "duplicate-tool"),
    ]
    assert pol.errors[0].message == "tool 'demo.wire.propose' is declared twice"
    assert list(pol.manifests) == ["demo"]
    r = cli("lint", str(GOOD / "policies"), "--manifest", str(GOOD / "manifest.yaml"), "--manifest", str(other))
    assert r.returncode == 1 and "[duplicate-tool]" in r.stderr


def test_same_tool_name_twice_in_one_manifest_is_an_error(tmp_path: Path) -> None:
    # Cycle 4 finding 1: names inside one manifest are unique too.
    doc = parse_yaml((GOOD / "manifest.yaml").read_text())
    doc["tools"].append({**doc["tools"][0], "version": "2.0.0", "effect_class": "external_effect"})
    m = tmp_path / "m.yaml"
    m.write_text(yaml.safe_dump(doc, allow_unicode=True))
    pol = lint(GOOD / "policies", [m], NOW)
    n = len(doc["tools"]) - 1
    assert [str(f) for f in pol.errors] == [
        f"{m}: tools[{n}].name: [duplicate-tool] tool {doc['tools'][0]['name']!r} is declared twice"
    ]
    assert pol.manifests == {}
    r = cli("lint", str(GOOD / "policies"), "--manifest", str(m))
    assert r.returncode == 1 and "[duplicate-tool]" in r.stderr


def test_a_nested_policy_root_is_left_to_its_own_run(tmp_path: Path) -> None:
    """policies/pilot is a root of its own (build: `gxg-policy build policies/pilot`): not an unknown file of policies/;
    a directory that is no root (a typo) still is."""
    root = _copy_good(tmp_path)
    (root / "pilot" / "tenants").mkdir(parents=True)
    (root / "pilot" / "tenants" / "t-x.yaml").write_text("not: a tenant\n")  # not read here
    (root / "tenats").mkdir()
    (root / "tenats" / "t-y.yaml").write_text("x: 1\n")
    assert [str(f) for f in load(root).findings] == [
        "tenats/t-y.yaml: <root>: [unknown-file] expected tenants/, approval/ or platform/"
    ]


TERM = "expires_at: '2027-03-01T00:00:00Z'"


def _tg_tenant(tmp_path: Path, grants: str) -> Path:
    root = tmp_path / "policies"
    (root / "tenants").mkdir(parents=True)
    (root / "tenants/t-x.yaml").write_text(
        "tenant: t-x\nschema_version: 1\n"
        "connections:\n  - {id: tg, domain: telegram, credential_ref: secret://t-x/telegram/a}\n"
        "principals:\n  agents:\n    - id: agent:t-x:a\n"
        f"grants:\n{grants}",
        encoding="utf-8",
    )
    return root


def test_ext_mask_and_pii_without_expiry_are_warnings_not_errors(tmp_path: Path) -> None:
    root = _tg_tenant(
        tmp_path,
        f"  - {{id: g1, subjects: [agent:t-x:a], actions: [ext:read], resources: ['telegram:tg/chat/*'], {TERM}}}\n"
        "  - {id: g2, subjects: [agent:t-x:a], actions: ['ext:read:pii'], resources: ['telegram:tg/chat/c1']}\n",
    )
    r = lint(root, [], NOW)
    assert r.errors == []
    assert [(f.path, f.code, f.level) for f in r.findings] == [
        ("grants[0].resources[0]", "ext-mask-wide", "warning"),
        ("grants[1]", "ext-pii-no-expiry", "warning"),
    ]


def test_ext_read_on_a_single_chat_with_expiry_is_clean(tmp_path: Path) -> None:
    root = _tg_tenant(
        tmp_path,
        f"  - {{id: g1, subjects: [agent:t-x:a], actions: ['ext:read', 'ext:read:pii'], "
        f"resources: ['telegram:tg/chat/c1'], {TERM}}}\n",
    )
    assert lint(root, [], NOW).findings == []
