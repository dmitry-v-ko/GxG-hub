"""Fixtures for gxg-connector-kit tests (helpers live in kit_testing.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from kit_testing import BundleKit, Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bundle_kit(tmp_path: Path) -> BundleKit:
    return BundleKit(tmp_path / "bundle-state.json")


@pytest.fixture
def test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GXG_PROFILE", "test")
