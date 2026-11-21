"""GET, cancel, dispatch and JWKS of the internal API (AS-06, AS-09, AS-11a, T6, T9); AC-M7-09 at the API."""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from gxg_approval.api import create_app
from gxg_connector_kit import _jwt, exec_token

BOT, OTHER, BETA_BOT = "agent:t-alpha:bot", "agent:t-alpha:other", "agent:t-beta:bot"
SHARED = "agent:shared"  # registered in both tenants
HUMAN = "oidc:https://idp.example.invalid:1"


def q(tenant: str = "t-alpha", principal: str = BOT) -> dict[str, str]:
    return {"tenant_id": tenant, "principal": principal}


def test_get_own_intent(propose: Callable[..., Any], client: TestClient, world: Any) -> None:
    created = propose(business_task_id="ac-42").json()
    r = client.get(f"/v1/intents/{created['intent_id']}", params=q(), headers=world.auth("approval-mcp"))
    assert r.status_code == 200 and r.json() == created and created["business_task_id"] == "ac-42"


def test_status_counts_human_votes(
    propose: Callable[..., Any], client: TestClient, world: Any, pg: Any, owner: psycopg.Connection[Any]
) -> None:
    created = propose().json()
    owner.execute("UPDATE intents SET required_approvals = 2")
    row = owner.execute("SELECT canonical_args_hash FROM intents").fetchone()
    assert row is not None
    with pg.connect("gxg_ui") as ui:
        ui.execute("SELECT gxg_decide(%s, %s, 'approve', 1, %s, false, null)", (created["intent_id"], HUMAN, row[0]))
    doc = client.get(f"/v1/intents/{created['intent_id']}", params=q(), headers=world.auth("approval-mcp")).json()
    assert doc["approval"]["received_approvals"] == 1 and doc["status"] == "pending_approval"
    assert doc["approval"]["required_approvals"] == 2 and doc["version"] == 2


def test_foreign_and_missing_intents_are_the_same_404(
    propose: Callable[..., Any], client: TestClient, world: Any
) -> None:
    """AC-M7-09 at the API: another requester, another tenant, missing, malformed id → identical bytes."""
    mine = propose().json()["intent_id"]
    beta = propose(tenant="t-beta", requester=BETA_BOT, connection="demo-b", resource="demo:demo-b/board/b2")
    bodies = {
        client.get(f"/v1/intents/{intent}", params=params, headers=world.auth("approval-mcp")).content
        for intent, params in (
            (mine, q(principal=OTHER)),
            (mine, q(tenant="t-beta", principal=BETA_BOT)),
            (beta.json()["intent_id"], q()),
            (str(uuid.uuid4()), q()),
            ("not-a-uuid", q()),
        )
    }
    assert len(bodies) == 1
    assert bodies.pop() == b'{"code":"not_found","message":"intent not found","retryable":false}'


def test_same_principal_in_another_tenant_is_not_found(
    propose: Callable[..., Any], client: TestClient, world: Any, owner: psycopg.Connection[Any]
) -> None:
    """AC-M7-09, AS-11a: the tenant is part of the key, not only the principal ID (registered in both tenants)."""
    intent = propose(requester=SHARED).json()["intent_id"]
    beta = q(tenant="t-beta", principal=SHARED)
    missing = client.get(
        f"/v1/intents/{uuid.uuid4()}", params=q(principal=SHARED), headers=world.auth("approval-mcp")
    ).content
    assert missing == b'{"code":"not_found","message":"intent not found","retryable":false}'
    assert client.get(f"/v1/intents/{intent}", params=beta, headers=world.auth("approval-mcp")).content == missing
    r = client.post(
        f"/v1/intents/{intent}/cancel",
        json={"on_behalf_of": beta, "expected_version": 1},
        headers=world.auth("approval-mcp"),
    )
    assert (r.status_code, r.content) == (404, missing)
    assert owner.execute("SELECT status, version FROM intents").fetchall() == [("pending_approval", 1)]
    assert owner.execute("SELECT count(*) FROM audit_events WHERE action = 'cancel'").fetchone() == (0,)
    own = client.get(f"/v1/intents/{intent}", params=q(principal=SHARED), headers=world.auth("approval-mcp"))
    assert own.status_code == 200 and own.json()["status"] == "pending_approval"


def test_pool_exhausted_is_temporarily_unavailable(world: Any, pg: Any, owner: psycopg.Connection[Any]) -> None:
    """A busy pool is a retryable 503, not a 500 (error table: temporarily_unavailable)."""
    with ConnectionPool(pg.dsn("gxg_api"), min_size=1, max_size=1, timeout=0.2, kwargs={"autocommit": True}) as pool:
        with TestClient(create_app(world.deps(pool)), raise_server_exceptions=False) as client, pool.connection():
            r = client.post("/v1/intents", json=world.body(), headers=world.auth())
    assert r.status_code == 503
    assert r.json() == {"code": "temporarily_unavailable", "message": "approval service is busy", "retryable": True}


def test_read_and_cancel_only_for_approval_mcp(propose: Callable[..., Any], client: TestClient, world: Any) -> None:
    intent = propose().json()["intent_id"]
    refused: list[dict[str, str]] = [
        {},
        {"Authorization": "Basic " + world.token("approval-mcp")},  # a valid token, but not as a bearer token
        {"Authorization": "Bearer "},
        world.auth("demo"),
        world.auth("executor"),
        world.auth("approval-mcp", aud="x"),
    ]
    rejected = {"code": "unauthenticated", "message": "token rejected", "retryable": False}
    for headers in refused:
        r = client.get(f"/v1/intents/{intent}", params=q(), headers=headers)
        assert (r.status_code, r.json()) == (401, rejected), headers
        r = client.post(
            f"/v1/intents/{intent}/cancel", headers=headers, json={"on_behalf_of": q(), "expected_version": 1}
        )
        assert (r.status_code, r.json()) == (401, rejected), headers
    missing = client.get(f"/v1/intents/{intent}", params={"tenant_id": "t-alpha"}, headers=world.auth("approval-mcp"))
    assert missing.status_code == 400  # AS-11a: on_behalf_of is mandatory


def test_facade_only_for_its_tenants(propose: Callable[..., Any], client: TestClient, world: Any) -> None:
    """AS-06: an approval_mcp registered for t-beta cannot read or cancel t-alpha intents."""
    intent = propose().json()["intent_id"]
    r = client.get(f"/v1/intents/{intent}", params=q(), headers=world.auth("approval-mcp-beta"))
    assert r.status_code == 404
    r = client.post(
        f"/v1/intents/{intent}/cancel",
        json={"on_behalf_of": q(), "expected_version": 1},
        headers=world.auth("approval-mcp-beta"),
    )
    assert r.status_code == 404
    assert client.get(f"/v1/intents/{intent}", params=q(), headers=world.auth("approval-mcp")).status_code == 200


def test_cancel(propose: Callable[..., Any], client: TestClient, world: Any, owner: psycopg.Connection[Any]) -> None:
    intent = propose().json()["intent_id"]
    url = f"/v1/intents/{intent}/cancel"

    def post(body: Any) -> Any:
        return client.post(url, json=body, headers=world.auth("approval-mcp"))

    assert post({"on_behalf_of": q(principal=OTHER), "expected_version": 1}).status_code == 404
    assert post({"on_behalf_of": q(), "expected_version": 7}).status_code == 409
    for bad in (
        {"on_behalf_of": q()},
        {"on_behalf_of": q(), "expected_version": 0},
        {"expected_version": 1},
        {"on_behalf_of": {**q(), "x": 1}, "expected_version": 1},
    ):
        r = post(bad)
        assert (r.status_code, r.json()["message"]) == (400, "body does not match the API contract"), bad
    r = client.post(url, content=b"{", headers={**world.auth("approval-mcp"), "content-type": "application/json"})
    assert (r.status_code, r.json()["message"]) == (400, "body is not JSON")
    r = post({"on_behalf_of": q(), "expected_version": 1})
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["version"] == 2
    bad_id = client.post(
        "/v1/intents/not-a-uuid/cancel",
        json={"on_behalf_of": q(), "expected_version": 1},
        headers=world.auth("approval-mcp"),
    )
    assert bad_id.status_code == 404 and bad_id.json()["message"] == "intent not found"
    again = post({"on_behalf_of": q(), "expected_version": 2})
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    audit = owner.execute("SELECT actor, from_status, to_status FROM audit_events WHERE action = 'cancel'").fetchall()
    assert audit == [(BOT, "pending_approval", "cancelled")]


def exec_claims(intent: dict[str, Any], execution: str, aud: str = "demo", **over: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": "gxg-executor",
        "aud": aud,
        "sub": intent["intent_id"],
        "jti": execution,
        "ten": "t-alpha",
        "req": BOT,
        "tool": "demo.message.propose@1.0.0",
        "con": "demo-main",
        "res": "demo:demo-main/board/b1",
        "ahs": intent["canonical_args_hash"],
        "idk": intent["idempotency_key"],
        "iat": now,
        "exp": now + 60,
    }
    return {**claims, **over}


def test_dispatch(
    world: Any,
    api_pool: ConnectionPool[Any],
    pg: Any,
    owner: psycopg.Connection[Any],
    make_jwks: Callable[..., dict[str, Any]],
) -> None:
    """T9 over HTTP: connector JWT = token aud, jti = execution, only once; JWKS from configuration (AS-09)."""
    executor_key = Ed25519PrivateKey.generate()
    deps = world.deps(api_pool)
    published = make_jwks(executor_key)
    deps.exec_jwks = lambda: published
    with TestClient(create_app(deps)) as client:
        assert client.get("/.well-known/gxg-exec-jwks.json").json() == published
        created = client.post("/v1/intents", json=world.body(tool="demo.note.propose"), headers=world.auth()).json()
        cur = owner.cursor(row_factory=psycopg.rows.dict_row)
        intent = cur.execute("SELECT * FROM intents WHERE intent_id = %s", (created["intent_id"],)).fetchone()
        assert intent is not None
        intent = {**intent, "intent_id": str(intent["intent_id"])}
        with pg.connect("gxg_executor") as ex:
            row = ex.execute("SELECT gxg_reserve(%s, 'executor-1')", (intent["intent_id"],)).fetchone()
        execution = str(row[0])
        kid = _jwt.thumbprint(executor_key.public_key())

        def token(**over: Any) -> str:
            return _jwt.sign(executor_key, exec_claims(intent, execution, **over), typ=exec_token.TYP, kid=kid)

        url = f"/v1/executions/{execution}/dispatch"
        refused = [
            (world.auth("rogue"), token(), 401),  # aud = demo, caller rogue
            (world.auth("demo"), token(aud="rogue"), 401),
            (world.auth("demo"), token(jti=str(uuid.uuid4())), 401),
            (
                world.auth("demo"),
                _jwt.sign(Ed25519PrivateKey.generate(), exec_claims(intent, execution), typ=exec_token.TYP, kid=kid),
                401,
            ),
            (world.auth("executor"), token(), 401),
        ]
        for headers, tok, code in refused:
            assert client.post(url, json={"execution_token": tok}, headers=headers).status_code == code
        world.demo_tenants = ["t-beta"]  # the connector is no longer registered for t-alpha (AS-25)
        world.reload(version="c" * 40)
        with TestClient(create_app(dataclasses.replace(deps, _exec_verifiers={}))) as other:
            r = other.post(url, json={"execution_token": token()}, headers=world.auth())
        assert r.status_code == 404
        world.demo_tenants = ["t-alpha", "t-beta"]
        world.reload(version="d" * 40)
        r = client.post(url, json={"execution_token": token()}, headers=world.auth())
        assert r.status_code == 200, r.text
        assert r.json()["execution_id"] == execution
        deadline = datetime.fromisoformat(r.json()["dispatch_deadline"])
        assert r.json()["dispatch_deadline"].endswith("Z")
        assert 55 < (deadline - datetime.now(UTC)).total_seconds() <= 60  # now + dispatch_timeout_s
        lease = owner.execute(
            "SELECT round(extract(epoch FROM dispatch_lease_until - dispatched_at)) FROM executions"
        ).fetchone()
        assert lease == (90,)  # dispatch_timeout_s 60 by default (no value in the bundle) + 30 s
        r = client.post(url, json={"execution_token": token()}, headers=world.auth())
        assert r.status_code == 401  # the same jti again: the local jti cache (EFF-20, extra layer)
        with TestClient(create_app(dataclasses.replace(deps, _exec_verifiers={}))) as second:
            r = second.post(url, json={"execution_token": token()}, headers=world.auth())
        assert r.status_code == 409  # another api instance, no shared cache: the CAS in gxg_dispatch (EFF-20)
        r = client.post(
            f"/v1/executions/{uuid.uuid4()}/dispatch", json={"execution_token": token()}, headers=world.auth()
        )
        assert r.status_code == 401


def test_error_headers_and_unexpected_errors(client: TestClient, world: Any, api_pool: ConnectionPool[Any]) -> None:
    """Retry-After only with retry_after_s; an unexpected exception is a 500 internal_error, no details."""
    r = client.get(f"/v1/intents/{uuid.uuid4()}", params=q(), headers=world.auth("approval-mcp"))
    assert r.status_code == 404 and "retry-after" not in r.headers
    deps = dataclasses.replace(world.deps(api_pool), pool=None)  # every database call raises AttributeError
    with TestClient(create_app(deps), raise_server_exceptions=False) as broken:
        r = broken.get(f"/v1/intents/{uuid.uuid4()}", params=q(), headers=world.auth("approval-mcp"))
    assert (r.status_code, r.json()) == (
        500,
        {"code": "internal_error", "message": "internal error", "retryable": False},
    )


def test_health(client: TestClient, world: Any) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ready": True, "reasons": []}


def test_not_ready_without_bundle(api_pool: ConnectionPool[Any], world: Any, tmp_path: Any) -> None:
    from gxg_connector_kit.grants import BundleStore

    deps = world.deps(api_pool)
    deps.store = BundleStore({}, state_file=tmp_path / "s.json")
    with TestClient(create_app(deps)) as client:
        assert client.get("/readyz").status_code == 503
        r = client.post("/v1/intents", json=world.body(), headers=world.auth())
        assert r.status_code == 503 and r.json()["code"] == "temporarily_unavailable"


def test_dispatch_of_a_tool_no_longer_in_the_bundle(world: Any, api_pool: ConnectionPool[Any]) -> None:
    """AS-26: the dispatch timeout comes from the bundle; a tool removed from it cannot be dispatched."""
    import pytest

    from gxg_approval import intents
    from gxg_connector_kit.errors import GxgError

    with api_pool.connection() as conn:
        connector = dataclasses.replace(world.store.registry()["demo"], tools={})
        with pytest.raises(GxgError) as err:
            intents.dispatch(conn, uuid.uuid4(), uuid.uuid4(), connector)
    assert (err.value.code, err.value.message) == ("not_found", "execution not found")


def test_dispatch_timeout_comes_from_the_bundle() -> None:
    from gxg_approval.intents import dispatch_timeout_s

    assert dispatch_timeout_s({"dispatch_timeout_s": 30}) == 30
    assert dispatch_timeout_s({}) == 60
