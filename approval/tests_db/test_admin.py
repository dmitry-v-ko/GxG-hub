"""AC-M10-04 (EX-13, AS-24): `gxg-approval switch set --scope global` → the next dispatch of any tenant is refused
(423) inside its own transaction; the change is audited and counted for the alert. Admin auth: operator OIDC token,
platform_admin, step-up for `clear`. Passkey invites (AS-30)."""

from __future__ import annotations

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
