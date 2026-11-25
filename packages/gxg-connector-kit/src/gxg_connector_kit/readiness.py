"""Readiness result for `/readyz` (GR-12, GR-13)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...] = ()


def all_ready(*items: Readiness) -> Readiness:
    """Ready only if every component is ready; reasons are concatenated."""
    reasons = tuple(r for item in items for r in item.reasons)
    return Readiness(all(item.ready for item in items), reasons)
