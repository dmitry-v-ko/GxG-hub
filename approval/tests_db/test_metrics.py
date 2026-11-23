"""AC-M10-05: every metric of approval-service-v1 «Наблюдаемость» on /metrics (api: database figures, quota counter,
bundle age; executor: reserve conflicts; connector: token verify failures — see their own tests)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from gxg_approval import intents, metrics

HUMAN = "oidc:https://idp.example.invalid:1"
SPEC = {  # approval-service-v1 «Наблюдаемость»; token_verify_failures: connector side, reserve_conflicts: executor
    "gxg_intents_created_total", "gxg_intents_pending", "gxg_approval_latency_seconds", "gxg_executions_total",
    "gxg_intents_outcome_unknown", "gxg_quota_rejections_total", "gxg_switch_blocked", "gxg_bundle_age_seconds",
}  # fmt: skip


def scrape(client: TestClient) -> dict[str, list[tuple[dict[str, str], float]]]:
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(r.text):
        for s in family.samples:
            out.setdefault(s.name, []).append((dict(s.labels), s.value))
    return out


def test_every_metric_is_exposed(
    client: TestClient, propose: Callable[..., Any], owner: psycopg.Connection[Any], decide_db: Any, pg: Any
) -> None:
    pending = propose().json()["intent_id"]
    approved = propose(args={"text": "b", "n": 2}).json()["intent_id"]
    row = owner.execute("SELECT canonical_args_hash FROM intents WHERE intent_id = %s", (approved,)).fetchone()
    owner.execute(
        "UPDATE intents SET created_at = created_at - interval '400 seconds' WHERE intent_id = %s", (approved,)
    )
    decide_db(approved, HUMAN, "approve", 1, row[0])
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        e = ex.execute("SELECT gxg_reserve(%s, 'x')", (approved,)).fetchone()[0]
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (e, approved))
        ex.execute("SELECT gxg_complete(%s, 'x', 'outcome_unknown', null, null, null)", (e,))
    with pg.connect("gxg_admin") as admin:
        admin.execute("SELECT gxg_switch('tenant:t-beta', true, 'drill', 'oidc:op')")
    intents.QUOTA_REJECTIONS.labels("t-alpha", "agent:t-alpha:bot").inc(0)  # the series exists
    m = scrape(client)
    names = {n.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count") for n in m}
    assert SPEC <= names, SPEC - names
    t = {"tenant": "t-alpha"}
    created = m["gxg_intents_created_total"]
    assert ({"tenant": "t-alpha", "tool": "demo.message.propose", "mode": "human"}, 2.0) in created
    assert (t, 1.0) in m["gxg_intents_pending"] and (t, 1.0) in m["gxg_intents_outcome_unknown"]
    assert ({**t, "tool": "demo.message.propose", "outcome": "outcome_unknown"}, 1.0) in m["gxg_executions_total"]
    assert ({"scope": "tenant:t-beta"}, 1.0) in m["gxg_switch_blocked"]
    assert ({"action": "admin_switch_set"}, 1.0) in m["gxg_admin_actions_total"]
    buckets = {s["le"]: v for s, v in m["gxg_approval_latency_seconds_bucket"]}
    assert buckets["60.0"] == 0 and buckets["900.0"] == 1 and buckets["+Inf"] == 1  # approved after ~400 s
    assert 399 < m["gxg_approval_latency_seconds_sum"][0][1] < 500
    (age,) = m["gxg_bundle_age_seconds"]
    assert 0 <= age[1] < 60
    assert pending  # stays pending_approval


def test_no_people_in_labels(client: TestClient, propose: Callable[..., Any]) -> None:
    """«метки без персональных данных»: no requester or human principal in a database metric label."""
    propose()
    text = client.get("/metrics").text
    db_lines = [line for line in text.splitlines() if not line.startswith(("#", "gxg_quota_rejections"))]
    assert not [line for line in db_lines if "oidc:" in line or "agent:" in line]


def test_bundle_age_without_a_bundle(world: Any) -> None:
    class NoBundle:
        @property
        def current(self) -> Any:
            raise RuntimeError("not ready")

    age, limit = metrics.BundleAge(NoBundle()).collect()  # type: ignore[arg-type]
    assert age.samples == [] and limit.samples == []  # absent: the absent() alert of the rules fires
    world.set_policy("t-beta", [], quotas={"pending_per_tenant_max": 7})
    age, limit = metrics.BundleAge(world.store, clock=lambda: world.store.current.built_at.timestamp() + 7).collect()
    assert [s.value for s in age.samples] == [7.0]
    assert [(s.labels, s.value) for s in limit.samples] == [({"tenant": "t-alpha"}, 100.0), ({"tenant": "t-beta"}, 7.0)]
