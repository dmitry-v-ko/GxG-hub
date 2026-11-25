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

BOT, SIGNALS = "agent:t-alpha:bot", "agent:t-alpha:signals"
HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"


def stored_hash(owner: psycopg.Connection[Any]) -> str:
    row = owner.execute("SELECT canonical_args_hash FROM intents").fetchone()
    assert row is not None
    return str(row[0])


def count(owner: psycopg.Connection[Any], table: str = "intents") -> int:
    # M10: webhook events (one per status change) are counted by test_webhooks.py, not here
    where = " WHERE topic <> 'webhook'" if table == "outbox" else ""
    row = owner.execute(f"SELECT count(*) FROM {table}{where}").fetchone()
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
    row = owner.execute("SELECT topic, payload FROM outbox WHERE topic <> 'webhook'").fetchall()
    assert row == [
        ("notify", {"intent_id": doc["intent_id"], "tenant_id": "t-alpha", "status": "pending_approval", "version": 1,
                    "title": "Send a message", "target_label": "Board b1", "requester": "agent:t-alpha:bot",
                    "expires_at": doc["expires_at"], "approvers": [HUMAN1, HUMAN2]})
    ]  # fmt: skip  # AS-19: no fields, changes or args
    audit = owner.execute("SELECT actor, action, tenant_id, intent_id::text, to_status, details FROM audit_events")
    assert audit.fetchall() == [
        ("demo", "propose", "t-alpha", doc["intent_id"], "pending_approval",
         {"requester": "agent:t-alpha:bot", "identity_mode": "connector_attested", "rule_ids": [], "mode": "human",
          "args_hash": stored_hash(owner), "policy_version": "b" * 40}),
    ]  # fmt: skip  # GR-14: the bundle version of the intent is in the audit row too


def test_write_reversible_is_approved_without_human(
    propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    doc = propose(tool="demo.note.propose").json()
    assert doc["status"] == "approved" and doc["approval"]["mode"] == "not_required"
    assert doc["execution_deadline"] is not None
    assert owner.execute("SELECT topic FROM outbox WHERE topic <> 'webhook'").fetchall() == [("execute",)]
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
        ({**world.body(), "requester": {"principal": "", "tenant_id": "t-alpha"}}, "requester/principal"),
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


def test_same_key_of_two_requesters_is_two_intents(
    client: TestClient, world: Any, owner: psycopg.Connection[Any]
) -> None:
    """EFF-08 (owner 2026-11-21): idempotency per (tenant, requester, key). Two agents of one tenant, one key: each
    gets its own intent and repeats find only their own; the other's intent is never returned."""
    key = "signals:cand-1:v1"
    mine = client.post("/v1/intents", json=world.body(key=key, requester=SIGNALS), headers=world.auth())
    theirs = client.post("/v1/intents", json=world.body(key=key, requester=BOT), headers=world.auth())
    assert (mine.status_code, theirs.status_code) == (201, 201)  # same args, same key: still two intents
    assert mine.json()["intent_id"] != theirs.json()["intent_id"]
    again = client.post("/v1/intents", json=world.body(key=key, requester=BOT), headers=world.auth())
    assert again.status_code == 200 and again.json() == theirs.json()
    other = client.post("/v1/intents", json=world.body(key=key, requester=SIGNALS, args={"text": "x"}),
                        headers=world.auth())  # fmt: skip
    assert other.status_code == 409 and mine.json()["intent_id"] not in other.text
    rows = owner.execute("SELECT requester_principal FROM intents ORDER BY requester_principal").fetchall()
    assert rows == [(BOT,), (SIGNALS,)]


def test_the_database_enforces_one_key_per_requester(owner: psycopg.Connection[Any], world: Any, client: Any) -> None:
    constraint = owner.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'intents'::regclass AND contype = 'u'"
    ).fetchall()
    assert constraint == [("UNIQUE (tenant_id, requester_principal, idempotency_key)",)]


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


def test_same_principal_and_key_in_two_tenants_is_two_intents(propose: Callable[..., Any]) -> None:
    """EFF-08: the key is per (tenant, requester, key); one principal ID in both tenants is two requesters."""
    beta = {"tenant": "t-beta", "connection": "demo-b", "resource": "demo:demo-b/board/b2"}
    a = propose(key="idem-shared-2", requester="agent:shared")
    b = propose(key="idem-shared-2", requester="agent:shared", **beta)
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


def test_quotas_count_only_the_tenant(propose: Callable[..., Any], world: Any) -> None:
    """AP-05: another tenant's intents never count, even for the same principal ID (registered in both)."""
    beta = {"tenant": "t-beta", "connection": "demo-b", "resource": "demo:demo-b/board/b2"}
    assert propose(requester="agent:shared").status_code == 201  # t-alpha, pending
    world.set_policy("t-beta", [], quotas={"propose_per_requester_per_hour": 1, "pending_per_tenant_max": 1})
    assert propose(requester="agent:shared", **beta).status_code == 201
    assert propose(requester="agent:t-beta:bot", **beta).status_code == 429  # t-beta's own pending counts


def test_quota_window_is_one_hour(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    world.set_policy("t-alpha", [], quotas={"propose_per_requester_per_hour": 2})
    first, second = propose().json()["intent_id"], propose().json()["intent_id"]
    owner.execute("UPDATE intents SET created_at = now() - interval '59 minutes' WHERE intent_id = %s", (first,))
    owner.execute("UPDATE intents SET created_at = now() - interval '10 minutes' WHERE intent_id = %s", (second,))
    r = propose()
    assert r.status_code == 429 and 55 <= r.json()["retry_after_s"] <= 61  # until the oldest leaves the hour
    owner.execute("UPDATE intents SET created_at = now() - interval '61 minutes' WHERE intent_id = %s", (first,))
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
    assert doc["approval"]["received_approvals"] == 0  # human approvals only; the policy decision is not one
    rows = owner.execute("SELECT decided_by, decision, intent_version FROM decisions").fetchall()
    assert rows == [(f"policy:auto-b@{'b' * 40}", "approve", 1)]
    assert owner.execute("SELECT topic FROM outbox WHERE topic <> 'webhook'").fetchall() == [("execute",)]


def test_policy_decision_only_for_auto_intents(
    propose: Callable[..., Any], world: Any, pg: Any, owner: psycopg.Connection[Any]
) -> None:
    """EFF-03: gxg_api writes a policy decision only for its own fresh auto-approved intent."""
    doc = propose(tool="demo.note.propose").json()
    assert (doc["status"], doc["approval"]["mode"], doc["version"]) == ("approved", "not_required", 1)
    world.set_policy("t-alpha", [{"id": "auto-b", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "auto_approve", "limits": {"max_per_hour": 5}}])  # fmt: skip
    auto = propose().json()
    assert auto["approval"]["mode"] == "auto"
    with pg.connect("gxg_api") as api:
        for intent in (doc["intent_id"], auto["intent_id"]):  # not an auto intent; already has its decision
            with pytest.raises(psycopg.Error) as err:
                api.execute("SELECT gxg_record_auto_decision(%s)", (intent,))
            assert err.value.sqlstate == "GX409", intent
    assert owner.execute("SELECT intent_id::text FROM decisions").fetchall() == [(auto["intent_id"],)]


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
    owner.execute("UPDATE intents SET created_at = now() - interval '50 minutes' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "human"  # still inside the hour
    owner.execute("UPDATE intents SET created_at = now() - interval '2 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"  # the hour is free again, day: 1 of 2
    owner.execute("UPDATE intents SET created_at = now() - interval '23 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "human"  # day: 2 of 2, still inside the day
    owner.execute("UPDATE intents SET created_at = now() - interval '25 hours' WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"
    owner.execute("UPDATE intents SET tenant_id = 't-beta', created_at = now() WHERE approval_mode = 'auto'")
    assert propose().json()["approval"]["mode"] == "auto"  # another tenant's auto intents do not count


def test_auto_limit_counts_only_its_rule(propose: Callable[..., Any], world: Any) -> None:
    """AP-03: each rule's limit counts the intents that rule auto-approved, not those of another rule."""
    world.set_policy("t-alpha", [
        {"id": f"auto-{who}", "match": {"requesters": [f"agent:t-alpha:{who}"]}, "decision": "auto_approve",
         "limits": {"max_per_hour": 1}}
        for who in ("bot", "other")
    ])  # fmt: skip
    assert propose().json()["approval"]["rule_ids"] == ["auto-bot"]
    assert propose(requester="agent:t-alpha:other").json()["approval"]["mode"] == "auto"
    assert propose().json()["approval"]["mode"] == "human"  # auto-bot used its one


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


def test_allowed_approvers_are_stored(propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]) -> None:
    """AP-01.4: the approver set is frozen into the intent: every approver of the tenant by role, narrowed by the
    policy (M9: gxg_decide checks it, so the database refuses a decision by anyone else)."""
    h1, h2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
    propose()
    world.set_policy("t-alpha", [{"id": "narrow", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvers": [h2, h1]}])  # fmt: skip
    propose()
    rows = owner.execute("SELECT allowed_approvers, rule_ids FROM intents ORDER BY created_at, intent_id").fetchall()
    assert sorted(rows, key=lambda r: r[1]) == [([h1, h2], []), ([h1, h2], ["narrow"])]
    outsider = "oidc:https://not-an-approver:9"
    world.set_policy("t-alpha", [{"id": "one", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvers": [h2, outsider]}])  # fmt: skip
    propose()
    row = owner.execute("SELECT allowed_approvers FROM intents WHERE rule_ids = '{one}'").fetchone()
    assert row == ([h2],)  # a policy approver without the role is not an approver
    propose(tool="demo.note.propose")  # not a human decision: no set
    assert owner.execute("SELECT allowed_approvers FROM intents WHERE approval_mode <> 'human'").fetchall() == [(None,)]


def _scans(plan: dict[str, Any]) -> list[str]:
    here = f"{plan['Node Type']}:{plan.get('Relation Name', '')}:{plan.get('Index Name', '')}"
    return [here, *(scan for child in plan.get("Plans", []) for scan in _scans(child))]


def test_t1_counts_use_indexes_on_a_long_history(owner: psycopg.Connection[Any]) -> None:
    """AP-03, AP-05 under RET-02 history: the quota and auto-limit counts of T1 never scan the tenant's history."""
    from gxg_approval import intents

    with owner.transaction():  # 100 000 terminal intents of one year; the T1 guard does not allow such rows
        owner.execute("ALTER TABLE intents DISABLE TRIGGER intents_insert_guard")
        owner.execute(
            """INSERT INTO intents (intent_id, tenant_id, requester_principal, tool_key, tool_version, connector_id,
                 execute_tool, connection_id, resource_id, effect_class, canonical_args_hash, idempotency_key,
                 policy_version, rule_ids, approval_mode, required_approvals, step_up_required, status, created_at,
                 expires_at, execution_window_s)
               SELECT gen_random_uuid(), 't-alpha', CASE WHEN i % 2 = 0 THEN 'agent:t-alpha:bot'
                        ELSE 'agent:t-alpha:other' END, 'demo.message.propose', '1.0.0', 'demo',
                      'demo.message.execute', 'demo-main', 'demo:demo-main/board/b1', 'external_effect',
                      repeat('0', 64), 'h-' || i, 'b', CASE WHEN i % 3 = 0 THEN '{auto-b}'::text[] ELSE '{}' END,
                      CASE WHEN i % 3 = 0 THEN 'auto' ELSE 'human' END, CASE WHEN i % 3 = 0 THEN 0 ELSE 1 END,
                      false, 'expired', now() - interval '2 days' - make_interval(mins => i * 5),
                      now() - interval '1 day', 3600
                 FROM generate_series(1, 100000) AS i"""
        )
        owner.execute("ALTER TABLE intents ENABLE TRIGGER intents_insert_guard")
    owner.execute("ANALYZE intents")
    for sql, params in (
        (intents._SQL_COUNTS, {"t": "t-alpha", "r": "agent:t-alpha:bot"}),
        (intents._SQL_AUTO_USED, ("t-alpha", "auto-b")),
    ):
        row = owner.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
        assert row is not None
        scans = _scans(row[0][0]["Plan"])
        assert not any(s.startswith("Seq Scan:") for s in scans) and any(":intents:" in s for s in scans), scans
        if sql is intents._SQL_AUTO_USED:  # the day window needs its own index, not the tenant part of another
            assert any(s.endswith(":intents_tenant_id_created_at_idx") for s in scans), scans
    owner.execute("TRUNCATE intents CASCADE")


def test_no_approver_can_decide_alert(
    propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A human intent whose frozen approver set is empty can only expire: the operator is told (M9)."""
    outsider = "oidc:https://not-an-approver:9"
    world.set_policy("t-alpha", [{"id": "nobody", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvers": [outsider]}])  # fmt: skip
    doc = propose().json()
    assert owner.execute("SELECT allowed_approvers FROM intents").fetchall() == [([],)]
    assert ("gxg_approval", 30, f"ALERT policy tenant=t-alpha: no approver of the tenant may decide intent "
            f"{doc['intent_id']}") in caplog.record_tuples  # fmt: skip
    payload = owner.execute("SELECT payload FROM outbox WHERE topic <> 'webhook'").fetchone()
    assert payload is not None and payload[0]["approvers"] == []


def test_preview_envelope_carries_preview_and_canonical_args(
    propose: Callable[..., Any], world: Any, owner: psycopg.Connection[Any]
) -> None:
    """EFF-33 (M9): the ui reads only preview_ciphertext, so the canonical args travel in the preview envelope."""
    import json

    body = world.body(args={"text": "envelope", "n": 3})
    r = propose(args={"text": "envelope", "n": 3})
    assert r.status_code == 201, r.text
    row = owner.execute("SELECT preview_ciphertext FROM intents").fetchone()
    assert row is not None
    opened = Decryptor("preview", {"t-alpha": world.payload_private["t-alpha"]["preview"]}).open(
        "t-alpha", bytes(row[0])
    )
    assert json.loads(opened) == {"preview": body["preview"], "canonical_args": {"text": "envelope", "n": 3}}


def test_no_alert_when_approvers_exist(
    propose: Callable[..., Any], world: Any, caplog: pytest.LogCaptureFixture
) -> None:
    propose()  # human, the tenant has approvers
    world.set_policy("t-alpha", [{"id": "auto", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "auto_approve", "limits": {"max_per_hour": 5}}])  # fmt: skip
    propose()  # auto: no approver set at all
    assert [m for _, _, m in caplog.record_tuples if "no approver" in m] == []


def test_max_attempts_of_the_policy_is_frozen_at_t1(
    propose: Callable[..., Any], owner: psycopg.Connection[Any], world: Any
) -> None:
    """M10: the policy's `max_attempts` (default 3) is stored at T1; a later policy does not change it (AP-04)."""
    first = propose().json()["intent_id"]
    world.set_policy("t-alpha", [], defaults={"max_attempts": 1})
    second = propose(args={"text": "b", "n": 2}).json()["intent_id"]
    world.set_policy("t-alpha", [], defaults={"max_attempts": 2})
    rows = owner.execute(
        "SELECT intent_id::text, max_attempts FROM intents WHERE intent_id IN (%s, %s)", (first, second)
    ).fetchall()
    assert dict(rows) == {first: 3, second: 1}


def test_a_new_bundle_applies_to_the_very_next_propose_and_names_its_version(
    propose: Callable[..., Any], owner: psycopg.Connection[Any], world: Any
) -> None:
    """OPS-02 / GR-14: weakening the policy (human -> auto) acts on the next propose, without a restart; the intent
    and its policy decision name the bundle that decided, so the change is traceable to a commit."""
    before = propose().json()
    assert before["approval"]["mode"] == "human"
    world.policies["t-alpha"] = {
        "tenant": "t-alpha", "schema_version": 1,
        "rules": [{"id": "auto-b", "match": {"tools": ["demo.message.propose"]}, "decision": "auto_approve",
                   "limits": {"max_per_hour": 5}}],
    }  # fmt: skip
    world.reload(version="c" * 40)
    after = propose(args={"text": "b", "n": 2}).json()
    assert after["approval"]["mode"] == "auto" and after["status"] == "approved"
    versions = dict(owner.execute("SELECT intent_id::text, policy_version FROM intents").fetchall())
    assert versions == {before["intent_id"]: "b" * 40, after["intent_id"]: "c" * 40}
    assert owner.execute("SELECT decided_by FROM decisions").fetchall() == [("policy:auto-b@" + "c" * 40,)]
