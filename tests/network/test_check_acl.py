"""scripts/network/check_acl.py: the committed policy passes; broken copies fail (offline)."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_acl", ROOT / "scripts/network/check_acl.py")
assert _spec and _spec.loader
acl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acl)

TAGS = {"tag:gateway", "tag:approval-ui", "tag:approval-admin"}


def policy() -> dict[str, Any]:
    return acl.load(ROOT / "policies/network/tailscale-acl.hujson")  # type: ignore[no-any-return]


def test_committed_policy_ok() -> None:
    assert acl.check(policy(), acl.used_tags(ROOT / "deploy/railway/services.yaml")) == []


def test_services_tags_are_the_three_entry_tags() -> None:
    assert acl.used_tags(ROOT / "deploy/railway/services.yaml") == TAGS


def test_hujson_comments_and_trailing_commas() -> None:
    text = '{"a": "x // not a comment", /* c */ "b": [1, 2,], // tail\n}'
    assert json.loads(acl.strip_hujson(text)) == {"a": "x // not a comment", "b": [1, 2]}


def mutate(fn: Any) -> list[str]:
    p = copy.deepcopy(policy())
    fn(p)
    return acl.check(p, TAGS)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("fn", "needle"),
    [
        (lambda p: p["tagOwners"].pop("tag:gateway"), "tag:gateway must be owned"),
        (lambda p: p["tagOwners"].__setitem__("tag:agent", ["alice@example.com"]), "tag:agent must be owned"),
        (lambda p: p["nodeAttrs"].append({"target": ["tag:gateway"], "attr": ["funnel"]}), "funnel"),
        (lambda p: p["acls"].append({"action": "accept", "src": ["*"], "dst": ["*:*"]}), "wildcard"),
        (
            lambda p: p["acls"].append({"action": "accept", "src": ["tag:agent"], "dst": ["tag:approval-ui:443"]}),
            "no accept test",
        ),
        (lambda p: p.__setitem__("tests", p["tests"][:3]), "no deny test"),
        (lambda p: p.__setitem__("autoApprovers", {"exitNode": ["tag:gateway"]}), "autoApprovers"),
    ],
)
def test_broken_policy_fails(fn: Any, needle: str) -> None:
    assert any(needle in e for e in mutate(fn))


def test_wrong_test_expectation_fails() -> None:
    errs = mutate(lambda p: p["tests"].append({"src": "tag:agent", "accept": ["tag:gateway:8443"]}))
    assert any("no rule allows it" in e for e in errs)
