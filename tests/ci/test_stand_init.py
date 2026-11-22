"""stand/init.py: the stand bundle merges the grants fixtures of several connectors per tenant (M6)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def load_init() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stand_init", REPO / "stand" / "init.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["stand_init"] = module
    spec.loader.exec_module(module)
    return module


def tenant(path: Path, **doc: object) -> Path:
    base = {"tenant": "t-alpha", "schema_version": 1, "connections": [], "principals": {"agents": []}, "grants": []}
    path.write_text(yaml.safe_dump(base | doc))
    return path


def test_merged_grants_keeps_everything_and_dedupes_principals(tmp_path: Path) -> None:
    init = load_init()
    a = tenant(
        tmp_path / "a.yaml",
        connections=[{"id": "demo-main", "domain": "demo", "credential_ref": "secret://x"}],
        principals={"agents": [{"id": "agent:t-alpha:reader"}]},
        grants=[{"id": "g1"}],
        groups={"ga": []},
    )
    b = tenant(
        tmp_path / "b.yaml",
        connections=[{"id": "kb-main", "domain": "kb", "credential_ref": "secret://y"}],
        principals={"agents": [{"id": "agent:t-alpha:reader"}, {"id": "agent:t-alpha:nobody"}]},
        grants=[{"id": "g2"}],
        groups={"gb": []},
    )
    merged = init.merged_grants([a, b])
    assert [c["id"] for c in merged["connections"]] == ["demo-main", "kb-main"]
    assert [p["id"] for p in merged["principals"]["agents"]] == ["agent:t-alpha:reader", "agent:t-alpha:nobody"]
    assert [g["id"] for g in merged["grants"]] == ["g1", "g2"] and merged["groups"] == {"ga": [], "gb": []}


@pytest.mark.parametrize("field", ["groups", "connections"])
def test_merged_grants_refuses_a_shadowed_group_or_connection(tmp_path: Path, field: str) -> None:
    init = load_init()
    same: object = {"g": []} if field == "groups" else [{"id": "kb-main", "domain": "kb", "credential_ref": "s://x"}]
    a, b = tenant(tmp_path / "a.yaml", **{field: same}), tenant(tmp_path / "b.yaml", **{field: same})
    with pytest.raises(SystemExit, match="the same group or connection id"):
        init.merged_grants([a, b])
