"""MC-06: the registry loader rejects invalid entries (fail closed)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from kit_testing import load_yaml

from gxg_connector_kit.capabilities import RegistryError, load_registry

EXAMPLE = "schemas/examples/telegram.gxg-capabilities.yaml"


def _doc() -> dict[str, Any]:
    return load_yaml(EXAMPLE)  # type: ignore[no-any-return]


def test_example_loads() -> None:
    reg = load_registry(_doc())
    assert reg.connector == "telegram"
    assert reg.get("chats.history") is not None
    assert reg.get("chats.history").enabled_default is False  # type: ignore[union-attr]
    assert reg.get("nope") is None


@pytest.mark.parametrize("missing", ["effect_class", "input_schema", "action", "resource", "version", "description"])
def test_entry_without_required_field_is_rejected(missing: str) -> None:
    doc = _doc()
    del doc["capabilities"][1][missing]
    with pytest.raises(RegistryError):
        load_registry(doc)


def test_resource_without_type_is_rejected() -> None:
    doc = _doc()
    doc["capabilities"][1]["resource"] = {"scope": "single"}
    with pytest.raises(RegistryError):
        load_registry(doc)


def test_duplicate_name_is_rejected() -> None:
    doc = _doc()
    dup = copy.deepcopy(doc["capabilities"][1])
    dup["description"] = "other"  # not an identical item: uniqueItems would not catch it
    doc["capabilities"].append(dup)
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(doc)


@pytest.mark.parametrize("name", ["invoke", "x.invoke", "auth.2fa.set", "sessions.terminate", "account.delete_account"])
def test_forbidden_name_is_rejected(name: str) -> None:
    doc = _doc()
    doc["capabilities"][1]["name"] = name
    with pytest.raises(RegistryError):
        load_registry(doc)


def test_non_read_enabled_by_default_is_rejected() -> None:
    doc = _doc()
    doc["capabilities"][2]["enabled_default"] = True
    with pytest.raises(RegistryError):
        load_registry(doc)


def test_not_a_mapping_is_rejected() -> None:
    with pytest.raises(RegistryError):
        load_registry([])
