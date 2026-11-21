"""T1 over HTTP: AC-M7-03, AC-M7-04, AC-M7-07, AC-M7-08, AC-M7-10 and the T1 conditions of the transition table."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from psycopg_pool import ConnectionPool

from gxg_approval.api import create_app
from gxg_approval.crypto import Decryptor
from gxg_connector_kit import _schema


def stored_hash(owner: psycopg.Connection[Any]) -> str:
    row = owner.execute("SELECT canonical_args_hash FROM intents").fetchone()
    assert row is not None
    return str(row[0])


def count(owner: psycopg.Connection[Any], table: str = "intents") -> int:
    row = owner.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_human_approval_by_default_for_external_effect(
    propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    r = propose()
    assert r.status_code == 201, r.text
    doc = r.json()
    assert _schema.validator("intent-status").is_valid(doc)
    assert doc["status"] == "pending_approval" and doc["version"] == 1
    assert doc["created_at"].endswith("Z") and doc["expires_at"].endswith("Z") and doc["execution_deadline"] is None
    stored = owner.execute("SELECT connector_id, execute_tool, effect_class, policy_version FROM intents").fetchone()
    assert stored == ("demo", "demo.message.execute", "external_effect", "b" * 40)  # AS-26: from the bundle
    assert doc["approval"] == {"mode": "human", "required_approvals": 1, "received_approvals": 0, "rule_ids": []}
    row = owner.execute("SELECT topic, payload FROM outbox").fetchall()
    assert row == [
        ("notify", {"intent_id": doc["intent_id"], "tenant_id": "t-alpha", "status": "pending_approval", "version": 1})
    ]
    audit = owner.execute("SELECT actor, action, tenant_id, intent_id::text, to_status, details FROM audit_events")
    assert audit.fetchall() == [
        ("demo", "propose", "t-alpha", doc["intent_id"], "pending_approval",
         {"requester": "agent:t-alpha:bot", "identity_mode": "connector_attested", "rule_ids": [], "mode": "human",
          "args_hash": stored_hash(owner)}),
    ]  # fmt: skip


def test_write_reversible_is_approved_without_human(
    propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    doc = propose(tool="demo.note.propose").json()
    assert doc["status"] == "approved" and doc["approval"]["mode"] == "not_required"
    assert doc["execution_deadline"] is not None
    assert owner.execute("SELECT topic FROM outbox").fetchall() == [("execute",)]
    assert count(owner, "decisions") == 0


def test_hash_mismatch_is_invalid_input(client: TestClient, world: Any, owner: psycopg.Connection[Any]) -> None:
    """AC-M7-03."""
    body = world.body()
    body["canonical_args"]["text"] = "something else"
    r = client.post("/v1/intents", json=body, headers=world.auth())
    assert r.status_code == 400
    assert r.json() == {
        "code": "invalid_input",
        "message": "canonical_args_hash does not match canonical_args",
        "retryable": False,
    }
    assert count(owner) == 0


def test_body_outside_the_schema_is_invalid_input(
    client: TestClient, world: Any, owner: psycopg.Connection[Any]
) -> None:
    bodies: list[tuple[Any, str]] = [
        ({**world.body(), "extra": 1}, "body"),
        ({**world.body(), "idempotency_key": "short"}, "idempotency_key"),
        ([], "body"),
        ("x", "body"),
    ]
    for body, where in bodies:
        r = client.post("/v1/intents", json=body, headers=world.auth())
        assert r.status_code == 400 and r.json()["message"] == f"intent-create: invalid at {where}", body
    r = client.post("/v1/intents", content=b"{not json", headers=world.auth())
    assert r.status_code == 400
    assert count(owner) == 0


def test_float_args_are_invalid_input(client: TestClient, world: Any) -> None:
    body = world.body()
    body["canonical_args"]["n"] = 1.5
    assert client.post("/v1/intents", json=body, headers=world.auth()).status_code == 400


def test_idempotent_repeat(client: TestClient, world: Any, owner: psycopg.Connection[Any]) -> None:
    """AC-M7-04 (EX-06): same key and hash → 200, same intent; other hash → 409."""
    body = world.body(key="idem-0001")
    first = client.post("/v1/intents", json=body, headers=world.auth())
    again = client.post("/v1/intents", json=body, headers=world.auth())
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    other = world.body(key="idem-0001", args={"text": "changed"})
    r = client.post("/v1/intents", json=other, headers=world.auth())
    assert r.status_code == 409
    assert r.json()["message"] == "idempotency_key is already used with other parameters"
    assert count(owner) == 1 and count(owner, "outbox") == 1


def test_same_key_in_another_tenant_is_another_intent(
    propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    a = propose(key="idem-shared")
    b = propose(
        key="idem-shared",
        tenant="t-beta",
        requester="agent:t-beta:bot",
        connection="demo-b",
        resource="demo:demo-b/board/b2",
    )
    assert a.status_code == b.status_code == 201
    assert a.json()["intent_id"] != b.json()["intent_id"]


def test_50_parallel_identical_requests_make_one_row(
    world: Any, api_pool: ConnectionPool[Any], owner: psycopg.Connection[Any]
) -> None:
    """AC-M7-04: 50 concurrent identical proposes → exactly one row."""
    body = world.body(key="idem-race-1")
    app = create_app(world.deps(api_pool))

    async def run() -> list[httpx.Response]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
            return await asyncio.gather(*(c.post("/v1/intents", json=body, headers=world.auth()) for _ in range(50)))

    responses = asyncio.run(run())
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 49 + [201]
    assert len({r.json()["intent_id"] for r in responses}) == 1
    assert count(owner) == 1


def test_31st_propose_within_an_hour_is_rate_limited(
    propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]
) -> None:
    """AC-M7-07 (EX-17): 31st propose → 429 with retry_after_s > 0, no row, nothing in the outbox."""
    world.set_policy("t-alpha", [], quotas={"pending_per_requester_max": 100})
    metric = {"tenant": "t-alpha", "requester": "agent:t-alpha:bot"}
    metric_before = REGISTRY.get_sample_value("gxg_quota_rejections_total", metric) or 0
    for _ in range(30):
        assert propose().status_code == 201
    owner.execute("DELETE FROM outbox")
    r = propose()
    assert r.status_code == 429
    err = r.json()
    assert err["code"] == "rate_limited" and err["retry_after_s"] > 0 and err["retryable"] is True
    assert err["message"] == "quota propose_per_requester_per_hour exceeded"
    assert r.headers["retry-after"] == str(err["retry_after_s"])
    assert count(owner) == 30 and count(owner, "outbox") == 0
    audit = owner.execute("SELECT actor, tenant_id, details FROM audit_events WHERE action = 'quota_rejected'")
    assert audit.fetchall() == [
        ("demo", "t-alpha", {"requester": "agent:t-alpha:bot", "quota": "propose_per_requester_per_hour"})
    ]
    sample = {"tenant": "t-alpha", "requester": "agent:t-alpha:bot"}
    assert REGISTRY.get_sample_value("gxg_quota_rejections_total", sample) == metric_before + 1  # AP-07
    # another agent of the tenant is not limited by this agent's quota
    assert propose(requester="agent:t-alpha:other").status_code == 201


def test_pending_caps(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    world.set_policy("t-alpha", [], quotas={"pending_per_requester_max": 2, "pending_per_tenant_max": 3})
    assert [propose().status_code for _ in range(3)] == [201, 201, 429]
    assert propose(requester="agent:t-alpha:other").status_code == 201
    r = propose(requester="agent:t-alpha:signals")
    assert r.status_code == 429 and r.json()["retry_after_s"] == 60
    assert count(owner) == 3


def test_quota_window_is_one_hour(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    world.set_policy("t-alpha", [], quotas={"propose_per_requester_per_hour": 1})
    assert propose().status_code == 201
    owner.execute("UPDATE intents SET created_at = now() - interval '59 minutes'")
    r = propose()
    assert r.status_code == 429 and 55 <= r.json()["retry_after_s"] <= 61
    owner.execute("UPDATE intents SET created_at = now() - interval '61 minutes'")
    assert propose().status_code == 201


def test_unknown_connector_or_foreign_key(client: TestClient, world: Any, owner: psycopg.Connection[Any]) -> None:
    """AC-M7-08 (EX-21): connector not registered for the tenant, or a JWT signed with another key."""
    r = client.post("/v1/intents", json=world.body(), headers=world.auth("rogue"))
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    forged = world.auth("demo", signer=Ed25519PrivateKey.generate())
    r = client.post("/v1/intents", json=world.body(), headers=forged)
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
    for headers in ({}, {"Authorization": "Basic abc"}, world.auth("approval-mcp"), world.auth("demo", aud="x")):
        assert client.post("/v1/intents", json=world.body(), headers=headers).status_code == 401
    assert count(owner) == 0


def test_requester_tool_and_connection_must_come_from_the_bundle(
    client: TestClient, world: Any, owner: psycopg.Connection[Any]
) -> None:
    """AS-08, AS-26, GR-02."""
    not_registered = "connector or requester is not registered for this tenant"
    not_a_tool = "tool is not a registered effect tool of this connector"
    bad_connection = "connection_id is not a connection of the tenant or of the resource"
    cases = [
        (world.body(requester="agent:t-alpha:ghost"), 403, not_registered),
        (world.body(requester="agent:t-beta:bot"), 403, not_registered),  # principal of another tenant
        (world.body(tenant="t-gamma"), 403, not_registered),
        (world.body(tool="demo.board.search"), 403, not_a_tool),  # read tool: not an effect
        (world.body(tool="demo.unknown.propose"), 403, not_a_tool),
        ({**world.body(), "tool_version": "9.9.9"}, 403, not_a_tool),
        (world.body(tool="demo.orphan.propose"), 403, not_a_tool),
        (
            world.body(tool="demo.memo.write"),
            403,
            not_a_tool,
        ),  # a direct write tool, not propose  # no executes_via in the bundle
        (world.body(connection="demo-b", resource="demo:demo-b/board/b2"), 400, bad_connection),  # t-beta's
        (world.body(resource="demo:demo-other/board/b1"), 400, bad_connection),
    ]
    for body, code, message in cases:
        r = client.post("/v1/intents", json=body, headers=world.auth())
        assert (r.status_code, r.json()["message"]) == (code, message), body
    assert count(owner) == 0


def test_kill_switch_inside_t1(propose: Callable[..., Any], owner: psycopg.Connection[Any]) -> None:
    """AS-05: global, tenant and tool scopes."""
    for scope in ("global", "tenant:t-alpha", "tool:t-alpha:demo.message.propose"):
        owner.execute("INSERT INTO switches VALUES (%s, true, 'test', 'op', now())", (scope,))
        r = propose()
        assert r.status_code == 423 and r.json()["code"] == "effect_disabled", scope
        assert r.json()["message"] == "effects are disabled for this tenant or tool"
        owner.execute("DELETE FROM switches")
    owner.execute("INSERT INTO switches VALUES ('tool:t-alpha:demo.note.propose', true, 'test', 'op', now())")
    owner.execute("INSERT INTO switches VALUES ('tenant:t-beta', true, 'test', 'op', now())")
    owner.execute("INSERT INTO switches VALUES ('global', false, 'cleared', 'op', now())")
    assert propose().status_code == 201
    assert count(owner) == 1


def test_policy_deny_stores_nothing(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    world.set_policy("t-alpha", [{"id": "no-msg", "match": {"tools": ["demo.message.propose"]}, "decision": "deny"}])
    r = propose()
    assert r.status_code == 403 and r.json()["message"] == "denied by the approval policy"
    assert count(owner) == 0 and count(owner, "outbox") == 0
    assert owner.execute("SELECT actor, action, tenant_id, details FROM audit_events").fetchall() == [
        ("demo", "propose_denied", "t-alpha",
         {"requester": "agent:t-alpha:bot", "tool": "demo.message.propose", "reason": "no-msg"})
    ]  # fmt: skip


def test_auto_approval_writes_policy_decision(
    propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]
) -> None:
    """EFF-03, EFF-22."""
    world.set_policy(
        "t-alpha",
        [
            {
                "id": "auto-b",
                "match": {"tools": ["demo.message.propose"]},
                "decision": "auto_approve",
                "limits": {"max_per_hour": 5},
            }
        ],
    )
    doc = propose().json()
    assert doc["status"] == "approved" and doc["approval"]["mode"] == "auto"
    assert doc["approval"]["rule_ids"] == ["auto-b"]
    rows = owner.execute("SELECT decided_by, decision, intent_version FROM decisions").fetchall()
    assert rows == [(f"policy:auto-b@{'b' * 40}", "approve", 1)]
    assert owner.execute("SELECT topic FROM outbox").fetchall() == [("execute",)]


def test_auto_limits_use_sliding_hour_and_day(
    propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]
) -> None:
    """AP-03: the hourly limit counts the last hour, the daily one the last day."""
    world.set_policy(
        "t-alpha",
        [
            {
                "id": "a",
                "match": {"tools": ["demo.message.propose"]},
                "decision": "auto_approve",
                "limits": {"max_per_hour": 1, "max_per_day": 2},
            }
        ],
    )
    assert propose().json()["approval"]["mode"] == "auto"
    assert propose().json()["approval"]["mode"] == "human"  # hourly limit 1 used
    owner.execute("UPDATE intents SET created_at = now() - interval '2 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"  # the hour is free again, day: 1 of 2
    owner.execute("UPDATE intents SET created_at = now() - interval '3 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "human"  # day: 2 of 2
    owner.execute("UPDATE intents SET created_at = now() - interval '25 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"
    owner.execute("UPDATE intents SET tenant_id = 't-beta', created_at = now() WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"  # another tenant's auto intents do not count


def test_30_parallel_proposes_with_auto_limit_10(
    world: Any, api_pool: ConnectionPool[Any], owner: psycopg.Connection[Any]
) -> None:
    """AC-M7-06 (AP-03): exactly 10 auto-approved, 20 pending."""
    world.set_policy(
        "t-alpha",
        [
            {
                "id": "auto10",
                "match": {"tools": ["demo.message.propose"]},
                "decision": "auto_approve",
                "limits": {"max_per_hour": 10},
            }
        ],
    )
    app = create_app(world.deps(api_pool))

    async def run() -> list[httpx.Response]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
            return await asyncio.gather(
                *(c.post("/v1/intents", json=world.body(), headers=world.auth()) for _ in range(30))
            )

    responses = asyncio.run(run())
    assert all(r.status_code == 201 for r in responses)
    rows = owner.execute("SELECT status, approval_mode, count(*) FROM intents GROUP BY 1, 2 ORDER BY 1").fetchall()
    assert rows == [("approved", "auto", 10), ("pending_approval", "human", 20)]
    assert count(owner, "decisions") == 10


def test_payload_is_encrypted(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    """AC-M7-10: no plaintext of args in args_ciphertext / preview_ciphertext over 100 intents."""
    world.set_policy("t-alpha", [], quotas={"propose_per_requester_per_hour": 1000, "pending_per_requester_max": 1000})
    secrets = [f"plain-{uuid.uuid4().hex}" for _ in range(100)]
    for s in secrets:
        assert propose(args={"text": s, "to": "partner-42"}).status_code == 201
    rows = owner.execute("SELECT tenant_id, args_ciphertext, preview_ciphertext FROM intents").fetchall()
    assert len(rows) == 100
    needles = [s.encode() for s in secrets] + [b"partner-42", b"hello", b"Board b1", b"Send a message"]
    assert sum(1 for _, a, p in rows for n in needles if n in a or n in p) == 0
    # the ciphertext is the payload: the executor's args key opens it, the preview key does not
    args = Decryptor("args", {"t-alpha": world.payload_private["t-alpha"]["args"]})
    opened = {args.open("t-alpha", bytes(a)) for _, a, _ in rows}
    assert {o for o in opened if secrets[0].encode() in o}
    preview = Decryptor("preview", {"t-alpha": world.payload_private["t-alpha"]["preview"]})
    assert b"Send a message" in preview.open("t-alpha", bytes(rows[0][2]))


def test_rid_with_colon_in_the_id(propose: Callable[..., Any]) -> None:
    """GR-02 check takes the connection between the first ':' and the next '/'."""
    assert propose(resource="demo:demo-main/board/a:b:c").status_code == 201


def test_policy_sees_requester_groups_and_resource_masks(propose: Callable[..., Any], world: Any) -> None:
    world.set_policy("t-alpha", [
        {"id": "bots-two", "match": {"requesters": ["group:bots"], "resources": ["demo:demo-main/board/*"]},
         "decision": "require_approval", "approvals": 2},
        {"id": "b9-step", "match": {"resources": ["demo:demo-main/board/b9"]}, "decision": "require_approval",
         "step_up": True},
    ])  # fmt: skip
    bot = propose().json()["approval"]
    assert (bot["required_approvals"], bot["rule_ids"]) == (2, ["bots-two"])
    signals = propose(requester="agent:t-alpha:signals").json()["approval"]
    assert (signals["required_approvals"], signals["rule_ids"]) == (1, [])
    b9 = propose(requester="agent:t-alpha:signals", resource="demo:demo-main/board/b9").json()["approval"]
    assert b9["rule_ids"] == ["b9-step"]


def test_ap02_alert_is_logged(propose: Callable[..., Any], world: Any, caplog: pytest.LogCaptureFixture) -> None:
    world.set_policy("t-alpha", [{"id": "auto-w", "match": {"tools": ["demo.wire.propose"]},
                                  "decision": "auto_approve", "limits": {"max_per_hour": 5}}])  # fmt: skip
    doc = propose(tool="demo.wire.propose").json()
    assert doc["approval"]["mode"] == "human"
    assert ("gxg_approval", 30, "ALERT policy tenant=t-alpha: auto_approve ignored for high_risk tool "
            "demo.wire.propose: ['auto-w']") in caplog.record_tuples  # fmt: skip
