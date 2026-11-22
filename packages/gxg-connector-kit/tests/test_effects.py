"""gxg_connector_kit.effects (M1.1): propose -> intent, internal execute/reconcile in EFF-18 order, connect().

The Approval Service is the conformance suite's fake (gxg_connector_check.fakes); tokens are signed here.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_check import bundle
from gxg_connector_check.fakes import FakeApproval
from gxg_connector_kit import effects, exec_token, mcp, svc_jwt
from gxg_connector_kit._jwt import b64url_encode
from gxg_connector_kit.effects import Dropped, Sent
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.exec_token import ExecutionClaims, ExecutorJwks
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.identity import VerifiedIdentity
from gxg_connector_kit.mcp import Access
from gxg_connector_kit.svc_jwt import parse_registry

pytestmark = pytest.mark.anyio
ACTOR = "agent:t-alpha:actor"
RES = "kit:kit-main/box/b1"
GRANTS: dict[str, Any] = {
    "tenant": "t-alpha",
    "schema_version": 1,
    "connections": [{"id": "kit-main", "domain": "kit", "credential_ref": "secret://t-alpha/kit/main"}],
    "principals": {"agents": [{"id": ACTOR}]},
    "grants": [{"id": "g", "subjects": [ACTOR], "actions": ["read", "propose"], "resources": [RES]}],
}
SPEC = {
    "name": "kit.msg.propose",
    "version": "1.2.0",
    "effect_class": "external_effect",
    "action": "propose",
    "resource": {"type": "box"},
    "executes_via": "kit.msg.execute",
    "input_schema": "x",
    "output_schema": "y",
    "preview": {"title": "Send", "fields": {"to": "Recipient", "text": "Text"}},
}
EXECUTE = "/internal/v1/execute/kit.msg.execute"
RECONCILE = "/internal/v1/reconcile/kit.msg.execute"


@dataclass
class Provider:
    mode: str = "ok"
    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(self, claims: ExecutionClaims, args: dict[str, Any]) -> Sent:
        self.sent.append((claims["sub"], args))
        if self.mode == "drop":
            raise Dropped
        if self.mode == "crash":
            raise RuntimeError("boom")
        if self.mode == "slow":
            import asyncio

            await asyncio.sleep(0.3)
        if self.mode == "ok_without_receipt":
            return Sent("ok", None)
        mode = "ok" if self.mode == "slow" else self.mode
        return Sent(mode, "op-1" if mode in ("ok", "duplicate") else None)  # type: ignore[arg-type]

    def lookup(self, intent_id: str) -> tuple[bool, str | None]:
        return (True, "op-1") if any(i == intent_id for i, _ in self.sent) else (False, None)


@dataclass
class Rig:
    tmp: Path
    bundle_signer: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    executor: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    svc: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    provider: Provider = field(default_factory=Provider)
    served: bool = True

    def __post_init__(self) -> None:
        manifest = {"connector": "kit", "tools": [SPEC]}
        self.platform = bundle.registry(
            manifest,
            ["t-alpha"],
            executor=self.executor.public_key(),
            svc=self.svc.public_key(),
            approval=Ed25519PrivateKey.generate().public_key(),
        )
        self.write([GRANTS])
        self.store = BundleStore(
            mcp.parse_keys(bundle.keys_setting(self.bundle_signer), "k"), state_file=self.tmp / "s.json"
        )
        self.files = mcp.BundleFiles(self.store, self.tmp / "bundle")
        self.files.refresh()
        self.fake = FakeApproval(lambda: parse_registry(self.platform), bundle.jwks(self.executor.public_key()), "kit")
        http = httpx2.AsyncClient(base_url="http://a", transport=self.fake.transport())
        self.approval = effects.Approval(http, self.svc, connector_id="kit", audience="approval-api")
        self.jwks = ExecutorJwks(lambda: bundle.jwks(self.executor.public_key()))
        self.internal = effects.Internal(
            connector_id="kit",
            tools=[SPEC, {"name": "kit.box.get"}],
            grants=self.store,
            approval=self.approval,
            jwks=self.jwks,
            provider=self.provider,
            serves=lambda t, c, r: self.served and (t, c, r) == ("t-alpha", "kit-main", RES),
            refresh=self.files.refresh,
            logs=mcp.Logs.of("kit_fx"),
        )

    def write(self, grants: list[dict[str, Any]], at: datetime | None = None) -> None:
        bundle.write(
            self.tmp / "bundle",
            grants,
            self.platform,
            self.bundle_signer,
            built_at=at or datetime.now(UTC) - timedelta(minutes=2),
        )

    def access(self, rid: str | None = RES, connection: str | None = "kit-main") -> Access:
        now = datetime.now(UTC)
        ident = VerifiedIdentity("t-alpha", ACTOR, now, now, "req-1", None, "signed_assertion")
        return Access(ident, "propose", connection, rid, "g", "b1")

    async def propose(self, args: dict[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        content, fields = await effects.propose(
            self.approval,
            SPEC,
            self.access(),
            args or {"to": "bob", "text": "hi"},
            target_label="Box 1",
            kinds={"to": "identifier"},
            idempotency_key=key,
        )
        assert fields == {"intent_id": content["intent_id"]}
        return content

    def token(self, intent: str, body: dict[str, Any], **change: Any) -> tuple[str, str]:
        key = change.pop("key", self.executor)
        now = change.pop("now", time.time)
        execution = change.pop("execution", str(uuid.uuid4()))
        claims = {
            "aud": "kit",
            "sub": intent,
            "jti": execution,
            "ten": "t-alpha",
            "req": ACTOR,
            "tool": f"{body['tool_key']}@{body['tool_version']}",
            "con": body["connection_id"],
            "res": body["resource_id"],
            "ahs": body["canonical_args_hash"],
            "idk": body["idempotency_key"],
            **change,
        }
        return exec_token.sign(key, claims, now=now), execution

    async def post(self, path: str, payload: Any, auth: str | None = "executor") -> httpx2.Response:
        headers = {}
        if auth == "executor":
            headers["authorization"] = f"Bearer {svc_jwt.sign(self.executor, iss='executor', aud='kit')}"
        elif auth:
            headers["authorization"] = auth
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.internal.app()), base_url="http://i"
        ) as h:
            return await h.post(path, content=content, headers=headers)

    async def execute(self, **kw: Any) -> tuple[httpx2.Response, str, dict[str, Any]]:
        [(intent, body)] = self.fake.intents.items()
        tok, execution = kw.pop("token", None) or self.token(intent, body)
        payload = {
            "intent_id": intent,
            "execution_id": execution,
            "execution_token": tok,
            "args": kw.pop("args", body["canonical_args"]),
        }
        return await self.post(EXECUTE, payload, **kw), execution, body


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


async def test_propose_builds_the_intent(rig: Rig) -> None:
    got = await rig.propose({"to": "bob", "text": "Café"}, key="k-12345678")
    assert got["status"] == "pending_approval"
    body = rig.fake.intents[got["intent_id"]]
    args = {"to": "bob", "text": "Café"}  # NFC (EFF-05)
    assert body == {
        "tool_key": "kit.msg.propose",
        "tool_version": "1.2.0",
        "connection_id": "kit-main",
        "resource_id": RES,
        "canonical_args": args,
        "canonical_args_hash": effects.args_hash("kit.msg.propose", "1.2.0", "t-alpha", "kit-main", RES, args),
        "idempotency_key": "k-12345678",
        "preview": {
            "schema_version": 1,
            "title": "Send",
            "target": {"label": "Box 1", "rid": RES},
            "fields": [
                {"label": "Recipient", "value": "bob", "kind": "identifier"},
                {"label": "Text", "value": "Café", "kind": "text"},
            ],
        },
        "requester": {"principal": ACTOR, "tenant_id": "t-alpha"},
    }
    again = await rig.propose({"to": "bob", "text": "Café"}, key="k-12345678")
    assert again["intent_id"] == got["intent_id"]  # the agent's key: deduplicated by the Approval Service
    own = [await rig.propose() for _ in range(2)]
    keys = [rig.fake.intents[o["intent_id"]]["idempotency_key"] for o in own]
    assert keys[0] != keys[1] and all(k.startswith("kit:") and len(k) == 40 for k in keys)


def test_args_hash_is_the_eff05_document() -> None:
    from gxg_connector_kit import canonical

    doc = {"tool_key": "t", "tool_version": "1", "tenant_id": "ten", "connection_id": "c", "resource_id": "r",
           "args": {"a": 1}}  # fmt: skip
    assert effects.args_hash("t", "1", "ten", "c", "r", {"a": 1}) == canonical.hash(doc)


def test_args_hash_covers_every_part() -> None:
    base = ("t", "1.0.0", "ten", "con", "res", {"a": 1})
    h = effects.args_hash(*base)
    for i in range(6):
        changed = list(base)
        changed[i] = {"a": 2} if i == 5 else f"{base[i]}x"
        assert effects.args_hash(*changed) != h  # type: ignore[arg-type]


async def test_propose_refusals(rig: Rig) -> None:
    with pytest.raises(RuntimeError, match=r"^propose needs `resource\.scope: single`$"):
        await effects.propose(
            rig.approval, SPEC, rig.access(rid=None), {}, target_label="", kinds={}, idempotency_key=None
        )
    with pytest.raises(RuntimeError, match=r"^propose needs `resource\.scope: single`$"):
        await effects.propose(
            rig.approval, SPEC, rig.access(connection=None), {}, target_label="", kinds={}, idempotency_key=None
        )
    rig.fake.down = True
    with pytest.raises(GxgError) as exc:
        await rig.propose()
    assert (exc.value.code, exc.value.message) == ("temporarily_unavailable", "approval service is not reachable")
    rig.fake.down = False
    await rig.propose(key="same-key-1")
    with pytest.raises(GxgError) as exc:
        await rig.propose({"to": "eve", "text": "x"}, key="same-key-1")
    assert (exc.value.code, exc.value.message) == ("conflict", "idempotency key reused")


@pytest.mark.parametrize(
    ("status", "doc", "expected"),
    [
        (409, {"code": "conflict", "message": "m", "retry_after_s": 5}, ("conflict", "m", None)),
        (429, {"code": "rate_limited", "message": "m", "retry_after_s": 7}, ("rate_limited", "m", 7)),
        (
            429,
            {"code": "rate_limited", "message": "m"},
            ("temporarily_unavailable", "approval service did not answer", None),
        ),
        (403, {"code": "permission_denied", "message": "m"}, ("permission_denied", "m", None)),
        (404, {"code": "not_found", "message": "m"}, ("not_found", "m", None)),
        (400, {"code": "invalid_input", "message": "m"}, ("invalid_input", "m", None)),
        (423, {"code": "effect_disabled", "message": "m"}, ("effect_disabled", "m", None)),
        (
            401,
            {"code": "unauthenticated", "message": "m"},
            ("temporarily_unavailable", "approval service did not answer", None),
        ),
        (500, "<html>", ("temporarily_unavailable", "approval service did not answer", None)),
        (201, {"intent_id": "i"}, ("temporarily_unavailable", "approval service did not answer", None)),
        (
            202,
            {"intent_id": "i", "status": "pending_approval"},
            ("temporarily_unavailable", "approval service did not answer", None),
        ),
    ],
)
async def test_approval_answers(rig: Rig, status: int, doc: Any, expected: tuple[str, str, int | None]) -> None:
    body = doc if isinstance(doc, str) else json.dumps(doc)
    rig.approval.http = httpx2.AsyncClient(
        base_url="http://a", transport=httpx2.MockTransport(lambda r: httpx2.Response(status, content=body.encode()))
    )
    with pytest.raises(GxgError) as exc:
        await rig.propose()
    assert (exc.value.code, exc.value.message, exc.value.retry_after_s) == expected


async def test_approval_ok_answers(rig: Rig) -> None:
    for status in (200, 201):
        doc = {"intent_id": "i-1", "status": "approved", "extra": 1}
        rig.approval.http = httpx2.AsyncClient(
            base_url="http://a", transport=httpx2.MockTransport(lambda r, s=status, d=doc: httpx2.Response(s, json=d))
        )
        assert await rig.propose() == {"intent_id": "i-1", "status": "approved"}


async def test_execute_once_and_reconcile(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    await rig.propose()
    r, execution, body = await rig.execute()
    assert r.status_code == 200 and r.json() == {
        "operation_id": "op-1",
        "status": "succeeded",
        "effect_ref": None,
        "reconciliation_required": False,
        "provider_receipt_ref": "op-1",
    }
    [(intent, sent)] = rig.provider.sent
    assert sent == body["canonical_args"] and rig.fake.dispatched == [execution]
    rec = await rig.post(RECONCILE, {"intent_id": intent, "execution_id": execution, "idempotency_key": "k" * 8})
    assert rec.json() == {
        "intent_id": intent,
        "execution_id": execution,
        "finding": "executed",
        "provider_operation_id": "op-1",
    }
    other = str(uuid.uuid4())
    rec = await rig.post(RECONCILE, {"intent_id": other, "execution_id": execution, "idempotency_key": "k" * 8})
    assert rec.json()["finding"] == "not_executed" and rec.json()["provider_operation_id"] is None
    bad = await rig.post(RECONCILE, {"intent_id": "x", "execution_id": execution, "idempotency_key": "k" * 8})
    assert bad.status_code == 422 and bad.json()["message"] == "body does not match the reconcile request"
    lines = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kit_fx.audit"]
    assert [(e["op"], e["outcome"]) for e in lines] == [
        ("execute", "succeeded"),
        ("reconcile", "executed"),
        ("reconcile", "not_executed"),
        ("reconcile", "invalid_input"),
    ]
    assert lines[0]["intent_id"] == intent and lines[0]["execution_id"] == execution and lines[0]["tenant"] == "t-alpha"
    assert lines[0]["tool"] == "kit.msg.execute" and "args" not in json.dumps(lines)


async def test_replay_of_a_dispatched_token_is_conflict(rig: Rig) -> None:
    await rig.propose()
    [(intent, body)] = rig.fake.intents.items()
    tok = rig.token(intent, body)
    assert (await rig.execute(token=tok))[0].status_code == 200
    again = (await rig.execute(token=tok))[0]
    assert again.status_code == 409 and again.json()["message"] == "execution token already used"
    undispatched = rig.token(intent, body)
    rig.fake.dispatch_status = 409
    assert (await rig.execute(token=undispatched))[0].status_code == 409
    replay = (await rig.execute(token=undispatched))[0]  # never dispatched: an ordinary replay
    assert replay.status_code == 401 and len(rig.provider.sent) == 1


@pytest.mark.parametrize(
    ("mode", "status", "required", "receipt"),
    [
        ("drop", "outcome_unknown", True, None),
        ("crash", "outcome_unknown", True, None),
        ("duplicate", "succeeded", False, "op-1"),
        ("reject", "failed_before_effect", False, None),
        ("rate_limited", "failed_before_effect", False, None),
        ("timeout_before_start", "failed_before_effect", False, None),
    ],
)
async def test_outcomes_after_dispatch(rig: Rig, mode: str, status: str, required: bool, receipt: str | None) -> None:
    rig.provider.mode = mode
    await rig.propose()
    r, execution, _ = await rig.execute()
    assert r.status_code == 200
    assert r.json() == {
        "operation_id": receipt or execution,
        "status": status,
        "effect_ref": None,
        "reconciliation_required": required,
        "provider_receipt_ref": receipt,
    }


async def test_refusals_before_dispatch(rig: Rig) -> None:
    await rig.propose()
    [(intent, body)] = rig.fake.intents.items()
    other = Ed25519PrivateKey.generate()

    def retarget(tool: str, version: str) -> tuple[str, str]:  # another tool, with a consistent hash
        ahs = effects.args_hash(tool, version, "t-alpha", "kit-main", RES, body["canonical_args"])
        return rig.token(intent, body, tool=f"{tool}@{version}", ahs=ahs)

    cases: list[tuple[dict[str, Any], int, str]] = [
        ({"auth": None}, 401, "token rejected"),
        ({"auth": "Basic abc"}, 401, "token rejected"),
        ({"auth": "Bearer "}, 401, "token rejected"),
        ({"auth": f"Bearer {svc_jwt.sign(rig.svc, iss='kit', aud='kit')}"}, 401, "token rejected"),
        ({"auth": f"Bearer {svc_jwt.sign(other, iss='executor', aud='kit')}"}, 401, "token rejected"),
        ({"token": rig.token(intent, body, aud="other")}, 401, "token rejected"),
        ({"token": rig.token(intent, body, key=other)}, 401, "token rejected"),
        ({"token": rig.token(intent, body, now=lambda: time.time() - 600)}, 401, "token rejected"),
        ({"token": rig.token(str(uuid.uuid4()), body)}, 401, "token rejected"),  # bound to another intent
        ({"token": (rig.token(intent, body)[0], str(uuid.uuid4()))}, 401, "token rejected"),  # another execution
        ({"args": {**body["canonical_args"], "to": "eve"}}, 422, "args do not match the approved hash"),
        ({"args": {**body["canonical_args"], "n": 1.5}}, 422, "args do not match the approved hash"),
        ({"token": retarget("kit.msg.propose", "9.0.0")}, 403, "execution target is not served by this connector"),
        ({"token": retarget("kit.other.propose", "1.2.0")}, 403, "execution target is not served by this connector"),
    ]
    for kw, status, message in cases:
        r = (await rig.execute(**kw))[0]
        assert (r.status_code, r.json()["message"]) == (status, message), kw
    rig.served = False
    r = (await rig.execute())[0]
    assert (r.status_code, r.json()["message"]) == (403, "execution target is not served by this connector")
    assert not rig.provider.sent and not rig.fake.dispatched


async def test_body_shape_is_checked(rig: Rig) -> None:
    for payload, message in (
        (b"not json", "body is not JSON"),
        (b"[1]", "body is not an object"),
        ({"intent_id": "x"}, "body does not match execute-request"),
    ):
        r = await rig.post(EXECUTE, payload)
        assert (r.status_code, r.json()["message"]) == (422, message)
    unauth = await rig.post(EXECUTE, b"not json", auth=None)
    assert unauth.status_code == 401  # identity of the caller first
    assert (await rig.post("/healthz", b"", auth=None)).status_code == 405
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=rig.internal.app()), base_url="http://i") as h:
        health = await h.get("/healthz")
        assert (health.status_code, health.json()) == (200, {"status": "ok"})
        assert (await h.post("/internal/v1/execute/kit.box.get")).status_code == 404  # only executes_via routes


async def test_revoked_grant_and_dispatch_refusals(rig: Rig) -> None:
    await rig.propose()
    rig.fake.dispatch_status = 423
    r = (await rig.execute())[0]
    assert (r.status_code, r.json()["code"], r.json()["message"]) == (423, "effect_disabled", "effects are disabled")
    for status in (503, 409, 201):
        rig.fake.dispatch_status = status
        r = (await rig.execute())[0]
        assert (r.status_code, r.json()["message"]) == (409, "dispatch refused")
    rig.fake.dispatch_status = None
    rig.write([{**GRANTS, "grants": [{**GRANTS["grants"][0], "actions": ["read"]}]}], datetime.now(UTC))
    r = (await rig.execute())[0]
    assert (r.status_code, r.json()["message"]) == (403, "the requester no longer may propose on this resource")
    rig.fake.down = True
    r = (await rig.execute())[0]
    assert r.status_code in (401, 403, 503) and not rig.provider.sent


async def test_unexpected_error_before_dispatch(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    await rig.propose()

    def boom(*_: Any) -> bool:
        raise RuntimeError("secret-zzz")

    rig.internal.serves = boom
    r = (await rig.execute())[0]
    assert (r.status_code, r.json()) == (
        500,
        {"code": "internal_error", "message": "internal error", "retryable": False},
    )
    assert "secret-zzz" not in caplog.text and not rig.provider.sent
    [frames] = [m.getMessage() for m in caplog.records if m.name == "kit_fx.errors"]
    assert frames.startswith("internal_error ") and " RuntimeError\n" in frames and frames.endswith(" in boom")


def test_connect_reads_the_key_and_the_jwks(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    (tmp_path / "k").write_text(b64url_encode(key.private_bytes_raw()) + "\n", encoding="ascii")

    class S(mcp.Settings):
        PREFIX = "KIT"

    settings = S.from_env(
        {"KIT_APPROVAL_URL": "http://a", "KIT_SVC_KEY_FILE": str(tmp_path / "k"), "KIT_APPROVAL_AUDIENCE": "aud"}
    )
    executor = Ed25519PrivateKey.generate()
    seen: list[str] = []

    def jwks(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(200, content=bundle.jwks(executor.public_key()))

    approval, keys = effects.connect(settings, "kit", effects.Wiring(jwks=httpx2.MockTransport(jwks)))
    assert keys.readiness().ready and seen == [effects.JWKS_PATH]
    assert (approval.connector_id, approval.audience) == ("kit", "aud")
    assert str(approval.http.base_url) == "http://a"
    token = svc_jwt.sign(approval._key, iss="kit", aud="aud")
    assert (
        token.count(".") == 2 and approval._key.public_key().public_bytes_raw() == key.public_key().public_bytes_raw()
    )
    for content in ("", "not base64 !", b64url_encode(b"short")):
        (tmp_path / "k").write_text(content, encoding="ascii")
        with pytest.raises(mcp.ConfigError, match=r"^KIT_SVC_KEY_FILE: (ValueError|Error)$"):
            effects.connect(settings, "kit")
    (tmp_path / "k").unlink()
    with pytest.raises(mcp.ConfigError, match=r"^KIT_SVC_KEY_FILE: FileNotFoundError$"):
        effects.connect(settings, "kit")


async def test_two_executions_in_a_row_and_receipt_fallback(rig: Rig) -> None:
    await rig.propose()
    first, e1, _ = await rig.execute()
    rig.provider.mode = "ok_without_receipt"
    second, e2, _ = await rig.execute()
    assert first.status_code == second.status_code == 200
    assert second.json()["operation_id"] == e2 and rig.fake.dispatched == [e1, e2]


async def test_internal_audit_lines(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import re

    caplog.set_level(logging.INFO)
    await rig.propose()
    [(intent, body)] = rig.fake.intents.items()
    await rig.post(EXECUTE, b"x", auth="Basic abc")
    await rig.post(EXECUTE, b"x", auth=None)
    await rig.execute(token=rig.token(str(uuid.uuid4()), body))  # bound to another intent
    _, execution, _ = await rig.execute()
    await rig.post(RECONCILE, {"intent_id": intent, "execution_id": execution, "idempotency_key": "k" * 8})
    rig.provider.mode = "crash"
    await rig.execute()

    def boom(*_: Any) -> bool:
        raise RuntimeError("x")

    rig.internal.serves = boom
    await rig.execute()
    lines = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kit_fx.audit"]
    for line in lines:
        assert len(line.pop("request_id")) == 36 and 0 <= line.pop("duration_ms") < 5000
        assert line.pop("tool") == "kit.msg.execute"
    basic, none, binding, done, rec, crash, boomed = lines
    assert basic == none == {"op": "execute", "outcome": "unauthenticated", "reason": "missing"}
    assert binding == {"op": "execute", "outcome": "unauthenticated", "reason": "binding"}
    ids = {"tenant": "t-alpha", "principal": ACTOR, "intent_id": intent}
    assert done == {"op": "execute", "outcome": "succeeded", **ids, "execution_id": execution}
    assert rec == {"op": "reconcile", "outcome": "executed", "intent_id": intent, "execution_id": execution}
    assert crash.pop("execution_id") and crash == {"op": "execute", "outcome": "outcome_unknown", **ids,
                                                   "error": "RuntimeError"}  # fmt: skip
    assert boomed.pop("execution_id") and boomed == {"op": "execute", "outcome": "internal_error", **ids,
                                                     "error": "RuntimeError"}  # fmt: skip
    [frames] = [m.getMessage() for m in caplog.records if m.name == "kit_fx.errors"]
    head, *stack = frames.split("\n")
    assert head == "internal_error " + [json.loads(m.getMessage()) for m in caplog.records
                                        if m.name == "kit_fx.audit"][-1]["request_id"] + " RuntimeError"  # fmt: skip
    assert stack and all(re.fullmatch(r"/\S+\.py:\d+ in \S+", f) for f in stack), stack


async def test_connect_wires_the_approval_transport(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    (tmp_path / "k").write_text(b64url_encode(key.private_bytes_raw()), encoding="ascii")

    class S(mcp.Settings):
        PREFIX = "KIT"

    settings = S.from_env({"KIT_APPROVAL_URL": "http://approval.test", "KIT_SVC_KEY_FILE": str(tmp_path / "k")})
    hosts: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        hosts.append(f"{request.url.host}{request.url.path}")
        return httpx2.Response(201, json={"intent_id": "i", "status": "pending_approval"})

    transport = httpx2.MockTransport(answer)
    approval, jwks = effects.connect(settings, "kit", effects.Wiring(approval=transport, jwks=transport))
    r = await approval.post("/v1/intents", {})
    assert r.status_code == 201 and approval.audience == "approval-api"
    jwks.readiness()
    assert hosts == ["approval.test/v1/intents", f"approval.test{effects.JWKS_PATH}"]


async def test_internal_duration_and_spaced_token(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO)
    await rig.propose()
    await rig.post(EXECUTE, b"x", auth="Bearer a b")  # the token is everything after the first space
    rig.provider.mode = "slow"
    assert (await rig.execute())[0].status_code == 200
    spaced, slow = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kit_fx.audit"]
    assert spaced["outcome"] == "unauthenticated" and spaced["reason"] != "missing"
    assert 250 <= slow["duration_ms"] <= 2000
