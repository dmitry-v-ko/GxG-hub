"""Readiness combination for /readyz."""

from __future__ import annotations

from gxg_connector_kit.readiness import Readiness, all_ready


def test_all_ready() -> None:
    assert all_ready(Readiness(True), Readiness(True)) == Readiness(True)
    assert all_ready(Readiness(True), Readiness(False, ("no bundle",)), Readiness(False, ("jwks",))) == Readiness(
        False, ("no bundle", "jwks")
    )
    assert all_ready() == Readiness(True)
