"""Stage C of `demo` without the Approval Service: `demo.message.propose` against a mock `api`, the fake provider,
its test controls and the configuration. The end-to-end ACs (AC-M8-01…12, 14) run in approval/tests_db/test_executor.py.
"""

from __future__ import annotations

import base64
import json
import shutil
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from demo_connector import server, testctl
from demo_connector.backend import Backend
from demo_connector.effects import EXECUTE, PROPOSE, args_hash
from demo_connector.provider import Dropped, Provider
from gxg_connector_kit import _schema, canonical, exec_token, svc_jwt
from gxg_connector_kit.identity import StubIssuer
from gxg_policy import cli as policy_cli

pytestmark = pytest.mark.anyio
REPO = next(p for p in Path(__file__).resolve().parents if (p / "policies").is_dir())
DEMO = REPO / "connectors" / "demo"
ACTOR, READER, NOBODY = "agent:t-alpha:actor", "agent:t-alpha:reader", "agent:t-alpha:nobody"
KID = "demo-test"
ARGS = {"board_id": "a-public", "recipient": "partner_1", "text": "hello"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def public_key(key: Any) -> str:
    return "ed25519:" + base64.urlsafe_b64encode(key.public_bytes_raw()).rstrip(b"=").decode()


EXECUTOR_SVC = Ed25519PrivateKey.generate()  # the executor's service JWT key, in the test registry
EXEC = Ed25519PrivateKey.generate()  # execution-token key, in the mock JWKS


def build_bundle(out: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = out.parent / f"{out.name}-policies"
    (root / "tenants").mkdir(parents=True)
    (root / "platform").mkdir()
    for f in (DEMO / "fixtures" / "grants").glob("t-*.yaml"):
        shutil.copy(f, root / "tenants" / f.name)
    registry = yaml.safe_load((REPO / "policies" / "platform" / "registry.example.yaml").read_text())
    for svc in registry["services"]:
        if svc["id"] == "executor":
            svc["public_keys"] = [public_key(EXECUTOR_SVC.public_key())]
    (root / "platform" / "registry.yaml").write_text(yaml.safe_dump(registry))
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    monkeypatch.setenv(policy_cli.KEY_ENV, base64.urlsafe_b64encode(seed).rstrip(b"=").decode())
    args = ["build", str(root), "--out", str(out), "--bundle-version", "0" * 40, "--kid", KID]
    assert policy_cli.main([*args, "--manifest", str(DEMO / "gxg-manifest.yaml")]) == 0
    return f"{KID}={public_key(Ed25519PrivateKey.from_private_bytes(seed).public_key())}"


class MockApi:
    """The `api` process as the connector sees it: records requests, answers with `reply`."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.reply: Callable[[httpx2.Request], httpx2.Response] = lambda r: httpx2.Response(
            201, json={"intent_id": "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "status": "approved", "version": 1}
        )
        self.jwks_up = True

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)

    def jwks(self, _request: httpx2.Request) -> httpx2.Response:
        keys = [exec_token.public_jwk(EXEC.public_key())] if self.jwks_up else []
        return httpx2.Response(200, json={"keys": keys})


class Agent:
    def __init__(self, http: httpx2.AsyncClient, session: ClientSession, issuer: StubIssuer) -> None:
        self.http, self.session, self.issuer = http, session, issuer

    async def call(self, principal: str, args: dict[str, Any]) -> dict[str, Any]:
        headers = self.issuer.headers(tenant_id=principal.split(":")[1], principal=principal, audience="demo")
        self.http.headers.update(headers)
        try:
            result = await self.session.call_tool(PROPOSE, args)
        finally:
            for k in headers:
                del self.http.headers[k]
        out: dict[str, Any] = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        return out


@pytest.fixture
def api() -> MockApi:
    return MockApi()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issuer: StubIssuer) -> server.Settings:
    monkeypatch.setenv("GXG_PROFILE", "test")
    keys = build_bundle(tmp_path / "bundle", monkeypatch)
    (tmp_path / "svc.seed").write_text(base64.urlsafe_b64encode(b"\x07" * 32).rstrip(b"=").decode())
    return server.Settings.from_env(
        {
            "GXG_PROFILE": "test",
            "DEMO_BUNDLE_DIR": str(tmp_path / "bundle"),
            "DEMO_BUNDLE_KEYS": keys,
            "DEMO_STATE_FILE": str(tmp_path / "state" / "bundle-state.json"),
            "DEMO_TEST_ISSUER_KEY": public_key(issuer.public_key),
            "DEMO_APPROVAL_URL": "https://approval-api.internal:8090",
            "DEMO_SVC_KEY_FILE": str(tmp_path / "svc.seed"),
            "DEMO_PROVIDER_DB": str(tmp_path / "provider" / "journal.sqlite3"),
            "DEMO_PROVIDER_TOKEN": "demo-secret-unit",
        }
    )


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch) -> StubIssuer:
    monkeypatch.setenv("GXG_PROFILE", "test")
    return StubIssuer()


@pytest.fixture
async def agent(settings: server.Settings, api: MockApi, issuer: StubIssuer) -> AsyncIterator[tuple[Agent, Any, Any]]:
    wiring = server.Wiring(approval=httpx2.MockTransport(api.handle), jwks=httpx2.MockTransport(api.jwks))
    app, connector = server.build(settings, Backend(scan_delay_s=0), wiring)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://demo")
        )
        read, write = await stack.enter_async_context(streamable_http_client("http://demo/mcp", http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        yield Agent(http, session, issuer), connector, http


def schema(rel: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((DEMO / rel).read_text())
    return doc


# --- configuration ---------------------------------------------------------------------------------


def test_approval_url_needs_a_service_key(tmp_path: Path) -> None:
    with pytest.raises(server.ConfigError, match="DEMO_SVC_KEY_FILE"):
        server.Settings.from_env({"DEMO_APPROVAL_URL": "https://approval"})


async def test_propose_is_published_with_its_schema_files_and_execute_never(
    agent: tuple[Agent, Any, Any],
) -> None:
    a, connector, _ = agent
    listed = {t.name: t for t in (await a.session.list_tools()).tools}
    assert sorted(listed) == ["demo.boards.list", "demo.message.propose", "demo.note.get", "demo.notes.search"]
    assert EXECUTE not in listed  # EFF-02, MCC-07
    tool = listed[PROPOSE]
    assert tool.input_schema == schema("schemas/message-propose.input.json")
    assert tool.output_schema == schema("schemas/message-propose.output.json")
    effects = connector.effects
    assert effects is not None and effects.tool["executes_via"] == EXECUTE


async def test_readyz_needs_the_executor_jwks(agent: tuple[Agent, Any, Any], api: MockApi) -> None:
    """MCC-09: a connector with effect tools is ready only with the Approval Service's JWKS."""
    _, connector, http = agent
    assert (await http.get("/readyz")).status_code == 200
    api.jwks_up = False
    connector.effects.jwks._refetch_at = float("-inf")  # the next readiness check refetches
    assert (await http.get("/readyz")).status_code == 503


def test_configs_add_the_internal_port_only_with_effects(settings: server.Settings) -> None:
    app, connector = server.build(settings, Backend(scan_delay_s=0))
    ports = [c.port for c in server.configs(settings, app, connector)]
    assert ports == [8080, 8081]
    test = server.configs(server.Settings(**{**settings.__dict__, "test_controls": True}), app, connector)
    assert [(c.host, c.port) for c in test] == [("0.0.0.0", 8080), ("0.0.0.0", 8081), ("127.0.0.1", 8090)]  # noqa: S104
    read_only = server.Settings(**{**settings.__dict__, "approval_url": None})
    app2, c2 = server.build(read_only, Backend(scan_delay_s=0))
    assert c2.effects is None and [c.port for c in server.configs(read_only, app2, c2)] == [8080]


# --- propose (EFF-04…EFF-08) ------------------------------------------------------------------------


async def test_propose_sends_canonical_args_hash_preview_and_requester(
    agent: tuple[Agent, Any, Any], api: MockApi
) -> None:
    a, _, _ = agent
    text = unicodedata.normalize("NFD", "привет, café")
    out = await a.call(
        ACTOR, {**ARGS, "text": text, "approved": True, "tenant_id": "t-beta", "idempotency_key": "key-00001"}
    )
    assert out.get("isError") is not True, out
    assert out["structuredContent"] == {"intent_id": "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "status": "approved"}
    jsonschema.validate(out["structuredContent"], schema("schemas/message-propose.output.json"))
    (req,) = api.requests
    assert req.url.path == "/v1/intents"
    body = json.loads(req.content)
    assert _schema.validator("intent-create").is_valid(body)
    nfc = unicodedata.normalize("NFC", text)
    assert body["canonical_args"] == {"recipient": "partner_1", "text": nfc}  # nothing from the extra fields (EX-02)
    resource = "demo:demo-main/board/a-public"
    target = ("demo.message.propose", "1.0.0", "t-alpha", "demo-main", resource)
    assert body["canonical_args_hash"] == args_hash(target, {"recipient": "partner_1", "text": nfc})
    assert body["requester"] == {"principal": ACTOR, "tenant_id": "t-alpha"}  # identity, not arguments
    assert body["idempotency_key"] == "key-00001"
    assert body["preview"]["title"] == "Отправка демо-сообщения"
    assert body["preview"]["target"] == {"label": "Alpha public", "rid": resource}  # EFF-30: label from the backend
    assert [f["label"] for f in body["preview"]["fields"]] == ["Получатель", "Текст"]
    assert body["preview"]["fields"][1]["value"] == nfc
    token = req.headers["authorization"].split()[1]
    assert _jwt_claims(token)["iss"] == "demo" and _jwt_claims(token)["aud"] == "approval-api"


def _jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    doc: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return doc


async def test_propose_without_a_key_gets_a_generated_one(agent: tuple[Agent, Any, Any], api: MockApi) -> None:
    a, _, _ = agent
    await a.call(ACTOR, ARGS)
    await a.call(ACTOR, ARGS)
    keys = [json.loads(r.content)["idempotency_key"] for r in api.requests]
    assert all(k.startswith("demo:") and 8 <= len(k) <= 256 for k in keys) and keys[0] != keys[1]  # EFF-08


@pytest.mark.parametrize(
    ("principal", "board", "code"),
    [(READER, "a-public", "resource_forbidden"), (NOBODY, "a-public", "not_found"), (ACTOR, "a-sales", "not_found"),
     (ACTOR, "nope", "not_found"), (ACTOR, "b-public", "not_found")],
)  # fmt: skip
async def test_propose_rights_before_the_approval_service(
    agent: tuple[Agent, Any, Any], api: MockApi, principal: str, board: str, code: str
) -> None:
    """EFF-04, GR-17: read without propose → resource_forbidden; no right, no board, other tenant's → not_found."""
    a, _, _ = agent
    out = await a.call(principal, {**ARGS, "board_id": board})
    assert out["isError"] is True and out["structuredContent"]["code"] == code
    assert api.requests == []


@pytest.mark.parametrize(
    ("status", "body", "code", "retry"),
    [
        (409, {"code": "conflict", "message": "idempotency_key is already used", "retryable": False}, "conflict", None),
        (429, {"code": "rate_limited", "message": "quota", "retryable": True, "retry_after_s": 7}, "rate_limited", 7),
        (403, {"code": "permission_denied", "message": "policy", "retryable": False}, "permission_denied", None),
        (423, {"code": "effect_disabled", "message": "switch", "retryable": False}, "effect_disabled", None),
        (429, {"code": "rate_limited", "message": "no retry"}, "temporarily_unavailable", None),
        (401, {"code": "unauthenticated", "message": "token rejected"}, "temporarily_unavailable", None),
        (500, {"code": "internal_error", "message": "x"}, "temporarily_unavailable", None),
        (502, None, "temporarily_unavailable", None),
        (201, {"unexpected": True}, "temporarily_unavailable", None),
    ],
)
async def test_propose_maps_the_approval_service_answer(
    agent: tuple[Agent, Any, Any], api: MockApi, *, status: int, body: Any, code: str, retry: int | None
) -> None:
    a, _, _ = agent
    api.reply = lambda _r: (
        httpx2.Response(status, json=body) if body is not None else httpx2.Response(status, text="<html>")
    )
    out = await a.call(ACTOR, ARGS)
    assert out["isError"] is True and out["structuredContent"]["code"] == code
    assert out["structuredContent"].get("retry_after_s") == retry


async def test_propose_when_the_approval_service_is_down(agent: tuple[Agent, Any, Any], api: MockApi) -> None:
    """EX-16: no intent, a retryable error (the agent may repeat with the same idempotency key)."""

    def down(_r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    a, _, _ = agent
    api.reply = down
    out = await a.call(ACTOR, ARGS)
    assert out["structuredContent"] == {
        "code": "temporarily_unavailable",
        "message": "approval service is not reachable",
        "retryable": True,
    }


# --- internal endpoints without an executor -----------------------------------------------------------


async def test_internal_endpoints_need_an_executor_service_jwt(settings: server.Settings, api: MockApi) -> None:
    wiring = server.Wiring(approval=httpx2.MockTransport(api.handle), jwks=httpx2.MockTransport(api.jwks))
    _, connector = server.build(settings, Backend(scan_delay_s=0), wiring)
    assert connector.effects is not None
    internal = connector.effects.internal_app()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=internal), base_url="http://i") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        for op in ("execute", "reconcile"):
            r = await c.post(f"/internal/v1/{op}/{EXECUTE}", json={})
            assert r.status_code == 401
            demo_key = Ed25519PrivateKey.generate()  # not the executor's registry key
            auth = {"Authorization": "Bearer " + svc_jwt.sign(demo_key, iss="executor", aud="demo")}
            assert (await c.post(f"/internal/v1/{op}/{EXECUTE}", json={}, headers=auth)).status_code == 401
    assert api.requests == []


# --- fake provider ---------------------------------------------------------------------------------


def send(p: Provider, key: str = "k-1", intent: str = "i-1", tenant: str = "t-alpha") -> Any:
    import asyncio

    return asyncio.run(
        p.send(tenant=tenant, intent_id=intent, execution_id="e", idempotency_key=key, recipient="r_1", text="t")
    )


def test_provider_dedupes_by_tenant_and_key_unless_no_dedupe(tmp_path: Path) -> None:
    p = Provider(tmp_path / "j.sqlite3", "demo-secret-x")
    first = send(p)
    again = send(p)
    assert (first.outcome, again.outcome) == ("ok", "duplicate") and again.operation_id == first.operation_id
    assert send(p, tenant="t-beta").outcome == "ok"  # another tenant's account: no dedupe across tenants
    p.set_mode("ok,no_dedupe")
    assert send(p).outcome == "ok"
    calls = p.calls("k-1")
    assert [c["attempt"] for c in calls if c["tenant"] == "t-alpha"] == [1, 2, 3]
    assert "demo-secret-x" not in json.dumps(calls) and "t" not in [c["recipient"] for c in calls]
    assert len(Provider(tmp_path / "j.sqlite3").calls()) == 4  # the journal survives a restart (SQLite file)


def test_provider_modes_count_and_lookup(tmp_path: Path) -> None:
    p = Provider(tmp_path / "j.sqlite3")
    p.set_mode("timeout_before_start", count=2)
    assert [send(p, intent="i-2").outcome for _ in range(3)] == ["timeout_before_start", "timeout_before_start", "ok"]
    assert p.lookup("i-2")[0] is True and p.lookup("i-unknown") == (False, None)
    for mode in ("reject", "rate_limited"):
        p.set_mode(mode)
        assert send(p, key=f"k-{mode}", intent=f"i-{mode}").outcome == mode
        assert p.lookup(f"i-{mode}") == (False, None)
    p.set_mode("commit_then_drop")
    with pytest.raises(Dropped):
        send(p, key="k-drop", intent="i-drop")
    assert p.lookup("i-drop")[0] is True  # the effect happened
    p.set_mode("ok,slow:5")
    assert p.delay_s == 0.005 and p.dedupe
    for bad in ("nope", "ok,slow:x", "ok,fast"):
        with pytest.raises(ValueError, match="unknown provider mode"):
            p.set_mode(bad)
    with pytest.raises(ValueError, match="count"):
        p.set_mode("ok", count=0)
    assert p.delay_s == 0.005  # a bad spec changes nothing


async def test_provider_test_controls(tmp_path: Path) -> None:
    provider = Provider(tmp_path / "j.sqlite3")
    ctl = testctl.create_app(Backend(scan_delay_s=0), provider)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=ctl), base_url="http://ctl") as c:
        r = await c.put("/_test/provider/mode", json={"mode": "reject", "count": 1})
        assert r.status_code == 200 and provider.mode == "reject" and provider.remaining == 1
        assert (await c.put("/_test/provider/mode", json={"mode": 5})).status_code == 400
        assert (await c.put("/_test/provider/mode", json={"mode": "bad"})).status_code == 400
        await provider.send(
            tenant="t-alpha", intent_id="i", execution_id="e", idempotency_key="k-1", recipient="r_1", text="t"
        )
        r = await c.get("/_test/provider/calls", params={"idempotency_key": "k-1"})
        assert r.json()["count"] == 1 and r.json()["calls"][0]["outcome"] == "reject"
    without = testctl.create_app(Backend(scan_delay_s=0))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=without), base_url="http://ctl") as c:
        assert (await c.get("/_test/provider/calls")).status_code == 404
        assert (await c.put("/_test/provider/mode", json={"mode": "ok"})).status_code == 404


def test_manifest_propose_matches_the_spec_example() -> None:
    """MCC-01: the propose entry of the manifest is the one of schemas/examples/demo.gxg-manifest.yaml."""
    ours = yaml.safe_load((DEMO / "gxg-manifest.yaml").read_text())["tools"]
    spec = yaml.safe_load((REPO / "schemas/examples/demo.gxg-manifest.yaml").read_text())["tools"]
    keys = ("name", "version", "effect_class", "action", "resource", "executes_via", "dispatch_timeout_s", "preview")
    pick = [{k: t.get(k) for k in keys} for t in ours if t["name"] == PROPOSE]
    assert pick == [{k: t.get(k) for k in keys} for t in spec if t["name"] == PROPOSE]
    assert canonical.normalize(pick[0]) == pick[0]


# --- execute / reconcile against a mock `api` (the end-to-end cases: approval/tests_db/test_executor.py) ----------

INTENT, EXECUTION = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "9d8c7b6a-5f4e-4d3c-9b2a-1f0e9d8c7b6a"
RES = "demo:demo-main/board/a-public"
GOOD = {"recipient": "partner_1", "text": "hello"}


def claims(**over: Any) -> dict[str, Any]:
    base = {
        "aud": "demo",
        "sub": INTENT,
        "jti": EXECUTION,
        "ten": "t-alpha",
        "req": ACTOR,
        "tool": "demo.message.propose@1.0.0",
        "con": "demo-main",
        "res": RES,
        "idk": "cand-1:v1",
    }
    base.update(over)
    tool, _, version = base["tool"].rpartition("@")
    base.setdefault("ahs", args_hash((tool, version, base["ten"], base["con"], base["res"]), GOOD))
    return base


class Internal:
    def __init__(self, client: httpx2.AsyncClient, api: MockApi, provider: Provider) -> None:
        self.client, self.api, self.provider = client, api, provider
        self.dispatch_status = 200

    async def execute(self, token: str, args: dict[str, Any] | None = None, **body: Any) -> httpx2.Response:
        auth = svc_jwt.sign(EXECUTOR_SVC, iss="executor", aud="demo")
        doc = {"intent_id": INTENT, "execution_id": EXECUTION, "execution_token": token, "args": args or GOOD, **body}
        return await self.client.post(
            f"/internal/v1/execute/{EXECUTE}", json=doc, headers={"Authorization": f"Bearer {auth}"}
        )

    def dispatches(self) -> list[httpx2.Request]:
        return [r for r in self.api.requests if r.url.path.endswith("/dispatch")]


@pytest.fixture
async def internal(settings: server.Settings, api: MockApi) -> AsyncIterator[Internal]:
    wiring = server.Wiring(approval=httpx2.MockTransport(api.handle), jwks=httpx2.MockTransport(api.jwks))
    _, connector = server.build(settings, Backend(scan_delay_s=0), wiring)
    assert connector.effects is not None
    box = Internal(httpx2.AsyncClient(), api, connector.effects.provider)
    deadline = {"execution_id": EXECUTION, "dispatch_deadline": "2030-01-01T00:00:00Z"}
    api.reply = lambda _r: httpx2.Response(box.dispatch_status, json=deadline)
    app = connector.effects.internal_app()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://i") as c:
        box.client = c
        yield box


async def test_execute_dispatches_then_calls_the_provider_once(internal: Internal) -> None:
    token = exec_token.sign(EXEC, claims())
    r = await internal.execute(token)
    assert r.status_code == 200
    assert r.json() == {
        "operation_id": r.json()["provider_receipt_ref"],
        "status": "succeeded",
        "effect_ref": None,
        "reconciliation_required": False,
        "provider_receipt_ref": r.json()["provider_receipt_ref"],
    }
    assert _schema.validator("effect-result").is_valid(r.json())
    (d,) = internal.dispatches()
    assert d.url.path == f"/v1/executions/{EXECUTION}/dispatch" and json.loads(d.content) == {"execution_token": token}
    assert _jwt_claims(d.headers["authorization"].split()[1])["aud"] == "approval-api"
    (call,) = internal.provider.calls()
    assert (call["tenant"], call["intent_id"], call["execution_id"], call["idempotency_key"], call["recipient"]) == (
        "t-alpha", INTENT, EXECUTION, "cand-1:v1", "partner_1",
    )  # fmt: skip
    again = await internal.execute(token)
    assert again.status_code == 409 and again.json()["message"] == "execution token already used"  # AC-M8-04
    assert len(internal.provider.calls()) == 1


@pytest.mark.parametrize(
    ("over", "args", "status"),
    [
        ({}, {"recipient": "partner_1", "text": "hellO"}, 422),  # EFF-18 п.2
        ({}, {"recipient": "partner_2", "text": "hello"}, 422),
        ({}, {"recipient": "partner_1", "text": 1.5}, 422),  # not canonical at all
        ({"tool": "demo.message.other@1.0.0"}, None, 403),  # EFF-18 п.3: not this connector's tool
        ({"tool": "demo.message.propose@2.0.0"}, None, 403),
        ({"con": "demo-other"}, None, 403),
        ({"res": "demo:demo-main/board/nope"}, None, 403),
        ({"res": "demo:demo-other/board/a-public"}, None, 403),
        ({"res": "demo:demo-main/board/b-public", "ten": "t-beta"}, None, 403),
        ({"ten": "t-gamma"}, None, 403),
        ({"req": READER}, None, 403),  # read but no propose (EX-14)
        ({"req": "agent:t-alpha:unknown"}, None, 403),
    ],
)
async def test_execute_refusals_before_dispatch(
    internal: Internal, over: dict[str, Any], args: dict[str, Any] | None, status: int
) -> None:
    r = await internal.execute(exec_token.sign(EXEC, claims(**over)), args)
    assert r.status_code == status, r.text
    assert internal.dispatches() == [] and internal.provider.calls() == []


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"intent_id": "00000000-0000-4000-8000-000000000001"}, 401),  # token bound to another intent
        ({"execution_id": "00000000-0000-4000-8000-000000000001"}, 401),
        ({"extra": 1}, 422),  # execute-request has no other fields
    ],
)
async def test_execute_body_must_match_the_token(internal: Internal, body: dict[str, Any], status: int) -> None:
    r = await internal.execute(exec_token.sign(EXEC, claims()), **body)
    assert r.status_code == status and internal.provider.calls() == []


async def test_replay_of_a_refused_token_is_401(internal: Internal) -> None:
    token = exec_token.sign(EXEC, claims())
    assert (await internal.execute(token, {"recipient": "partner_1", "text": "x"})).status_code == 422
    r = await internal.execute(token)
    assert r.status_code == 401 and internal.dispatches() == []  # AC-M8-03: the local jti cache


@pytest.mark.parametrize(("dispatch", "status"), [(423, 423), (409, 409), (404, 409), (401, 409), (503, 409)])
async def test_execute_when_dispatch_refuses(internal: Internal, dispatch: int, status: int) -> None:
    internal.dispatch_status = dispatch
    r = await internal.execute(exec_token.sign(EXEC, claims()))
    assert r.status_code == status and internal.provider.calls() == []


async def test_execute_when_dispatch_is_unreachable(internal: Internal) -> None:
    def down(_r: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    internal.api.reply = down
    r = await internal.execute(exec_token.sign(EXEC, claims()))
    assert r.status_code == 503 and r.json()["code"] == "temporarily_unavailable"  # EX-16
    assert internal.provider.calls() == []


@pytest.mark.parametrize(
    ("mode", "status"),
    [("reject", "failed_before_effect"), ("rate_limited", "failed_before_effect"),
     ("timeout_before_start", "failed_before_effect"), ("commit_then_drop", "outcome_unknown")],
)  # fmt: skip
async def test_provider_outcomes_after_dispatch(internal: Internal, mode: str, status: str) -> None:
    internal.provider.set_mode(mode)
    r = await internal.execute(exec_token.sign(EXEC, claims()))
    assert r.status_code == 200 and r.json()["status"] == status
    assert r.json()["reconciliation_required"] is (status == "outcome_unknown")
    assert r.json()["operation_id"] == EXECUTION and r.json()["provider_receipt_ref"] is None
    assert len(internal.provider.calls()) == 1


async def test_any_error_after_dispatch_is_outcome_unknown(internal: Internal, monkeypatch: pytest.MonkeyPatch) -> None:
    """AS-12: an exception of the connector after a successful dispatch is outcome_unknown, never retried."""

    async def broken(**_: Any) -> Any:
        raise OSError("provider connection reset")

    monkeypatch.setattr(internal.provider, "send", broken)
    r = await internal.execute(exec_token.sign(EXEC, claims()))
    assert r.status_code == 200 and r.json()["status"] == "outcome_unknown"


async def test_reconcile_by_the_provider_journal(internal: Internal) -> None:
    auth = {"Authorization": "Bearer " + svc_jwt.sign(EXECUTOR_SVC, iss="executor", aud="demo")}
    body = {"intent_id": INTENT, "execution_id": EXECUTION, "idempotency_key": "cand-1:v1"}
    r = await internal.client.post(f"/internal/v1/reconcile/{EXECUTE}", json=body, headers=auth)
    assert r.json() == {**{k: body[k] for k in ("intent_id", "execution_id")}, "finding": "not_executed",
                        "provider_operation_id": None}  # fmt: skip
    internal.provider.set_mode("commit_then_drop")
    await internal.execute(exec_token.sign(EXEC, claims()))
    auth = {"Authorization": "Bearer " + svc_jwt.sign(EXECUTOR_SVC, iss="executor", aud="demo")}
    r = await internal.client.post(f"/internal/v1/reconcile/{EXECUTE}", json=body, headers=auth)
    assert r.json()["finding"] == "executed" and r.json()["provider_operation_id"].startswith("demo-op-")
    assert _schema.validator("reconcile-result").is_valid(r.json())
    for bad in ({}, {"intent_id": "x", "execution_id": EXECUTION}, {"intent_id": INTENT}):
        auth = {"Authorization": "Bearer " + svc_jwt.sign(EXECUTOR_SVC, iss="executor", aud="demo")}
        r = await internal.client.post(f"/internal/v1/reconcile/{EXECUTE}", json=bad, headers=auth)
        assert r.status_code == 422
    auth = {"Authorization": "Bearer " + svc_jwt.sign(EXECUTOR_SVC, iss="executor", aud="demo")}
    r = await internal.client.post(f"/internal/v1/reconcile/{EXECUTE}", content=b"[", headers=auth)
    assert r.status_code == 422


async def test_service_jwt_of_another_kind_is_refused(internal: Internal) -> None:
    """AS-06: only an `executor` may call execute; a connector's own key (kind connector) may not."""
    other = Ed25519PrivateKey.generate()
    auth = {"Authorization": "Bearer " + svc_jwt.sign(other, iss="demo", aud="demo")}
    r = await internal.client.post(f"/internal/v1/execute/{EXECUTE}", json={}, headers=auth)
    assert r.status_code == 401 and internal.provider.calls() == []
