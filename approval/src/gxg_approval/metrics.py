"""Prometheus metrics of approval-service-v1 «Наблюдаемость» on `api` `/metrics`.

The intent, execution, switch, admin-action and webhook figures come from the database at scrape time
(`gxg_metrics()`: aggregates only, labels tenant/tool/mode/outcome/scope/action/subscriber — no people), so they
are the same whichever process changed them. Process counters: `gxg_quota_rejections_total` (api). Executor's
`gxg_reserve_conflicts_total` is on the executor's own port; `gxg_token_verify_failures_total` on the connectors.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily, Metric
from prometheus_client.registry import Collector
from psycopg_pool import ConnectionPool

from gxg_connector_kit.grants import BundleStore

from . import quotas
from .intents import QUOTA_REJECTIONS

HELP = {
    "gxg_intents_created_total": "intents created (T1)",
    "gxg_intents_pending": "intents in pending_approval",
    "gxg_intents_outcome_unknown": "intents in outcome_unknown",
    "gxg_executions_total": "finished execution attempts by outcome",
    "gxg_switch_blocked": "kill switch state (1 blocked)",
    "gxg_admin_actions_total": "admin actions (audit_events admin_*)",
    "gxg_webhook_dead": "webhook deliveries given up after 24 h (kept 7 days)",
    "gxg_webhook_pending": "webhook deliveries not yet delivered",
    "gxg_execute_deferred_total": "execute attempts deferred by a 429 rate_limited (T12b, AS-12a)",
    "gxg_intents_expired_after_rate_limit": "intents expired in the last hour with a 429-deferred last attempt",
}
LABELS = {
    "gxg_intents_created_total": ("tenant", "tool", "mode"),
    "gxg_intents_pending": ("tenant",),
    "gxg_intents_outcome_unknown": ("tenant",),
    "gxg_executions_total": ("tenant", "tool", "outcome"),
    "gxg_switch_blocked": ("scope",),
    "gxg_admin_actions_total": ("action",),
    "gxg_webhook_dead": ("subscriber",),
    "gxg_webhook_pending": ("subscriber",),
    "gxg_execute_deferred_total": ("tenant", "tool"),
    "gxg_intents_expired_after_rate_limit": ("tenant",),
}
LATENCY = "gxg_approval_latency_seconds"


class DatabaseCollector(Collector):
    def __init__(self, pool: ConnectionPool[Any]) -> None:
        self.pool = pool

    def collect(self) -> Iterator[Metric]:
        with self.pool.connection(timeout=5) as conn:
            rows = conn.execute("SELECT name, labels, value FROM gxg_metrics()").fetchall()
        families: dict[str, Any] = {}
        for name, labels in LABELS.items():
            kind = CounterMetricFamily if name.endswith("_total") else GaugeMetricFamily
            families[name] = kind(name.removesuffix("_total"), HELP[name], labels=list(labels))
        latency: dict[tuple[str, str], dict[str, Any]] = {}
        for name, labels, value in rows:
            if name in families:
                families[name].add_metric([str(labels[k]) for k in LABELS[name]], value)
                continue
            if not name.startswith(f"{LATENCY}_"):  # a figure this version does not know: skipped, never a 500
                continue
            key = (str(labels["tenant"]), str(labels["tool"]))
            h = latency.setdefault(key, {"buckets": [], "sum": 0.0, "count": 0.0})
            if name == f"{LATENCY}_bucket":
                h["buckets"].append((float(labels["le"]), value))
            else:
                h[name.removeprefix(f"{LATENCY}_")] = value
        hist = HistogramMetricFamily(LATENCY, "time from T1 to approved (human)", labels=["tenant", "tool"])
        for key, h in sorted(latency.items()):
            buckets = [(str(le), v) for le, v in sorted(h["buckets"])] + [("+Inf", h["count"])]
            hist.add_metric(list(key), buckets, h["sum"])
        yield from families.values()
        yield hist


class BundleAge(Collector):
    """`gxg_bundle_age_seconds` and the AP-06 limit `pending_per_tenant_max` per tenant (the 80 % alert)."""

    def __init__(self, store: BundleStore, clock: Callable[[], float] = time.time) -> None:
        self.store, self.clock = store, clock

    def collect(self) -> Iterator[Metric]:
        age = GaugeMetricFamily("gxg_bundle_age_seconds", "age of the loaded grants bundle (alert > 13 h)")
        limit = GaugeMetricFamily("gxg_intents_pending_limit", "pending_per_tenant_max (AP-06)", labels=["tenant"])
        try:
            bundle = self.store.current
        except Exception:  # no valid bundle: no sample, the absent() alert fires
            bundle = None
        if bundle is not None:
            age.add_metric([], self.clock() - bundle.built_at.timestamp())
            for tenant, policy in sorted(bundle.approval_policies.items()):
                limit.add_metric([tenant], quotas.limits((policy or {}).get("quotas"))["pending_per_tenant_max"])
        yield age
        yield limit


def registry(pool: ConnectionPool[Any], store: BundleStore) -> CollectorRegistry:
    r = CollectorRegistry(auto_describe=False)
    r.register(DatabaseCollector(pool))
    r.register(BundleAge(store))
    r.register(QUOTA_REJECTIONS)
    return r


def exposition(r: CollectorRegistry) -> bytes:
    return generate_latest(r)
