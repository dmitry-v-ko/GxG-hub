"""Webhook delivery (AS-21…AS-23, EX-20): outbox `webhook` events → every subscriber of the tenant in the bundle.

1. fan_out: each undelivered outbox `webhook` row (one per status change, written by a trigger) becomes one
   `webhook_deliveries` row per subscriber of its tenant in the current bundle; the outbox row is then delivered.
2. deliver: per subscriber, only its oldest pending row is sent (head of line), so a subscriber that was down gets
   every event afterwards in the order they happened (`version` order per intent). 2xx = delivered. Otherwise the
   next try is after 5 s · 2ⁿ, at most 1 h; a row that still fails 24 h after its event is `dead` (alert:
   `gxg_webhook_dead`), and the next row goes.

Signature `GxG-Signature` (kit `webhooks.sign`) with the subscriber's secret: `secret://<path>` is the file
`<secrets_dir>/<path>` (AS-28: only the notifier mounts them). Without a readable secret nothing is sent unsigned:
the try counts as failed. Redirects are not followed (SEC-15); the network allowlist is the compose/k8s policy.
shortcut: one notifier instance (as the Telegram long poll); several would need a lock per subscriber head.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

from gxg_connector_kit import webhooks
from gxg_connector_kit.grants import BundleStore, Webhook

log = logging.getLogger("gxg_approval_notifier.webhooks")
HTTP_TIMEOUT_S = 10
FAN_OUT_BATCH = 200

_SQL_EVENTS = (
    "SELECT id, payload, created_at FROM outbox WHERE topic = 'webhook' AND delivered_at IS NULL "
    "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"
)
_SQL_ADD = (
    "INSERT INTO webhook_deliveries (outbox_id, subscriber_id, tenant_id, event, created_at, next_at) "
    "VALUES (%s, %s, %s, %s, %s, now()) ON CONFLICT (outbox_id, subscriber_id) DO NOTHING"
)
_SQL_EVENT_DONE = "UPDATE outbox SET delivered_at = now(), attempts = attempts + 1 WHERE id = %s"
_SQL_HEADS = (
    "SELECT DISTINCT ON (subscriber_id) id, subscriber_id, event, next_at <= now() FROM webhook_deliveries "
    "WHERE delivered_at IS NULL AND dead_at IS NULL ORDER BY subscriber_id, id"
)
_SQL_DELIVERED = (
    "UPDATE webhook_deliveries SET delivered_at = now(), attempts = attempts + 1, last_status = %s WHERE id = %s"
)
# AS-23: backoff from 5 s doubling up to 1 h; dead once the event is 24 h old (database clock, one for all rows)
_SQL_FAILED = (
    "UPDATE webhook_deliveries SET attempts = attempts + 1, last_status = %s, "
    "next_at = now() + make_interval(secs => least(3600, 5 * power(2, least(attempts, 20)))), "
    "dead_at = CASE WHEN now() - created_at >= interval '24 hours' THEN now() END WHERE id = %s RETURNING dead_at"
)
_SQL_GONE = "UPDATE webhook_deliveries SET dead_at = now(), last_status = 'not in the bundle' WHERE id = %s"


def body_of(event: dict[str, Any]) -> bytes:
    """intent-event.schema.json document, compact and with sorted keys (the bytes that are signed)."""
    return json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


@dataclass
class Webhooks:
    connect: Callable[[], psycopg.Connection[Any]]
    bundles: BundleStore
    secrets_dir: Path
    http: httpx.Client
    clock: Callable[[], float] = time.time

    def _subscribers(self) -> dict[str, Webhook]:
        return {w.id: w for w in self.bundles.current.webhooks}

    def fan_out(self) -> int:
        """Outbox events → deliveries. Returns the number of events handled."""
        subscribers = list(self._subscribers().values())
        with self.connect() as conn, conn.transaction():
            rows = conn.execute(_SQL_EVENTS, (FAN_OUT_BATCH,)).fetchall()
            for outbox_id, event, created_at in rows:
                task = str(event.get("business_task_id") or "")
                for w in subscribers:
                    if w.tenant == event["tenant_id"] and task.startswith(w.business_task_prefix or ""):
                        conn.execute(_SQL_ADD, (outbox_id, w.id, w.tenant, Jsonb(event), created_at))
                conn.execute(_SQL_EVENT_DONE, (outbox_id,))
        return len(rows)

    def deliver(self) -> int:
        """One try for the due head of every subscriber. Returns the number of tries."""
        subscribers = self._subscribers()
        tries = 0
        with self.connect() as conn:
            for row_id, subscriber_id, event, due in conn.execute(_SQL_HEADS).fetchall():
                w = subscribers.get(subscriber_id)
                if w is None:
                    conn.execute(_SQL_GONE, (row_id,))
                    continue
                if not due:
                    continue
                tries += 1
                status = self._send(w, event)
                if status.startswith("2"):
                    conn.execute(_SQL_DELIVERED, (status, row_id))
                    continue
                dead = (conn.execute(_SQL_FAILED, (status, row_id)).fetchone() or (None,))[0]
                if dead is not None:
                    log.error("webhook %s: event %s is dead after 24 h (%s)", w.id, event.get("event_id"), status)
                else:
                    log.warning("webhook %s: event %s not delivered (%s)", w.id, event.get("event_id"), status)
        return tries

    def _secret(self, ref: str) -> bytes | None:
        root = self.secrets_dir.resolve()
        path = (root / ref.removeprefix("secret://")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path.read_bytes().strip()

    def _send(self, w: Webhook, event: dict[str, Any]) -> str:
        """The HTTP status as text, or why nothing was sent."""
        secret = self._secret(w.secret_ref)
        if not secret:
            return "no secret"
        body = body_of(event)
        headers = {"Content-Type": "application/json", webhooks.HEADER: webhooks.sign(secret, body, int(self.clock()))}
        try:
            r = self.http.post(w.url, content=body, headers=headers, timeout=HTTP_TIMEOUT_S, follow_redirects=False)
        except httpx.HTTPError as exc:
            return type(exc).__name__  # never the URL with its query, never the body
        return str(r.status_code)
