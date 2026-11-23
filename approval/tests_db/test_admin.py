"""AC-M10-04 (EX-13, AS-24): `gxg-approval switch set --scope global` → the next dispatch of any tenant is refused
(423) inside its own transaction; the change is audited and counted for the alert. Admin auth: operator OIDC token,
platform_admin, step-up for `clear`. Passkey invites (AS-30)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from gxg_approval import admin, cli
from gxg_approval.decider import invite_hash

ISSUER, AUDIENCE = "https://idp.example.invalid", "gxg-admin"
OPERATOR = f"oidc:{ISSUER}:op-1"
KEY = ec.generate_private_key(ec.SECP256R1())


def token(sub: str = "op-1", *, key: Any = KEY, aud: str = AUDIENCE, iss: str = ISSUER, **claims: Any) -> str:
    now = int(time.time())
    doc = {"iss": iss, "aud": aud, "sub": sub, "iat": now, "exp": now + 300} | claims
    return jwt.encode(doc, key, algorithm="ES256")


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def admin_client(pg: Any, owner: psycopg.Connection[Any]) -> Iterator[TestClient]:
    with ConnectionPool(pg.dsn("gxg_admin"), min_size=1, max_size=2, kwargs={"autocommit": True}) as pool:
        deps = admin.Deps(pool, ISSUER, AUDIENCE, frozenset({OPERATOR}), signing_key=lambda _t: KEY.public_key())
        with TestClient(admin.create_app(deps), raise_server_exceptions=False) as client:
            yield client


def reserved(pg: Any, propose: Callable[..., Any], **kw: Any) -> tuple[str, str]:
    intent = propose(tool="demo.note.propose", **kw).json()["intent_id"]  # write_reversible: approved at T1
    with pg.connect("gxg_executor") as ex:
        row = ex.execute("SELECT gxg_reserve(%s, 'x')", (intent,)).fetchone()
    assert row is not None
    return intent, str(row[0])


def test_global_switch_blocks_the_next_dispatch_of_every_tenant(
    admin_client: TestClient, pg: Any, propose: Callable[..., Any], owner: psycopg.Connection[Any]
) -> None:
    alpha = reserved(pg, propose)
    beta = reserved(pg, propose, tenant="t-beta", requester="agent:t-beta:bot", connection="demo-b",
                    resource="demo:demo-b/board/b1")  # fmt: skip
    r = admin_client.post("/v1/switches", json={"scope": "global", "blocked": True, "reason": "drill"},
                          headers=auth(token()))  # fmt: skip
    assert r.status_code == 200, r.text
    with pg.connect("gxg_api") as api:
        for intent, execution in (alpha, beta):
            with pytest.raises(psycopg.Error) as err:
                api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (execution, intent))
            assert err.value.sqlstate == "GX423"  # AS-05: read inside T9, no cache
    assert owner.execute("SELECT state FROM executions ORDER BY reserved_at").fetchall() == [("reserved",)] * 2
    audit = owner.execute("SELECT actor, action, details FROM audit_events WHERE action LIKE 'admin%'").fetchall()
    assert audit == [(OPERATOR, "admin_switch_set", {"scope": "global", "reason": "drill"})]
    with pg.connect("gxg_api") as api:
        m = api.execute("SELECT name, labels, value FROM gxg_metrics() WHERE name IN "
                        "('gxg_switch_blocked', 'gxg_admin_actions_total')").fetchall()  # fmt: skip
    assert ("gxg_switch_blocked", {"scope": "global"}, 1.0) in m
    assert ("gxg_admin_actions_total", {"action": "admin_switch_set"}, 1.0) in m
    listed = admin_client.get("/v1/switches", headers=auth(token())).json()
    assert [(s["scope"], s["blocked"], s["changed_by"]) for s in listed] == [("global", True, OPERATOR)]


def test_clear_needs_a_fresh_login(admin_client: TestClient, owner: psycopg.Connection[Any]) -> None:
    body = {"scope": "tenant:t-alpha", "blocked": True, "reason": "incident"}
    assert admin_client.post("/v1/switches", json=body, headers=auth(token())).status_code == 200
    clear = body | {"blocked": False, "reason": "resolved"}
    for t in (token(), token(auth_time=int(time.time()) - 301), token(auth_time="yesterday")):
        assert admin_client.post("/v1/switches", json=clear, headers=auth(t)).status_code == 403
    fresh = token(auth_time=int(time.time()) - 60)
    assert admin_client.post("/v1/switches", json=clear, headers=auth(fresh)).status_code == 200
    assert owner.execute("SELECT blocked, reason FROM switches").fetchall() == [(False, "resolved")]
    actions = owner.execute("SELECT action FROM audit_events WHERE action LIKE 'admin%' ORDER BY id").fetchall()
    assert actions == [("admin_switch_set",), ("admin_switch_clear",)]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Basic x"}, 401),
        (auth("not-a-jwt"), 401),
        (auth(token(key=ec.generate_private_key(ec.SECP256R1()))), 401),  # another key
        (auth(token(aud="gxg-ui")), 401),
        (auth(token(iss="https://evil.example")), 401),
        (auth(token(exp=int(time.time()) - 60)), 401),
        (auth(token(sub="someone-else")), 403),  # valid, but not platform_admin
    ],
)
def test_operator_token_is_checked(
    admin_client: TestClient, owner: psycopg.Connection[Any], headers: dict[str, str], status: int
) -> None:
    body = {"scope": "global", "blocked": True, "reason": "x" * 10}
    assert admin_client.post("/v1/switches", json=body, headers=headers).status_code == status
    assert admin_client.get("/v1/switches", headers=headers).status_code == status
    assert admin_client.post("/v1/passkey-invites", json={"human": OPERATOR}, headers=headers).status_code == status
    assert owner.execute("SELECT count(*) FROM switches").fetchone() == (0,)


@pytest.mark.parametrize("scope", ["everything", "tenant:", "tool:t-alpha", "global; DROP", "tenant:T-ALPHA"])
def test_bad_scope_or_reason(admin_client: TestClient, scope: str) -> None:
    r = admin_client.post("/v1/switches", json={"scope": scope, "blocked": True, "reason": "x" * 10},
                          headers=auth(token()))  # fmt: skip
    assert r.status_code == 400
    r = admin_client.post("/v1/switches", json={"scope": "global", "blocked": True, "reason": "x"},
                          headers=auth(token()))  # fmt: skip
    assert r.status_code == 400


def test_tool_scope(admin_client: TestClient, pg: Any, propose: Callable[..., Any]) -> None:
    """EX-13 by tool: only that tool of that tenant stops."""
    blocked = reserved(pg, propose)
    body = {"scope": "tool:t-alpha:demo.note.propose", "blocked": True, "reason": "bad tool"}
    assert admin_client.post("/v1/switches", json=body, headers=auth(token())).status_code == 200
    with pg.connect("gxg_api") as api, pytest.raises(psycopg.Error) as err:
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (blocked[1], blocked[0]))
    assert err.value.sqlstate == "GX423"


def test_passkey_invite(admin_client: TestClient, owner: psycopg.Connection[Any], pg: Any) -> None:
    human = "oidc:https://idp.example.invalid:1"
    r = admin_client.post("/v1/passkey-invites", json={"human": human}, headers=auth(token()))
    assert r.status_code == 200 and r.json()["expires_in_s"] == 86400
    invite = r.json()["invite"]
    rows = owner.execute("SELECT token_hash, human_principal, created_by FROM passkey_invites").fetchall()
    assert rows == [(invite_hash(invite), human, OPERATOR)]  # the token itself is never stored
    with pg.connect("gxg_decider") as decider:
        assert decider.execute("SELECT gxg_enroll_challenge(%s, %s)", (human, invite_hash(invite))).fetchone()
    assert admin_client.post("/v1/passkey-invites", json={"human": "agent:x"}, headers=auth(token())).status_code == 400


def test_cli_switch_and_invite_call_the_admin_api(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "token").write_text("operator-token\n")
    monkeypatch.setenv("GXG_OPERATOR_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("GXG_ADMIN_URL", "https://admin.internal:8092/")
    sent: list[tuple[str, Any, Any]] = []

    class Reply:
        status_code, text = 200, '{"ok": true}'

    def post(url: str, *, json: Any, headers: Any, timeout: float) -> Reply:
        sent.append((url, json, headers))
        return Reply()

    monkeypatch.setattr("httpx.post", post)
    cli.main(["switch", "set", "--scope", "global", "--reason", "drill"])
    cli.main(["switch", "clear", "--scope", "global", "--reason", "done"])
    cli.main(["passkeys", "invite", "--human", "oidc:x:1"])
    bearer = {"Authorization": "Bearer operator-token"}
    assert sent == [
        ("https://admin.internal:8092/v1/switches", {"scope": "global", "blocked": True, "reason": "drill"}, bearer),
        ("https://admin.internal:8092/v1/switches", {"scope": "global", "blocked": False, "reason": "done"}, bearer),
        ("https://admin.internal:8092/v1/passkey-invites", {"human": "oidc:x:1"}, bearer),
    ]
    with pytest.raises(SystemExit, match="switch set"):
        cli.main(["switch", "set", "--scope", "global"])  # no reason
    Reply.status_code = 403
    with pytest.raises(SystemExit, match="HTTP 403"):
        cli.main(["switch", "set", "--scope", "global", "--reason", "drill"])


def test_admin_refuses_a_plain_http_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Critic M10-1 #7: discovery and JWKS decide who is an operator — https only (loopback for tests)."""
    monkeypatch.setenv("GXG_ADMIN_OIDC_ISSUER", "http://idp.example.com")
    with pytest.raises(SystemExit, match="GXG_ADMIN_OIDC_ISSUER must be https"):
        cli.main(["admin"])
    cli.require_https("https://idp.example.com", "x")
    cli.require_https("http://127.0.0.1:9400", "x")


def test_executor_metrics_registry_has_reserve_conflicts() -> None:
    """Critic M10-1 #4: the executor's port exposes gxg_reserve_conflicts_total (and not the database figures)."""
    from prometheus_client import generate_latest

    from gxg_approval.executor import RESERVE_CONFLICTS

    RESERVE_CONFLICTS.inc(0)
    text = generate_latest(cli.executor_metrics()).decode()
    assert "gxg_reserve_conflicts_total" in text and "gxg_intents_pending" not in text


def test_refusal_texts_and_db_messages(admin_client: TestClient) -> None:
    """Each refusal says why (the operator reads it in the CLI); a refusal of gxg_switch carries the database's text."""
    body = {"scope": "global", "blocked": True, "reason": "drill"}
    cases = [
        ({}, 401, "operator token required"),
        (auth("x.y.z"), 401, "operator token refused"),
        (auth(token(sub="other")), 403, "platform_admin required"),
    ]
    for headers, status, message in cases:
        r = admin_client.post("/v1/switches", json=body, headers=headers)
        assert (r.status_code, r.json()) == (status, {"message": message})
    r = admin_client.post("/v1/switches", json=body | {"blocked": False}, headers=auth(token()))
    assert r.json() == {"message": "a fresh login (auth_time ≤ 5 min) is required to clear a switch"}
    r = admin_client.post("/v1/switches", json=body | {"scope": "bad scope"}, headers=auth(token()))
    assert r.status_code == 400 and r.json()["message"].startswith("scope global|tenant:<id>|tool:<tenant>:<tool>")
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert admin_client.get(path).status_code == 404


def test_required_claims_and_leeway(admin_client: TestClient) -> None:
    """iat and sub are required (not only exp); 5 s of clock skew (AS-11), not more."""
    no_iat = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "op-1", "exp": int(time.time()) + 60}, KEY, "ES256")
    assert admin_client.get("/v1/switches", headers=auth(no_iat)).status_code == 401
    now = int(time.time())
    late = token(exp=now - 5, iat=now - 60)  # 5 s plus the fraction of this second ago: past the leeway
    assert admin_client.get("/v1/switches", headers=auth(late)).status_code == 401
    assert admin_client.get("/v1/switches", headers=auth(token(exp=now + 2))).status_code == 200


def test_pyjwk_signing_key_and_auth_time_boundary(pg: Any, owner: psycopg.Connection[Any]) -> None:
    """Production passes PyJWKClient's PyJWK (its `.key` is used); the step-up limit is ≤ 300 s exactly."""
    from jwt.algorithms import ECAlgorithm

    jwk = jwt.PyJWK(json.loads(ECAlgorithm.to_jwk(KEY.public_key())), algorithm="ES256")
    seen: list[str] = []
    clock = [1_900_000_000.0]

    def signing_key(t: str) -> Any:
        seen.append(t)
        return jwk

    with ConnectionPool(pg.dsn("gxg_admin"), min_size=1, max_size=1, kwargs={"autocommit": True}) as pool:
        deps = admin.Deps(
            pool, ISSUER, AUDIENCE, frozenset({OPERATOR}), signing_key=signing_key, clock=lambda: clock[0]
        )
        with TestClient(admin.create_app(deps)) as client:
            t = token(auth_time=int(clock[0]) - 300)
            body = {"scope": "global", "blocked": False, "reason": "boundary"}
            assert client.post("/v1/switches", json=body, headers=auth(t)).status_code == 200
            assert seen == [t]
            clock[0] += 1
            assert client.post("/v1/switches", json=body, headers=auth(t)).status_code == 403
