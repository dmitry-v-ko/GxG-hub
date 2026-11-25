"""AP-05, AP-06: which quota is hit and the retry hint."""

from __future__ import annotations

from gxg_approval.quotas import Counts, exceeded, limits


def test_defaults() -> None:
    assert limits(None) == {
        "propose_per_requester_per_hour": 30,
        "pending_per_requester_max": 20,
        "pending_per_tenant_max": 100,
    }
    assert limits({"pending_per_tenant_max": 5})["pending_per_tenant_max"] == 5


def test_boundaries() -> None:
    assert exceeded(None, Counts(29, 100.0, 19, 99)) is None
    assert exceeded(None, Counts(30, 100.0, 0, 0)) == ("propose_per_requester_per_hour", 3500)
    assert exceeded(None, Counts(0, 0.0, 20, 0)) == ("pending_per_requester_max", 60)
    assert exceeded(None, Counts(0, 0.0, 0, 100)) == ("pending_per_tenant_max", 60)
    assert exceeded({"pending_per_tenant_max": 1}, Counts(0, 0.0, 0, 1)) == ("pending_per_tenant_max", 60)


def test_retry_after_is_at_least_one_second() -> None:
    assert exceeded(None, Counts(30, 3599.5, 0, 0)) == ("propose_per_requester_per_hour", 1)
    assert exceeded(None, Counts(30, 3700.0, 0, 0)) == ("propose_per_requester_per_hour", 1)
    assert exceeded(None, Counts(30, 3598.2, 0, 0)) == ("propose_per_requester_per_hour", 2)


def test_order_hourly_first() -> None:
    assert exceeded(None, Counts(30, 0.0, 20, 100)) == ("propose_per_requester_per_hour", 3600)
    assert exceeded(None, Counts(0, 0.0, 20, 100)) == ("pending_per_requester_max", 60)
