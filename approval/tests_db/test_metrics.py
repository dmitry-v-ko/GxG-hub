"""AC-M10-05: every metric of approval-service-v1 «Наблюдаемость» on /metrics (api: database figures, quota counter,
bundle age; executor: reserve conflicts; connector: token verify failures — see their own tests)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import psycopg
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from gxg_approval import intents, metrics

HUMAN = "oidc:https://idp.example.invalid:1"
SPEC = {  # approval-service-v1 «Наблюдаемость»; token_verify_failures: connector side, reserve_conflicts: executor
    "gxg_intents_created_total", "gxg_intents_pending", "gxg_approval_latency_seconds", "gxg_executions_total",
    "gxg_intents_outcome_unknown", "gxg_quota_rejections_total", "gxg_switch_blocked", "gxg_bundle_age_seconds",
    "gxg_execute_deferred_total",
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
    assert row is not None
    decide_db(approved, HUMAN, "approve", 1, row[0])
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        e = (ex.execute("SELECT gxg_reserve(%s, 'x')", (approved,)).fetchone() or (None,))[0]
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (e, approved))
        ex.execute("SELECT gxg_complete(%s, 'x', 'outcome_unknown', null, null, null)", (e,))
        deferred = propose(args={"text": "c", "n": 3}).json()["intent_id"]
        _deferred(owner, decide_db, ex, deferred)
    with pg.connect("gxg_admin") as admin:
        admin.execute("SELECT gxg_switch('tenant:t-beta', true, 'drill', 'oidc:op')")
    intents.QUOTA_REJECTIONS.labels("t-alpha", "agent:t-alpha:bot").inc(0)  # the series exists
    m = scrape(client)
    names = {n.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count") for n in m}
    assert SPEC <= names, SPEC - names
    t = {"tenant": "t-alpha"}
    created = m["gxg_intents_created_total"]
    assert ({"tenant": "t-alpha", "tool": "demo.message.propose", "mode": "human"}, 3.0) in created
    assert (t, 1.0) in m["gxg_intents_pending"] and (t, 1.0) in m["gxg_intents_outcome_unknown"]
    assert ({**t, "tool": "demo.message.propose", "outcome": "outcome_unknown"}, 1.0) in m["gxg_executions_total"]
    assert ({"scope": "tenant:t-beta"}, 1.0) in m["gxg_switch_blocked"]
    assert ({"action": "admin_switch_set"}, 1.0) in m["gxg_admin_actions_total"]
    assert m["gxg_execute_deferred_total"] == [({**t, "tool": "demo.message.propose"}, 1.0)]
    buckets = {s["le"]: v for s, v in m["gxg_approval_latency_seconds_bucket"]}
    assert (
        buckets["60.0"] == 1 and buckets["900.0"] == 2 and buckets["+Inf"] == 2
    )  # ~400 s and the deferred one at once
    ((latency_labels, latency_sum),) = m["gxg_approval_latency_seconds_sum"]
    assert latency_labels == {**t, "tool": "demo.message.propose"} and 399 < latency_sum < 500
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
    assert (age.name, age.documentation) == ("gxg_bundle_age_seconds", "age of the loaded grants bundle (alert > 13 h)")
    assert (limit.name, limit.documentation) == ("gxg_intents_pending_limit", "pending_per_tenant_max (AP-06)")
    assert [(s.labels, s.value) for s in limit.samples] == [({"tenant": "t-alpha"}, 100.0), ({"tenant": "t-beta"}, 7.0)]


def test_executions_total_never_goes_down(
    client: TestClient, propose: Callable[..., Any], owner: psycopg.Connection[Any], pg: Any
) -> None:
    """Critic M10-1 #1: a counter by the outcome reached — reconcile keeps outcome_unknown and adds reconciled_*."""
    intent = propose(tool="demo.note.propose").json()["intent_id"]
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        e = (ex.execute("SELECT gxg_reserve(%s, 'x')", (intent,)).fetchone() or (None,))[0]
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (e, intent))
        ex.execute("SELECT gxg_complete(%s, 'x', 'outcome_unknown', null, null, null)", (e,))
        before = {s["outcome"]: v for s, v in scrape(client)["gxg_executions_total"]}
        ex.execute("SELECT gxg_reconcile(%s, 'x', 'executed', null)", (intent,))
    after = {s["outcome"]: v for s, v in scrape(client)["gxg_executions_total"]}
    assert before == {"outcome_unknown": 1.0}
    assert after == {"outcome_unknown": 1.0, "reconciled_executed": 1.0}


def _deferred(owner: psycopg.Connection[Any], decide_db: Any, ex: psycopg.Connection[Any], intent: str) -> None:
    """Approve, reserve and defer one attempt by a 429 rate_limited (T12b)."""
    row = owner.execute("SELECT canonical_args_hash FROM intents WHERE intent_id = %s", (intent,)).fetchone()
    assert row is not None
    decide_db(intent, HUMAN, "approve", 1, row[0])
    e = (ex.execute("SELECT gxg_reserve(%s, 'x')", (intent,)).fetchone() or (None,))[0]
    ex.execute("SELECT gxg_defer(%s, 'x', 30)", (e,))


def test_scrape_after_a_deferral_and_an_expiry_by_rate_limit(
    client: TestClient, propose: Callable[..., Any], owner: psycopg.Connection[Any], decide_db: Any, pg: Any
) -> None:
    """Critic M10-2 N1/N2: /metrics answers 200 with both AS-12a figures; the expiry figure leaves after an hour."""
    intent = propose().json()["intent_id"]
    with pg.connect("gxg_executor") as ex:
        _deferred(owner, decide_db, ex, intent)
    owner.execute("UPDATE intents SET execution_deadline = now() WHERE intent_id = %s", (intent,))
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_expire_execution(%s)", (intent,))
    t = {"tenant": "t-alpha"}
    m = scrape(client)
    assert m["gxg_execute_deferred_total"] == [({**t, "tool": "demo.message.propose"}, 1.0)]
    assert m["gxg_intents_expired_after_rate_limit"] == [(t, 1.0)]
    assert "gxg_approval_latency_seconds_bucket" in m  # the approval itself is still in the histogram
    owner.execute("UPDATE intents SET terminal_at = now() - interval '61 minutes' WHERE intent_id = %s", (intent,))
    assert "gxg_intents_expired_after_rate_limit" not in scrape(client)  # the alert clears


def test_unknown_database_figure_is_skipped(world: Any) -> None:
    """A figure the collector does not know (a newer migration) is skipped, not a 500 on every scrape."""

    class Pool:
        timeouts: ClassVar[list[float]] = []

        def connection(self, timeout: float) -> Any:
            import contextlib

            self.timeouts.append(timeout)

            class Conn:
                def execute(self, _sql: str) -> Any:
                    class R:
                        def fetchall(self) -> list[tuple[str, dict[str, str], float]]:
                            return [
                                ("gxg_new_figure", {"tenant": "t"}, 1.0),
                                ("gxg_intents_pending", {"tenant": "t"}, 2.0),
                            ]

                    return R()

            return contextlib.nullcontext(Conn())

    families = {f.name: f for f in metrics.DatabaseCollector(Pool()).collect()}  # type: ignore[arg-type]
    assert [s.value for s in families["gxg_intents_pending"].samples] == [2.0]
    assert families["gxg_approval_latency_seconds"].samples == []
    assert Pool.timeouts == [5]  # a scrape never waits long for a pool connection


def test_help_texts(client: TestClient) -> None:
    """Every family of /metrics carries its HELP text (the operator reads it in Prometheus)."""
    docs = {f.name: f.documentation for f in text_string_to_metric_families(client.get("/metrics").text)}
    expected = {n.removesuffix("_total"): h for n, h in metrics.HELP.items()}
    expected[metrics.LATENCY] = "time from T1 to approved (human)"
    assert {n: docs.get(n) for n in expected} == expected
