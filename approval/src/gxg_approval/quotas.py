"""`propose` quotas, AP-05…AP-07 (docs/specs/approval-policy-v1.md). Counts come from the T1 transaction."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PENDING_RETRY_S = 60  # shortcut: a pending cap has no natural deadline; refine when M9 shows decision latency


def limits(quotas: Mapping[str, Any] | None) -> dict[str, int]:
    """AP-06 defaults, overridden by the policy's `quotas`."""
    return {
        "propose_per_requester_per_hour": 30,
        "pending_per_requester_max": 20,
        "pending_per_tenant_max": 100,
        **(quotas or {}),
    }


@dataclass(frozen=True)
class Counts:
    proposed_last_hour: int  # intents of the requester created within the last hour
    oldest_age_s: float  # age of the oldest of them (0 if none)
    pending_requester: int
    pending_tenant: int


def exceeded(quotas: Mapping[str, Any] | None, c: Counts) -> tuple[str, int] | None:
    """(quota name, retry_after_s ≥ 1) of the first exceeded quota, or None."""
    q = limits(quotas)
    if c.proposed_last_hour >= q["propose_per_requester_per_hour"]:
        return "propose_per_requester_per_hour", max(1, math.ceil(3600 - c.oldest_age_s))
    if c.pending_requester >= q["pending_per_requester_max"]:
        return "pending_per_requester_max", PENDING_RETRY_S
    if c.pending_tenant >= q["pending_per_tenant_max"]:
        return "pending_per_tenant_max", PENDING_RETRY_S
    return None
