"""The kit's runtime schema copies are the repository's schemas/ (no drift)."""

from __future__ import annotations

from importlib.resources import files

import pytest
from kit_testing import REPO

from gxg_connector_kit import _schema


@pytest.mark.parametrize("name", _schema._FILES)
def test_copy_equals_repository_schema(name: str) -> None:
    copy = (files("gxg_connector_kit") / "schemas" / f"{name}.schema.json").read_bytes()
    assert copy == (REPO / "schemas" / f"{name}.schema.json").read_bytes(), f"run: cp schemas/{name}.schema.json …"


def test_no_extra_copies() -> None:
    shipped = {p.name for p in (files("gxg_connector_kit") / "schemas").iterdir()}
    assert shipped == {f"{n}.schema.json" for n in _schema._FILES}
