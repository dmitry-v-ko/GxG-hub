"""AC-M10-01, AC-M10-02 (EX-20, AS-21…AS-23): webhook events on every status change, signed; retries with backoff,
`dead` after 24 h with an alert metric; a subscriber back after an hour gets every event in order.

The receiver is the kit's `webhooks.verify` behind an httpx MockTransport (no TCP in tests, AC-M0-01).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import psycopg
import pytest

from gxg_approval_notifier import webhooks as hooks_mod
from gxg_approval_notifier.webhooks import Webhooks
from gxg_connector_kit import webhooks

ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
EVENT_SCHEMA = json.loads((ROOT / "schemas/intent-event.schema.json").read_text("utf-8"))
SECRET = b"whsec-test-only-0123456789abcdef"
HUMAN = "oidc:https://idp.example.invalid:1"
SUBSCRIBER = {"id": "signals-alpha", "tenant": "t-alpha", "url": "https://signals.internal/hooks/gxg",
              "secret_ref": "secret://t-alpha/webhooks/signals"}  # fmt: skip


@dataclass
class Receiver:
    """A subscriber that checks every delivery like Signals will (AS-22) and answers 401 when it refuses."""

    clock: Any
    up: bool = True
    seen: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.up:
            raise httpx.ConnectError("down", request=request)
        try:
            event = webhooks.verify(
                SECRET, request.headers.get(webhooks.HEADER, ""), request.content, now=self.clock(), seen=self.seen
            )
        except webhooks.WebhookError as exc:
            self.refused.append(exc.reason)
            return httpx.Response(401)
        self.events.append(event)
        return httpx.Response(204)


@pytest.fixture
def receiver(clock: Any) -> Receiver:
    return Receiver(clock)


@pytest.fixture
def hooks(world: Any, pg: Any, receiver: Receiver, clock: Any, tmp_path: Path) -> Webhooks:
    world.webhooks = [SUBSCRIBER]
    world.reload()
    (tmp_path / "t-alpha/webhooks").mkdir(parents=True)
    (tmp_path / "t-alpha/webhooks/signals").write_bytes(SECRET + b"\n")
    client = httpx.Client(transport=httpx.MockTransport(receiver))
    return Webhooks(lambda: pg.connect("gxg_notifier"), world.store, tmp_path, client, clock=clock)


def run(h: Webhooks) -> None:
    h.fan_out()
    h.deliver()


def execute(pg: Any, intent_id: str, outcome: str = "succeeded") -> None:
    """T7, T9, T11 through the real functions of their roles."""
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        row = ex.execute("SELECT gxg_reserve(%s, 'test-executor')", (intent_id,)).fetchone()
        assert row is not None
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (row[0], intent_id))
        ex.execute("SELECT gxg_complete(%s, 'test-executor', %s, 'op-1', 'r-1', null)", (row[0], outcome))


def approve(owner: Any, decide_db: Any, intent_id: str) -> None:
    r = owner.execute("SELECT version, canonical_args_hash FROM intents WHERE intent_id = %s", (intent_id,)).fetchone()
    decide_db(intent_id, HUMAN, "approve", r[0], r[1])


def deliveries(owner: Any) -> list[tuple[Any, ...]]:
    return owner.execute(
        "SELECT event->>'status', attempts, delivered_at IS NOT NULL, dead_at IS NOT NULL, last_status "
        "FROM webhook_deliveries ORDER BY id"
    ).fetchall()


def test_every_status_change_is_delivered_signed(
    hooks: Webhooks, receiver: Receiver, propose: Any, owner: Any, decide_db: Any, pg: Any
) -> None:
    """AC-M10-01: T1 → T2 → T7 → T11: one valid, signed event per status change, in order, nothing else in it."""
    intent = propose(business_task_id="task-7").json()["intent_id"]
    approve(owner, decide_db, intent)
    execute(pg, intent)
    for _ in range(4):  # head of line: one event per pass
        run(hooks)
    statuses = [(e["status"], e["version"]) for e in receiver.events]
    assert [s for s, _ in statuses] == ["pending_approval", "approved", "executing", "succeeded"]
    assert [v for _, v in statuses] == sorted(v for _, v in statuses)  # version order
    for event in receiver.events:
        jsonschema.validate(event, EVENT_SCHEMA)  # no args, no preview: additionalProperties false
        assert event["intent_id"] == intent and event["business_task_id"] == "task-7"
    assert len({e["event_id"] for e in receiver.events}) == 4 and receiver.refused == []
    assert deliveries(owner) == [(s, 1, True, False, "204") for s, _ in statuses]
    assert owner.execute("SELECT count(*) FROM outbox WHERE topic = 'webhook' AND delivered_at IS NULL").fetchone() == (
        0,
    )


def test_receiver_refuses_a_bad_signature_an_old_t_and_a_replay(
    hooks: Webhooks, receiver: Receiver, propose: Any, clock: Any
) -> None:
    """AC-M10-01: the test receiver refuses a wrong signature, `t` older than 300 s and a repeated event_id."""
    propose()
    run(hooks)
    (sent,) = receiver.requests
    header, body = sent.headers[webhooks.HEADER], sent.content
    t = int(header.split(",")[0].removeprefix("t="))
    assert t == int(clock()) and json.loads(body)["status"] == "pending_approval"
    attempts = [
        (webhooks.sign(b"wrong-secret", body, t), body),  # bad signature
        (header, body.replace(b"pending_approval", b"approved")),  # changed body
        (webhooks.sign(SECRET, body, t - 301), body),  # t older than 300 s
        (header, body),  # replay of the delivered event
    ]
    for h, b in attempts:
        receiver(httpx.Request("POST", SUBSCRIBER["url"], headers={webhooks.HEADER: h}, content=b))
    assert receiver.refused == ["signature", "signature", "stale", "replay"]
    assert len(receiver.events) == 1


def test_unavailable_subscriber_backoff_then_dead_and_alert(
    hooks: Webhooks, receiver: Receiver, propose: Any, owner: Any, pg: Any
) -> None:
    """AC-M10-02: retries with exponential backoff (5 s doubling, at most 1 h); 24 h after the event → `dead`,
    the dead count is a metric (alert), the next event is tried."""
    receiver.up = False
    first, second = propose().json()["intent_id"], propose(args={"text": "b", "n": 2}).json()["intent_id"]
    run(hooks)
    gaps = []
    for _ in range(12):
        gap = owner.execute(
            "SELECT extract(epoch FROM next_at - now())::int FROM webhook_deliveries ORDER BY id LIMIT 1"
        ).fetchone()[0]
        gaps.append(gap)
        owner.execute("UPDATE webhook_deliveries SET next_at = now() WHERE id = (SELECT min(id) FROM webhook_deliveries)")
        hooks.deliver()
    assert all(abs(g - want) <= 1 for g, want in zip(gaps[:6], [5, 10, 20, 40, 80, 160], strict=True)), gaps
    assert max(gaps) <= 3600 and gaps[-1] >= 3590  # capped at 1 h
    assert owner.execute("SELECT count(*) FROM webhook_deliveries WHERE dead_at IS NOT NULL").fetchone() == (0,)
    assert len({r.url.host for r in receiver.requests}) == 1 and all(
        json.loads(r.content)["intent_id"] == first for r in receiver.requests
    )  # head of line: the second event waits behind the first
    owner.execute("UPDATE webhook_deliveries SET created_at = created_at - interval '24 hours', next_at = now()")
    hooks.deliver()  # the first event fails once more, 24 h after it happened: dead
    rows = deliveries(owner)
    assert rows[0][3] is True and rows[0][4] == "ConnectError" and rows[1][3] is False
    with pg.connect("gxg_api") as api:
        metrics = api.execute("SELECT name, labels, value FROM gxg_metrics() WHERE name LIKE 'gxg_webhook%'").fetchall()
    assert ("gxg_webhook_dead", {"subscriber": "signals-alpha"}, 1.0) in metrics
    assert ("gxg_webhook_pending", {"subscriber": "signals-alpha"}, 1.0) in metrics
    receiver.up = True
    hooks.deliver()  # the second event goes now (it is past 24 h too, but this try succeeds)
    assert [e["intent_id"] for e in receiver.events] == [second]


def test_subscriber_back_after_an_hour_gets_everything_in_order(
    hooks: Webhooks, receiver: Receiver, propose: Any, owner: Any, decide_db: Any, pg: Any
) -> None:
    """AC-M10-02: down for an hour while one intent moves through four statuses; afterwards every event arrives,
    in version order, each once."""
    receiver.up = False
    intent = propose().json()["intent_id"]
    run(hooks)
    approve(owner, decide_db, intent)
    run(hooks)
    execute(pg, intent)
    run(hooks)
    owner.execute(
        "UPDATE webhook_deliveries SET created_at = created_at - interval '1 hour', next_at = next_at - interval '2 hours'"
    )
    receiver.up = True
    for _ in range(6):
        run(hooks)
    assert [(e["status"], e["version"]) for e in receiver.events] == sorted(
        ((e["status"], e["version"]) for e in receiver.events), key=lambda x: x[1]
    )
    assert [e["status"] for e in receiver.events] == ["pending_approval", "approved", "executing", "succeeded"]
    assert all(r[2] and not r[3] for r in deliveries(owner))


def test_fan_out_by_tenant_and_prefix_and_gone_subscribers(
    hooks: Webhooks, receiver: Receiver, world: Any, propose: Any, owner: Any
) -> None:
    """AS-21: a subscriber gets its tenant's events only (and those of its business_task_prefix); a subscriber
    removed from the bundle gets nothing more."""
    world.webhooks = [SUBSCRIBER | {"id": "prefixed", "business_task_prefix": "sig-"},
                      SUBSCRIBER | {"id": "beta", "tenant": "t-beta"}]  # fmt: skip
    world.reload("c" * 40)
    propose(business_task_id="sig-1")
    propose(args={"text": "b", "n": 2}, business_task_id="other")
    propose(tenant="t-beta", requester="agent:t-beta:bot", connection="demo-b", resource="demo:demo-b/board/b1")
    hooks.fan_out()
    rows = owner.execute(
        "SELECT subscriber_id, tenant_id, event->>'business_task_id' FROM webhook_deliveries ORDER BY id"
    ).fetchall()
    assert rows == [("prefixed", "t-alpha", "sig-1"), ("beta", "t-beta", None)]
    world.webhooks = []
    world.reload("d" * 40)
    hooks.deliver()
    assert receiver.requests == [] and [r[4] for r in deliveries(owner)] == ["not in the bundle"] * 2


def test_no_secret_no_unsigned_delivery(
    hooks: Webhooks, receiver: Receiver, propose: Any, owner: Any, tmp_path: Path
) -> None:
    """AS-22/AS-28: without the subscriber's secret file nothing is sent; a secret_ref outside the directory is
    no secret either."""
    (tmp_path / "t-alpha/webhooks/signals").unlink()
    propose()
    run(hooks)
    assert receiver.requests == [] and deliveries(owner)[0][4] == "no secret"
    hooks.bundles.current.webhooks  # the bundle stays; the ref is resolved inside secrets_dir only
    assert hooks._secret("secret://../../etc/passwd") is None


def test_body_is_compact_sorted_json() -> None:
    assert hooks_mod.body_of({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_notifier_role_cannot_rewrite_events(pg: Any, hooks: Webhooks, propose: Any) -> None:
    """AS-27: the notifier marks a delivery, it cannot change the event or retarget it."""
    propose()
    hooks.fan_out()
    with pg.connect("gxg_notifier") as conn:
        for sql in ("UPDATE webhook_deliveries SET event = '{}'", "UPDATE webhook_deliveries SET subscriber_id = 'x'",
                    "DELETE FROM webhook_deliveries"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
