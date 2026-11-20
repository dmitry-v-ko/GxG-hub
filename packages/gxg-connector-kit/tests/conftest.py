"""Fixtures for gxg-connector-kit tests (helpers live in kit_testing.py)."""

from __future__ import annotations

import pytest
from kit_testing import BundleKit, Clock


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def bundle_kit() -> BundleKit:
    return BundleKit()


@pytest.fixture
def test_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GXG_PROFILE", "test")
