"""gxg_connector_kit.mcp (M1.1): the call pipeline, its resource hook, configuration, readiness, process.

A tiny domain `kit` (boxes) over a manifest written here; the MCP client is the official SDK over an ASGI transport.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gxg_connector_check import bundle
from gxg_connector_kit import mcp
from gxg_connector_kit.cf_identity import ContextForgeVerifier
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.identity import StubIssuer, StubVerifier
from gxg_connector_kit.mcp import Access, ConfigError, Result

pytestmark = pytest.mark.anyio
READER, WRITER, NOBODY = "agent:t-alpha:reader", "agent:t-alpha:writer", "agent:t-alpha:nobody"
GRANTS: dict[str, Any] = {
    "tenant": "t-alpha",
    "schema_version": 1,
    "connections": [{"id": "kit-main", "domain": "kit", "credential_ref": "secret://t-alpha/kit/main"}],
    "principals": {"agents": [{"id": READER}, {"id": WRITER}, {"id": NOBODY}]},
    "grants": [
        {"id": "g-read", "subjects": [READER], "actions": ["read"], "resources": ["kit:kit-main/box/b1"]},
        {"id": "g-write", "subjects": [WRITER], "actions": ["read", "propose"], "resources": ["kit:kit-main/box/b1"]},
        {"id": "g-write-b2", "subjects": [WRITER], "actions": ["read"], "resources": ["kit:kit-main/box/b2"]},
    ],
}
SCHEMAS: dict[str, Any] = {
    "list.in.json": {
        "description": "List boxes",
        "type": "object",
        "additionalProperties": False,
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 5}},
    },
    "list.out.json": {"type": "object", "required": ["boxes"], "properties": {"boxes": {"type": "array"}}},
    "get.in.json": {
        "description": "Get a box",
        "type": "object",
        "required": ["box_id"],
        "properties": {
            "box_id": {"type": "string", "maxLength": 10},
            "meta": {"type": "object", "properties": {"note": {"type": "string"}}},
        },
    },
    "get.out.json": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
}
TOOLS = [
    {
        "name": "kit.boxes.list",
        "version": "1.0.0",
        "effect_class": "read",
        "action": "read",
        "resource": {"type": "box", "scope": "list"},
        "input_schema": "schemas/list.in.json",
        "output_schema": "schemas/list.out.json",
        "limits": {"max_results": 5, "timeout_s": 2},
    },
    {
        "name": "kit.box.get",
        "version": "1.0.0",
        "effect_class": "read",
        "action": "read",
        "resource": {"type": "box", "scope": "single", "from_arg": "box_id"},
        "input_schema": "schemas/get.in.json",
        "output_schema": "schemas/get.out.json",
        "limits": {"timeout_s": 2, "max_bytes": 200},
    },
    {
        "name": "kit.box.propose",
        "version": "1.0.0",
        "effect_class": "write_reversible",
        "action": "propose",
        "resource": {"type": "box", "from_arg": "box_id"},
        "input_schema": "schemas/get.in.json",
        "output_schema": "schemas/get.out.json",
        "limits": {"timeout_s": 2},
    },
]


@dataclass
class Boxes:
    """`mcp.Domain` of the tests: boxes b1, b2 of t-alpha; records every resolve."""

    connector_id: str = "kit"
    rtype: str = "box"
    resolved: list[str] = field(default_factory=list)
    calls: list[tuple[str, Access, dict[str, Any]]] = field(default_factory=list)  # (resolve tool | handler, …)
    reply: Any = None  # what the handlers return instead of the default
    fail: BaseException | None = None
    delay_s: float = 0.0

    def connection(self, tenant: str) -> str | None:
        return "kit-main" if tenant == "t-alpha" else None

    async def resolve(self, tool: str, access: Access, args: dict[str, Any]) -> tuple[str, Any] | None:
        self.resolved.append(args["box_id"])
        self.calls.append((tool, access, args))
        return (
            (f"kit:kit-main/box/{args['box_id']}", f"target-{args['box_id']}")
            if args["box_id"] in ("b1", "b2")
            else None
        )

    def handlers(self) -> Mapping[str, mcp.Handler]:
        return {"kit.boxes.list": self.listing, "kit.box.get": self.get, "kit.box.propose": self.get}

    async def listing(self, access: Access, args: dict[str, Any]) -> Result:
        self.calls.append(("listing", access, args))
        if access.connection is None:  # nothing to list, no grant lookup
            return {"boxes": [], "nothing": access.nothing}, {}
        boxes = [b for b in ("b1", "b2") if access.check(f"kit:kit-main/box/{b}").allowed]
        return {"boxes": boxes, "nothing": access.nothing}, {"seen": len(boxes)}

    async def get(self, access: Access, args: dict[str, Any]) -> Result:
        self.calls.append(("handler", access, args))
        if self.delay_s:
            import asyncio

            await asyncio.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        return (self.reply if self.reply is not None else {"id": access.target}), {"rid": access.rid}


@dataclass
class World:
    tmp: Path
    issuer: StubIssuer
    signer: Ed25519PrivateKey
    domain: Boxes

    @property
    def settings(self) -> mcp.Settings:
        return mcp.Settings.from_env(
            {
                "GXG_PROFILE": "test",
                "GXG_BUNDLE_DIR": str(self.tmp / "bundle"),
                "GXG_BUNDLE_KEYS": bundle.keys_setting(self.signer),
                "GXG_STATE_FILE": str(self.tmp / "s" / "state.json"),
                "GXG_TEST_ISSUER_KEY": bundle.public(self.issuer.public_key),
            }
        )

    def write_bundle(self, grants: dict[str, Any] = GRANTS, at: datetime | None = None) -> None:
        platform = {
            "schema_version": 1,
            "services": [
                {
                    "id": "kit",
                    "kind": "connector",
                    "public_keys": [bundle.public(self.signer.public_key())],
                    "tenants": ["t-alpha"],
                    "internal_url": "https://kit.internal",
                }
            ],
        }
        bundle.write(
            self.tmp / "bundle",
            [grants],
            platform,
            self.signer,
            built_at=at or datetime.now(UTC) - timedelta(minutes=1),
        )

    def app(self, settings: mcp.Settings | None = None, logs: mcp.Logs | None = None) -> Any:
        settings = settings or self.settings
        store, files = mcp.bundle_store(settings)
        tools = mcp.tools(
            self.domain,
            self.tmp / "gxg-manifest.yaml",
            store,
            mcp.verifier(settings, "kit"),
            files.refresh,
            logs or mcp.Logs.of("kit_test"),
        )
        return mcp.create_app("kit", tools, files.refresh, store.readiness)

    def headers(self, principal: str, **kw: Any) -> dict[str, str]:
        return self.issuer.headers(tenant_id=principal.split(":")[1], principal=principal, audience="kit", **kw)


def write_manifest(tmp: Path, tools: list[dict[str, Any]] = TOOLS, schemas: dict[str, Any] = SCHEMAS) -> Path:
    (tmp / "schemas").mkdir(exist_ok=True)
    for name, doc in schemas.items():
        (tmp / "schemas" / name).write_text(json.dumps(doc), encoding="utf-8")
    path = tmp / "gxg-manifest.yaml"
    path.write_text(
        yaml.safe_dump({"connector": "kit", "version": "0.1.0", "contract": "gxg-mcp-connector/v1", "tools": tools}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def world(tmp_path: Path, test_profile: None) -> World:
    write_manifest(tmp_path)
    w = World(tmp_path, StubIssuer(), Ed25519PrivateKey.generate(), Boxes())
    w.write_bundle()
    return w


@asynccontextmanager
async def client(app: Any) -> AsyncIterator[tuple[httpx2.AsyncClient, ClientSession]]:
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://k")
        )
        read, write = await stack.enter_async_context(streamable_http_client("http://k/mcp", http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        yield http, session


async def call(  # noqa: PLR0917 - the test helper reads best positional
    world: World,
    session: ClientSession,
    http: httpx2.AsyncClient,
    tool: str,
    args: dict[str, Any],
    principal: str | None = READER,
) -> dict[str, Any]:
    headers = world.headers(principal) if principal else {}
    http.headers.update(headers)
    try:
        r = await session.call_tool(tool, args)
    finally:
        for k in headers:
            del http.headers[k]
    out: dict[str, Any] = r.model_dump(by_alias=True, exclude_none=True, mode="json")
    return out


def audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "kit_test.audit"]


# --- the pipeline -----------------------------------------------------------------------------------------------


async def test_allowed_call_and_its_audit_line(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    async with client(world.app()) as (http, s):
        r = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        listing = await call(world, s, http, "kit.boxes.list", {}, principal=WRITER)
    assert r["structuredContent"] == {"id": "target-b1"} and r.get("isError") is not True
    assert listing["structuredContent"] == {"boxes": ["b1", "b2"], "nothing": False}
    first, second = audit(caplog)
    assert (first["tool"], first["tenant"], first["principal"], first["outcome"]) == (
        "kit.box.get",
        "t-alpha",
        READER,
        "ok",
    )
    assert first["grant_id"] == ["g-read"] and first["rid"] == "kit:kit-main/box/b1"
    assert isinstance(first["duration_ms"], int) and first["request_id"] != second["request_id"]
    assert "grant_id" not in second and second["seen"] == 2


async def test_refusals_and_their_order(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    async with client(world.app()) as (http, s):
        anon = await call(world, s, http, "kit.box.get", {"box_id": "b1"}, principal=None)
        bad = await call(world, s, http, "kit.box.get", {"box_id": "x" * 11})
        nobody = await call(world, s, http, "kit.box.get", {"box_id": "b1"}, principal=NOBODY)
        hidden = await call(world, s, http, "kit.box.get", {"box_id": "b2"})
        missing = await call(world, s, http, "kit.box.get", {"box_id": "b9"})
        forbidden = await call(world, s, http, "kit.box.propose", {"box_id": "b2"}, principal=WRITER)
        readable_only = await call(world, s, http, "kit.box.propose", {"box_id": "b1"})
        none_at_all = await call(world, s, http, "kit.box.propose", {"box_id": "b1"}, principal=NOBODY)
        allowed = await call(world, s, http, "kit.box.propose", {"box_id": "b1"}, principal=WRITER)
    assert anon["structuredContent"] == {"code": "unauthenticated", "message": "token rejected", "retryable": False}
    assert bad["structuredContent"] == {
        "code": "invalid_input",
        "message": "arguments do not match the input schema",
        "retryable": False,
        "details": {"path": "/box_id"},
    }
    nf = {"code": "not_found", "message": "not found", "retryable": False}
    assert nobody["structuredContent"] == hidden["structuredContent"] == missing["structuredContent"] == nf
    assert none_at_all["structuredContent"] == nf
    forbid = {"code": "resource_forbidden", "message": "no propose right on this resource", "retryable": False}
    assert forbidden["structuredContent"] == readable_only["structuredContent"] == forbid  # may read it (GR-17)
    assert allowed["structuredContent"] == {"id": "target-b1"}
    for r in (anon, bad, nobody, hidden, missing, forbidden):
        assert r["isError"] is True
    # not resolved: anonymous, invalid, no grant of the type at all (also not for propose without read)
    assert world.domain.resolved == ["b2", "b9", "b2", "b1", "b1"]
    outcomes = [(a["outcome"], a.get("reason")) for a in audit(caplog)]
    assert outcomes[:3] == [("unauthenticated", "missing"), ("invalid_input", None), ("not_found", None)]


async def test_list_without_grants_is_empty_and_unknown_tenant(world: World) -> None:
    async with client(world.app()) as (http, s):
        r = await call(world, s, http, "kit.boxes.list", {}, principal=NOBODY)
        other = await call(world, s, http, "kit.boxes.list", {}, principal="agent:t-beta:reader")
        single = await call(world, s, http, "kit.box.get", {"box_id": "b1"}, principal="agent:t-beta:reader")
    assert r["structuredContent"] == other["structuredContent"] == {"boxes": [], "nothing": True}
    unknown = [a for what, a, _ in world.domain.calls if what == "listing" and a.connection is None]
    assert len(unknown) == 1 and unknown[0].ident.principal == "agent:t-beta:reader" and unknown[0].action == "read"
    assert unknown[0].check("kit:kit-main/box/b1").allowed is False  # the caller's check, denying for t-beta
    assert single["structuredContent"]["code"] == "not_found" and world.domain.resolved == []


async def test_without_bundle_every_call_is_unavailable(world: World) -> None:
    settings = replace(world.settings, bundle_dir=world.tmp / "none")
    async with client(world.app(settings)) as (http, s):
        r = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        ready = await http.get("/readyz")
        health = await http.get("/healthz")
    assert r["structuredContent"]["code"] == "temporarily_unavailable" and world.domain.resolved == []
    assert (ready.status_code, ready.json()) == (503, {"status": "not_ready"})
    assert (health.status_code, health.json()) == (200, {"status": "ok"})


async def test_ready_with_a_bundle_and_reload(world: World) -> None:
    app = world.app()
    async with client(app) as (http, s):
        ready = await http.get("/readyz")
        revoked = {**GRANTS, "grants": [g for g in GRANTS["grants"] if g["id"] != "g-read"]}
        world.write_bundle(revoked, datetime.now(UTC))
        r = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
    assert (ready.status_code, ready.json()) == (200, {"status": "ready"})
    assert r["structuredContent"]["code"] == "not_found"  # the new bundle is used on the next call (GR-12)


async def test_output_schema_size_timeout_and_unexpected_errors(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    internal = {"code": "internal_error", "message": "internal error", "retryable": False}
    async with client(world.app()) as (http, s):
        world.domain.reply = {"no_id": 1}
        wrong = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.reply = {"id": "x" * 191}  # 201 bytes
        big = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.reply = {"id": "x" * 190}  # 200 bytes: the limit itself
        fits = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.reply, world.domain.fail = None, RuntimeError("secret-xyz")
        boom = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.fail = GxgError("rate_limited", "slow down", retry_after_s=3)
        limited = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.fail, world.domain.delay_s = None, 5
        slow = await call(world, s, http, "kit.box.get", {"box_id": "b1"})
    assert wrong["structuredContent"] == big["structuredContent"] == boom["structuredContent"] == internal
    assert fits["structuredContent"] == {"id": "x" * 190}
    assert limited["structuredContent"] == {
        "code": "rate_limited",
        "message": "slow down",
        "retryable": True,
        "retry_after_s": 3,
    }
    assert slow["structuredContent"] == {
        "code": "temporarily_unavailable",
        "message": "backend did not answer in time",
        "retryable": True,
    }
    assert "secret-xyz" not in caplog.text
    lines = audit(caplog)
    assert [line["error"] for line in lines if line["outcome"] == "internal_error"] == [
        "OutputSchemaError",
        "OutputTooLarge",
        "RuntimeError",
    ]
    [frames] = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kit_test.errors" and "RuntimeError" in r.getMessage() and " in get" in r.getMessage()
    ]
    assert frames.startswith(f"internal_error {lines[3]['request_id']} RuntimeError\n")


def test_tools_need_handlers_for_every_read_tool(world: World) -> None:
    store, files = mcp.bundle_store(world.settings)

    class Fewer(Boxes):
        def handlers(self) -> Mapping[str, mcp.Handler]:
            return {"kit.boxes.list": self.listing, "kit.box.get": self.get}

    class More(Boxes):
        def handlers(self) -> Mapping[str, mcp.Handler]:
            return {**super().handlers(), "kit.extra.get": self.get}

    args = (world.tmp / "gxg-manifest.yaml", store, mcp.NoVerifier(), files.refresh, mcp.Logs.of("x"))
    with pytest.raises(ConfigError, match=r"^manifest tools and handlers differ$"):
        mcp.tools(Fewer(), *args)  # a write tool without a handler is not optional
    with pytest.raises(ConfigError, match=r"^manifest tools and handlers differ$"):
        mcp.tools(More(), *args)
    effect = [
        *TOOLS,
        {
            "name": "kit.msg.propose",
            "version": "1.0.0",
            "effect_class": "external_effect",
            "action": "propose",
            "resource": {"type": "box"},
            "executes_via": "kit.msg.execute",
            "preview": {"title": "t", "fields": {"a": "A"}},
            "input_schema": "schemas/get.in.json",
            "output_schema": "schemas/get.out.json",
            "limits": {"timeout_s": 1},
        },
    ]
    (world.tmp / "m").mkdir()
    path = write_manifest(world.tmp / "m", effect)
    names = [t.name for t in mcp.tools(Boxes(), path, *args[1:])]
    assert names == ["kit.boxes.list", "kit.box.get", "kit.box.propose"]  # the effect tool is not published
    assert [t.timeout for t in mcp.tools(Boxes(), *args)] == [2.0, 2.0, 2.0]


# --- manifest ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"name": "other.boxes.list"}, "a public tool is <connector>.<object>.<verb>"),
        ({"name": "kit.boxes.execute"}, "a public tool is <connector>.<object>.<verb>"),
        ({"name": "kit.boxes.reconcile"}, "a public tool is <connector>.<object>.<verb>"),
        ({"output_schema": None}, "output_schema is required"),
        ({"limits": {"max_results": 4, "timeout_s": 1}}, "must have a maximum <= limits.max_results"),
        ({"input_schema": "schemas/get.in.json"}, "must have a maximum <= limits.max_results"),
        ({"version": "1"}, "gxg-manifest.yaml: "),
    ],
)
def test_manifest_lint(tmp_path: Path, change: dict[str, Any], error: str) -> None:
    tool = {k: v for k, v in {**TOOLS[0], **change}.items() if v is not None}
    path = write_manifest(tmp_path, [tool])
    with pytest.raises(ConfigError, match=error):
        mcp.load_manifest(path)


def test_manifest_limit_bound_is_inclusive_and_needs_an_integer(tmp_path: Path) -> None:
    write_manifest(tmp_path)
    assert [t["name"] for t in mcp.load_manifest(tmp_path / "gxg-manifest.yaml")] == [t["name"] for t in TOOLS]
    schemas = {**SCHEMAS, "list.in.json": {**SCHEMAS["list.in.json"], "properties": {"limit": {"maximum": 5.0}}}}
    write_manifest(tmp_path, schemas=schemas)
    with pytest.raises(ConfigError, match="maximum"):
        mcp.load_manifest(tmp_path / "gxg-manifest.yaml")


def test_load_schema_finds_repository_schemas_by_their_tail(tmp_path: Path) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "x.json").write_text('{"a": 1}', encoding="utf-8")
    deep = tmp_path / "connectors" / "c"
    deep.mkdir(parents=True)
    assert mcp.load_schema(deep, "../../schemas/x.json") == {"a": 1}
    with pytest.raises(ConfigError, match=r"^schema not found: schemas/y.json$"):
        mcp.load_schema(deep, "schemas/y.json")


# --- configuration ----------------------------------------------------------------------------------------------


class Demo(mcp.Settings):
    PREFIX = "DEMO"


def test_settings_from_env() -> None:
    s = Demo.from_env(
        {
            "DEMO_BUNDLE_DIR": "/b",
            "DEMO_BUNDLE_KEYS": "k",
            "DEMO_IDENTITY_KEYS": "i",
            "DEMO_APPROVAL_URL": "https://a",
            "DEMO_SVC_KEY_FILE": "/k",
            "DEMO_APPROVAL_AUDIENCE": "aud",
            "GXG_BUNDLE_DIR": "/ignored",
        }
    )
    assert isinstance(s, Demo)
    assert (s.profile, s.bundle_dir, s.bundle_keys, s.identity_keys) == ("production", Path("/b"), "k", "i")
    assert (s.approval_url, s.svc_key_file, s.approval_audience) == ("https://a", "/k", "aud")
    d = Demo.from_env({})
    assert (d.bundle_dir, d.state_file, d.test_issuer_key, d.test_controls) == (
        Path("/bundle"),
        Path("/var/lib/demo/bundle-state.json"),
        None,
        False,
    )
    assert (d.approval_url, d.approval_audience, d.svc_key_file, d.identity_keys, d.bundle_keys) == (
        "",
        "approval-api",
        "",
        "",
        "",
    )
    assert Demo.from_env({"GXG_PROFILE": "test", "DEMO_TEST_CONTROLS": "1"}).test_controls is True
    assert Demo.from_env({"DEMO_TEST_CONTROLS": "yes"}).test_controls is False
    assert Demo.from_env({"DEMO_TEST_ISSUER_KEY": "", "GXG_PROFILE": "x"}).test_issuer_key is None


@pytest.mark.parametrize(
    ("env", "error"),
    [
        ({"DEMO_TEST_ISSUER_KEY": "k"}, "^test issuer and test controls are allowed only with GXG_PROFILE=test$"),
        ({"DEMO_TEST_CONTROLS": "1"}, "^test issuer and test controls are allowed only with GXG_PROFILE=test$"),
        (
            {"GXG_PROFILE": "test", "DEMO_TEST_ISSUER_KEY": "k", "DEMO_IDENTITY_KEYS": "i"},
            "^DEMO_TEST_ISSUER_KEY and DEMO_IDENTITY_KEYS are exclusive$",
        ),
        ({"DEMO_APPROVAL_URL": "https://a"}, r"^DEMO_APPROVAL_URL needs DEMO_SVC_KEY_FILE \(service JWT key, AS-06\)$"),
    ],
)
def test_settings_refused(env: dict[str, str], error: str) -> None:
    with pytest.raises(ConfigError, match=error):
        Demo.from_env(env)


def test_parse_keys() -> None:
    key = bundle.public(Ed25519PrivateKey.generate().public_key())
    assert list(mcp.parse_keys(f" a={key} , ,b={key}", "N")) == ["a", "b"] and mcp.parse_keys("", "N") == {}
    with pytest.raises(ConfigError, match=r"^N: \S"):
        mcp.parse_keys(f"a=b={key}", "N")  # the first `=` separates the kid
    for bad in ("nokey", f"={key}"):
        with pytest.raises(ConfigError, match=r"^N: expected kid=ed25519:<base64url>$"):
            mcp.parse_keys(bad, "N")
    with pytest.raises(ConfigError, match=r"^N: \S"):
        mcp.parse_keys("k=rsa:AAAA", "N")


def test_verifier_by_settings(world: World) -> None:
    assert isinstance(mcp.verifier(world.settings, "kit"), StubVerifier)
    key = bundle.public(Ed25519PrivateKey.generate().public_key())
    cf = replace(world.settings, test_issuer_key=None, identity_keys=f"a={key}")
    assert isinstance(mcp.verifier(cf, "kit"), ContextForgeVerifier)
    none = mcp.verifier(replace(cf, identity_keys=""), "kit")
    with pytest.raises(GxgError) as exc:
        none.verify({})
    assert getattr(exc.value, "reason", None) == "no identity verifier configured"


def test_bundle_files_keep_the_previous_bundle(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    events = logging.getLogger("kit_test")
    store, files = mcp.bundle_store(world.settings, events)
    assert store.readiness().ready
    (world.tmp / "bundle" / mcp.SIG_FILE).write_bytes(b"{}")
    files.refresh()
    files.refresh()  # unchanged files are not read again
    assert store.readiness().ready
    messages = [json.loads(r.getMessage()) for r in caplog.records if r.name == "kit_test"]
    assert messages[0] == {"event": "bundle_loaded", "bundle_version": "0" * 40}
    assert [m["event"] for m in messages] == ["bundle_loaded", "bundle_rejected"]
    (world.tmp / "bundle" / mcp.SIG_FILE).unlink()
    files.refresh()  # a missing file changes nothing
    assert store.readiness().ready and len([r for r in caplog.records if r.name == "kit_test"]) == 2


def test_cursor_is_bound() -> None:
    c = mcp.Cursor()
    token = c.make(b"q", 20)
    assert c.offset(b"q", token) == 20 and c.offset(b"q", None) == 0
    for bad in (c.make(b"other", 20), "20." + "0" * 64, "x", token + "0", f"9999999999.{'a' * 64}"):
        with pytest.raises(GxgError, match=r"^cursor is not valid for this caller and query$"):
            c.offset(b"q", bad)
    assert mcp.Cursor().make(b"q", 20) != token  # a key per process


async def test_readiness_of_combines() -> None:
    from gxg_connector_kit.readiness import Readiness

    both = mcp.readiness_of(lambda: Readiness(True), lambda: Readiness(False, ("x",)))()
    assert (both.ready, both.reasons) == (False, ("x",))
    assert mcp.readiness_of(lambda: Readiness(True))().ready


def test_run_main(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = mcp.Logs.of("kit_main")
    served: list[Any] = []
    config = uvicorn.Config(lambda *_: None)
    assert mcp.run_main(lambda: [config], served.append, logs) == 0 and served == [[config]]

    def refuse() -> list[uvicorn.Config]:
        raise ConfigError("nope")

    caplog.set_level(logging.ERROR)
    assert mcp.run_main(refuse, served.append, logs) == 2
    assert [r.getMessage() for r in caplog.records if r.name == "kit_main"] == [
        json.dumps({"event": "startup_refused", "reason": "nope"})
    ]
    for exc in (RuntimeError("r"), ValueError("v"), OSError("o")):

        def fail(e: Exception = exc) -> list[uvicorn.Config]:
            raise e

        assert mcp.run_main(fail, served.append, logs) == 2
    assert len(served) == 1


def test_setup_logging_routes_frames_to_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        logs = mcp.Logs.of("kit_route")
        mcp.setup_logging(logs)
        logs.audit.info("audit-line")
        logs.errors.error("frames-line")
        out, err = capfd.readouterr()
    finally:
        root.handlers, root.level = saved[0], saved[1]
    assert "audit-line" in out and "frames-line" not in out
    assert "frames-line" in err and "audit-line" not in err
    assert out.strip().splitlines()[-1] == "audit-line"  # the message only, no level or logger name


def test_access_defaults() -> None:
    from gxg_connector_kit.identity import VerifiedIdentity

    now = datetime.now(UTC)
    a = Access(VerifiedIdentity("t", "agent:t:a", now, now, "r", None, "signed_assertion"), "read", None)
    assert a.nothing and not a.check("kit:c/box/b").allowed and a.rid is None and a.target is None


async def test_resolve_and_handler_get_the_whole_access(world: World) -> None:
    async with client(world.app()) as (http, s):
        await call(world, s, http, "kit.box.get", {"box_id": "b1", "meta": {"note": "n"}})
    (tool, before, rargs), (what, after, hargs) = world.domain.calls
    assert (tool, what) == ("kit.box.get", "handler") and rargs == hargs == {"box_id": "b1", "meta": {"note": "n"}}
    for a in (before, after):
        assert (a.ident.principal, a.ident.tenant_id, a.action, a.connection) == (READER, "t-alpha", "read", "kit-main")
        assert a.allowed.rids == frozenset({"kit:kit-main/box/b1"}) and not a.allowed.all_of_type
        assert a.check("kit:kit-main/box/b1").allowed and not a.check("kit:kit-main/box/b2").allowed
    assert (before.rid, before.grant_id, before.target) == (None, None, None)
    assert (after.rid, after.grant_id, after.target) == ("kit:kit-main/box/b1", "g-read", "target-b1")


async def test_verifier_gets_the_tool_and_its_arguments(world: World) -> None:
    seen: list[tuple[Any, ...]] = []
    real = mcp.verifier(world.settings, "kit")

    class Recording:
        def verify(self, headers: Mapping[str, str], *, tool: str | None = None,
                   arguments: Mapping[str, Any] | None = None) -> Any:  # fmt: skip
            seen.append((tool, arguments, "x-gxg-test-identity" in headers))
            return real.verify(headers, tool=tool, arguments=arguments)

    store, files = mcp.bundle_store(world.settings)
    tools = mcp.tools(world.domain, world.tmp / "gxg-manifest.yaml", store, Recording(), files.refresh,
                      mcp.Logs.of("kit_test"))  # fmt: skip
    async with client(mcp.create_app("kit", tools, files.refresh, store.readiness)) as (http, s):
        await call(world, s, http, "kit.box.get", {"box_id": "b1"})
    assert seen == [("kit.box.get", {"box_id": "b1"}, True)]


async def test_audit_lines_exactly(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    world.domain.delay_s = 0.3
    async with client(world.app()) as (http, s):
        headers = world.headers(READER)
        http.headers.update(headers)
        await s.call_tool("kit.box.get", {"box_id": "b1"})
        for k in headers:
            del http.headers[k]
        await call(world, s, http, "kit.box.get", {"box_id": "b1", "meta": {"note": 1}})
        await call(world, s, http, "kit.box.get", {"box_id": "b1"}, principal=None)
    ok_line, bad, anon = audit(caplog)
    jti = json.loads(__import__("base64").urlsafe_b64decode(headers["x-gxg-test-identity"].split(".")[1] + "=="))["jti"]
    assert 250 <= ok_line.pop("duration_ms") <= 1500
    assert ok_line == {"request_id": jti, "tool": "kit.box.get", "tenant": "t-alpha", "principal": READER,
                       "grant_id": ["g-read"], "rid": "kit:kit-main/box/b1", "outcome": "ok"}  # fmt: skip
    assert bad["outcome"] == "invalid_input" and set(bad) == {"request_id", "tool", "tenant", "principal", "outcome",
                                                               "duration_ms"}  # fmt: skip
    assert len(anon.pop("request_id")) == 36 and anon.pop("duration_ms") < 250
    assert anon == {"tool": "kit.box.get", "outcome": "unauthenticated", "reason": "missing"}


async def test_invalid_input_names_the_nested_path(world: World) -> None:
    async with client(world.app()) as (http, s):
        r = await call(world, s, http, "kit.box.get", {"box_id": "b1", "meta": {"note": 1}})
    assert r["structuredContent"]["details"] == {"path": "/meta/note"}


async def test_output_errors_are_named_in_the_audit(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    async with client(world.app()) as (http, s):
        world.domain.reply = {"no_id": 1}
        await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.reply = {"id": "x" * 191}
        await call(world, s, http, "kit.box.get", {"box_id": "b1"})
        world.domain.reply, world.domain.fail = None, RuntimeError("x")
        await call(world, s, http, "kit.box.get", {"box_id": "b1"})
    assert [a["error"] for a in audit(caplog)] == ["OutputSchemaError", "OutputTooLarge", "RuntimeError"]
    frames = [r.getMessage() for r in caplog.records if r.name == "kit_test.errors"][-1].split("\n")
    assert len(frames) >= 3 and all(re.fullmatch(r"/\S+\.py:\d+ in \S+", f) for f in frames[1:]), frames


async def test_unknown_tenant_without_bundle_is_unavailable_too(world: World) -> None:
    settings = replace(world.settings, bundle_dir=world.tmp / "none")
    async with client(world.app(settings)) as (http, s):
        r = await call(world, s, http, "kit.boxes.list", {}, principal="agent:t-beta:reader")
    assert r["structuredContent"]["code"] == "temporarily_unavailable"


async def test_stateless_json_endpoint_and_server_name(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    events = logging.getLogger("kit_events")
    store, files = mcp.bundle_store(replace(world.settings, bundle_dir=world.tmp / "none"))
    tools = mcp.tools(world.domain, world.tmp / "gxg-manifest.yaml", store, mcp.verifier(world.settings, "kit"),
                      files.refresh, mcp.Logs.of("kit_test"))  # fmt: skip
    app = mcp.create_app("kit-name", tools, files.refresh, store.readiness, events)
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        h = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://k")
        )
        r = await h.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init}
        )
        assert r.headers["content-type"].startswith("application/json") and "mcp-session-id" not in r.headers
        assert r.json()["result"]["serverInfo"]["name"] == "kit-name"
        call_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "kit.boxes.list", "arguments": {}}}  # fmt: skip
        r = await h.post("/mcp", headers={**headers, **world.headers(READER)}, json=call_body)  # no session
        assert r.status_code == 200 and r.json()["result"]["structuredContent"]["code"] == "temporarily_unavailable"
        assert (await h.get("/readyz")).status_code == 503
    [event] = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kit_events"]
    assert event["event"] == "not_ready" and event["reasons"]


def test_serve_runs_every_config(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[Any] = []

    class Server:
        def __init__(self, config: Any) -> None:
            self.config = config

        async def serve(self) -> None:
            served.append(self.config)

    monkeypatch.setattr(uvicorn, "Server", Server)
    configs = [uvicorn.Config(lambda *_: None, port=1), uvicorn.Config(lambda *_: None, port=2)]
    mcp.serve(configs)
    assert served == configs


def test_run_main_sets_up_logging(capfd: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        logs = mcp.Logs.of("kit_rm")
        assert mcp.run_main(lambda: [], lambda _c: None, logs) == 0
        logs.audit.info("rm-audit")
        out, _ = capfd.readouterr()
    finally:
        root.handlers, root.level = saved[0], saved[1]
    assert "rm-audit" in out


def test_verifier_and_store_name_their_settings(world: World, tmp_path: Path) -> None:
    cf = replace(world.settings, test_issuer_key=None, identity_keys="bad")
    with pytest.raises(ConfigError, match=r"^GXG_IDENTITY_KEYS: "):
        mcp.verifier(cf, "kit")
    key = bundle.public(Ed25519PrivateKey.generate().public_key())
    v = mcp.verifier(replace(cf, identity_keys=f"a={key}"), "kit-aud")
    assert getattr(v, "_connector_id", getattr(v, "_audience", None)) in ("kit-aud", "gxg-connector:kit-aud")
    with pytest.raises(ConfigError, match=r"^GXG_BUNDLE_KEYS: "):
        mcp.bundle_store(replace(world.settings, bundle_keys="bad"))
    deep = replace(world.settings, state_file=tmp_path / "a" / "b" / "state.json")
    mcp.bundle_store(deep)
    mcp.bundle_store(deep)  # the directory exists now: fine
    assert (tmp_path / "a" / "b").is_dir()


def test_bundle_rejected_event_has_the_reason(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    (world.tmp / "bundle" / mcp.SIG_FILE).write_bytes(b"{}")
    mcp.bundle_store(world.settings, logging.getLogger("kit_rej"))
    [event] = [json.loads(r.getMessage()) for r in caplog.records if r.name == "kit_rej"]
    assert event == {"event": "bundle_rejected", "reason": "signature: alg must be EdDSA"}


def test_cursor_offsets_differ() -> None:
    c = mcp.Cursor()
    assert c.make(b"q", 20) != c.make(b"q", 40) and c.offset(b"q", c.make(b"q", 40)) == 40
    forged = "40." + c.make(b"q", 20).split(".")[1]  # the MAC covers the offset too
    with pytest.raises(GxgError):
        c.offset(b"q", forged)


def test_schema_tail_is_found_where_dot_dot_does_not_reach(tmp_path: Path) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "x.json").write_text('{"b": 2}', encoding="utf-8")
    (tmp_path / "c").mkdir()
    assert mcp.load_schema(tmp_path / "c", "../../schemas/x.json") == {"b": 2}


def test_manifest_needs_a_timeout_and_bounded_limit(tmp_path: Path) -> None:
    write_manifest(tmp_path, [{**TOOLS[1], "limits": {"max_bytes": 10}}])
    with pytest.raises(ConfigError, match=r"limits.timeout_s is required \(MCC-08\)$"):
        mcp.load_manifest(tmp_path / "gxg-manifest.yaml")
    write_manifest(tmp_path, [{k: v for k, v in TOOLS[1].items() if k != "limits"}])
    with pytest.raises(ConfigError, match=r"limits\.timeout_s is required"):
        mcp.load_manifest(tmp_path / "gxg-manifest.yaml")
    noprops = {**SCHEMAS, "list.in.json": {"description": "d", "type": "object"}}
    write_manifest(tmp_path, [TOOLS[0]], noprops)
    with pytest.raises(ConfigError, match="maximum"):
        mcp.load_manifest(tmp_path / "gxg-manifest.yaml")


def test_tools_read_schemas_from_root_and_descriptions(world: World, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp("elsewhere")  # not under world.tmp: its parents have no schemas/
    (other / "gxg-manifest.yaml").write_bytes((world.tmp / "gxg-manifest.yaml").read_bytes())
    store, files = mcp.bundle_store(world.settings)
    tools = mcp.tools(world.domain, other / "gxg-manifest.yaml", store, mcp.NoVerifier(), files.refresh,
                      mcp.Logs.of("x"), world.tmp)  # fmt: skip
    assert [t.description for t in tools] == ["List boxes", "Get a box", "Get a box"]
    with pytest.raises(ConfigError, match="schema not found"):
        mcp.tools(world.domain, other / "gxg-manifest.yaml", store, mcp.NoVerifier(), files.refresh, mcp.Logs.of("x"))
    with pytest.raises(ConfigError, match="schema not found"):
        mcp.load_manifest(other / "gxg-manifest.yaml")


async def test_published_schemas_are_the_files(world: World) -> None:
    async with client(world.app()) as (_http, s):
        listed = {t.name: t for t in (await s.list_tools()).tools}
    for t in TOOLS:
        name, inp, out = str(t["name"]), str(t["input_schema"]), str(t["output_schema"])
        assert listed[name].input_schema == SCHEMAS[inp.split("/")[-1]], name
        assert listed[name].output_schema == SCHEMAS[out.split("/")[-1]], name
