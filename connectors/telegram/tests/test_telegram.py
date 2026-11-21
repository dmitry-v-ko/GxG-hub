"""Telegram connector, read-only (milestone M11): AC-M11-01…05 in-process; the same through ContextForge in
stand/tests/test_telegram.py.

The MCP client is the official SDK over Streamable HTTP on httpx2's ASGITransport (no TCP socket). The bundle is
built by `gxg-policy build` from fixtures/grants/ with a key made here; identities come from the kit's test issuer.
The service is the fake of fixtures/service.json behind the `TelegramService` port: no real Telegram account.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gxg_connector_kit import TokenError
from gxg_connector_kit import cf_identity as cf
from gxg_connector_kit.identity import StubIssuer
from gxg_policy import cli as policy_cli
from telegram_connector import server, testctl, tools
from telegram_connector.fake import FakeTelegramService
from telegram_connector.service import ServiceError
from telegram_connector.tools import Connector

pytestmark = pytest.mark.anyio

REPO = next(p for p in Path(__file__).resolve().parents if (p / "policies").is_dir())
TG = REPO / "connectors" / "telegram"
ALPHA_READER, ALPHA_NOBODY, BETA_READER = "agent:t-alpha:reader", "agent:t-alpha:nobody", "agent:t-beta:reader"
CONNECTIONS = "t-alpha=tg-main:svc-alpha,t-beta=tg-main:svc-beta"
KID = "telegram-test"
FIXTURES = json.loads((TG / "fixtures" / "service.json").read_text(encoding="utf-8"))
REF = {ch["name"]: ch["chat_ref"] for c in FIXTURES["connections"].values() for ch in c["chats"]}
FORGED = "tgc1_" + "A" * 32  # well-formed, issued for no chat
NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}
SEARCH_RESULT = json.loads((REPO / "schemas" / "search-result.schema.json").read_text())
ERROR = json.loads((REPO / "schemas" / "error.schema.json").read_text())
MESSAGES = json.loads((TG / "schemas" / "messages.output.json").read_text())


def public_key(key: Any) -> str:
    return "ed25519:" + base64.urlsafe_b64encode(key.public_bytes_raw()).rstrip(b"=").decode()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_bundle(out: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = out.parent / f"{out.name}-policies"
    (root / "tenants").mkdir(parents=True)
    (root / "platform").mkdir()
    for f in (TG / "fixtures" / "grants").glob("t-*.yaml"):
        shutil.copy(f, root / "tenants" / f.name)
    shutil.copy(REPO / "policies" / "platform" / "registry.example.yaml", root / "platform")
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    monkeypatch.setenv(policy_cli.KEY_ENV, base64.urlsafe_b64encode(seed).rstrip(b"=").decode())
    args = ["build", str(root), "--out", str(out), "--bundle-version", "0" * 40, "--kid", KID]
    assert policy_cli.main([*args, "--manifest", str(TG / "gxg-manifest.yaml")]) == 0
    return f"{KID}={public_key(Ed25519PrivateKey.from_private_bytes(seed).public_key())}"


def env_of(tmp: Path, keys: str, issuer: StubIssuer, **extra: str) -> dict[str, str]:
    return {
        "GXG_PROFILE": "test",
        "TELEGRAM_SERVICE": "fake",
        "TELEGRAM_CONNECTIONS": CONNECTIONS,
        "TELEGRAM_BUNDLE_DIR": str(tmp / "bundle"),
        "TELEGRAM_BUNDLE_KEYS": keys,
        "TELEGRAM_STATE_FILE": str(tmp / "state" / "bundle-state.json"),
        "TELEGRAM_TEST_ISSUER_KEY": public_key(issuer.public_key),
    } | extra


@dataclass
class Env:
    settings: server.Settings
    issuer: StubIssuer
    raw: dict[str, str]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("GXG_PROFILE", "test")
    issuer = StubIssuer()
    raw = env_of(tmp_path, build_bundle(tmp_path / "bundle", monkeypatch), issuer)
    return Env(server.Settings.from_env(raw), issuer, raw)


class Tg:
    def __init__(self, connector: Connector, http: httpx2.AsyncClient, session: ClientSession, issuer: StubIssuer):
        self.connector, self.http, self.session, self.issuer = connector, http, session, issuer
        assert isinstance(connector.service, FakeTelegramService)
        self.service: FakeTelegramService = connector.service

    def identity(self, principal: str, **kw: Any) -> dict[str, str]:
        return self.issuer.headers(
            tenant_id=principal.split(":")[1], principal=principal, **({"audience": "telegram"} | kw)
        )

    async def call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        principal: str = ALPHA_READER,
        identity: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        extra = {**(self.identity(principal) if identity is None else identity), **(headers or {})}
        self.http.headers.update(extra)
        try:
            result = await self.session.call_tool(tool, args or {}, meta=meta)  # type: ignore[arg-type]
        finally:
            for k in extra:
                del self.http.headers[k]
        dumped: dict[str, Any] = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        return dumped

    async def ok(self, tool: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        r = await self.call(tool, args, **kw)
        assert r.get("isError") is not True, r
        content: dict[str, Any] = r["structuredContent"]
        return content

    async def error(self, tool: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        r = await self.call(tool, args, **kw)
        assert r["isError"] is True, r
        jsonschema.validate(r["structuredContent"], ERROR)
        err: dict[str, Any] = r["structuredContent"]
        return err


async def connect(stack: AsyncExitStack, app: Any) -> tuple[httpx2.AsyncClient, ClientSession]:
    await stack.enter_async_context(app.router.lifespan_context(app))
    transport = httpx2.ASGITransport(app=app)
    http = await stack.enter_async_context(httpx2.AsyncClient(transport=transport, base_url="http://telegram"))
    read, write = await stack.enter_async_context(streamable_http_client("http://telegram/mcp", http_client=http))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return http, session


@pytest.fixture
async def tg(env: Env) -> AsyncIterator[Tg]:
    app, connector = server.build(env.settings, FakeTelegramService())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        yield Tg(connector, http, session, env.issuer)


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "telegram_connector.audit"]


def texts_of(service: FakeTelegramService, chat: str) -> list[str]:
    ch = next(c for chats in service.chats.values() for c in chats.values() if c.name == chat)
    return [m.text or m.caption or "" for m in ch.messages]


def search_ids(content: dict[str, Any]) -> list[str]:
    return [i["id"] for i in content["items"]]


# health, configuration -------------------------------------------------------------------------


async def test_healthz_and_readyz(tg: Tg) -> None:
    assert (await tg.http.get("/healthz")).status_code == 200
    r = await tg.http.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})
    tg.service.failure = "auth_required"
    r = await tg.http.get("/readyz")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})  # MCC-09: no reasons in the body


async def test_readyz_503_and_calls_refused_without_a_valid_bundle(env: Env) -> None:
    (env.settings.bundle_dir / "grants-bundle.json.sig").write_text("{}")
    app, connector = server.build(env.settings, FakeTelegramService())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        assert (await http.get("/readyz")).status_code == 503
        t = Tg(connector, http, session, env.issuer)
        err = await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]})
        assert (err["code"], err["retryable"]) == ("temporarily_unavailable", True)
        assert t.service.calls.total() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"TELEGRAM_SERVICE": ""},  # no adapter to the real service yet: never a silent fake
        {"TELEGRAM_SERVICE": "http"},
        {"GXG_PROFILE": "production", "TELEGRAM_TEST_ISSUER_KEY": ""},  # the fake only in the test profile
        {"GXG_PROFILE": "production", "TELEGRAM_TEST_CONTROLS": "1"},
        {"TELEGRAM_IDENTITY_KEYS": "k=ed25519:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},  # with the test issuer
        {"TELEGRAM_CONNECTIONS": "t-alpha=tg-main"},
        {"TELEGRAM_CONNECTIONS": "t-alpha=tg-main:svc-alpha,t-alpha=tg-main:svc-beta"},
    ],
)
def test_settings_refuse_to_start(env: Env, change: dict[str, str]) -> None:
    with pytest.raises(server.ConfigError):
        server.Settings.from_env(env.raw | change)


def test_main_refuses_without_the_fake_and_logs_why(caplog: pytest.LogCaptureFixture) -> None:
    assert server.main({"GXG_PROFILE": "production"}) == 2
    line = json.loads(caplog.records[-1].getMessage())
    assert line["event"] == "startup_refused" and "TELEGRAM_SERVICE" in line["reason"]


def test_connections_and_keys_parse() -> None:
    assert server.parse_connections(" t-a=tg-main:svc.a:1 , t-b=c2:x") == {
        "t-a": ("tg-main", "svc.a:1"),
        "t-b": ("c2", "x"),
    }
    assert server.parse_connections("") == {}
    with pytest.raises(server.ConfigError, match="TELEGRAM_BUNDLE_KEYS"):
        server.parse_keys("nokey", "TELEGRAM_BUNDLE_KEYS")
    with pytest.raises(server.ConfigError, match="X"):
        server.parse_keys("k=ed25519:short", "X")


async def test_no_identity_keys_refuse_every_call(env: Env) -> None:
    settings = server.Settings.from_env(env.raw | {"TELEGRAM_TEST_ISSUER_KEY": ""})
    app, connector = server.build(settings, FakeTelegramService())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        t = Tg(connector, http, session, env.issuer)
        assert (await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]}))["code"] == "unauthenticated"
        assert t.service.calls.total() == 0


def test_identity_keys_select_the_contextforge_verifier(env: Env) -> None:
    key = public_key(Ed25519PrivateKey.generate().public_key())
    settings = server.Settings.from_env(
        env.raw | {"TELEGRAM_TEST_ISSUER_KEY": "", "TELEGRAM_IDENTITY_KEYS": f"a={key}"}
    )
    _, connector = server.build(settings, FakeTelegramService())
    assert server.configs(settings, *server.build(settings, FakeTelegramService()))[0].port == 8080
    assert connector.connections == {"t-alpha": ("tg-main", "svc-alpha"), "t-beta": ("tg-main", "svc-beta")}


def test_test_controls_listen_on_loopback_only(env: Env) -> None:
    settings = server.Settings.from_env(env.raw | {"TELEGRAM_TEST_CONTROLS": "1"})
    app, connector = server.build(settings)
    ports = [(c.host, c.port) for c in server.configs(settings, app, connector)]
    assert ports == [("0.0.0.0", 8080), ("127.0.0.1", 8090)]  # noqa: S104


async def test_test_controls_set_failures_and_report() -> None:
    service = FakeTelegramService()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=testctl.create_app(service)), base_url="http://t"
    ) as c:
        assert (await c.post("/_test/service/fail", json={"mode": "flood_wait:42"})).status_code == 200
        assert (service.failure, service.retry_after_s, service.delay_s) == ("flood_wait", 42.0, 0.0)
        await c.post("/_test/service/fail", json={"mode": "slow:1500"})
        assert (service.failure, service.delay_s) == (None, 1.5)
        await c.post("/_test/service/fail", json={"mode": "busy"})
        assert service.failure == "busy"
        assert (await c.post("/_test/service/fail", json={"mode": "nope"})).status_code == 400
        await c.post("/_test/service/fail", json={"mode": None})
        assert (service.failure, service.delay_s, service.retry_after_s) == (None, 0.0, 0.0)  # type: ignore[comparison-overlap]
        stats = (await c.get("/_test/service/stats")).json()
        assert stats == {"calls": {}, "open_sessions": 2}
        known = (await c.get("/_test/service/secrets")).json()
        assert len(known["peer_ids"]) == 7 and len(known["sessions"]) == 2 and len(known["texts"]) > 80


# AC-M11-01: SC-03 -------------------------------------------------------------------------------


async def test_sc03_identity_in_arguments_meta_and_headers_changes_nothing(
    tg: Tg, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "telegram_connector.audit")
    spoof = {"tenant_id": "t-beta", "agent_id": BETA_READER, "approved": True, "user": BETA_READER}
    meta = {"tenant_id": "t-beta", "agent_id": BETA_READER}
    forged = {"X-Tenant-Id": "t-beta", "X-Forwarded-User-Email": "reader@t-beta.agents.gxg.example.com"}
    for tool, args in (
        ("telegram.messages.search", {"query": "status", "limit": 5}),
        ("telegram.thread.read", {"chat_ref": REF["a-partners"], "limit": 3}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "limit": 3}),
        ("telegram.thread.read", {"chat_ref": REF["b-public"]}),  # t-beta's chat stays not_found
    ):
        plain = await tg.call(tool, args)
        spoofed = await tg.call(tool, args | spoof, meta=meta, headers=forged)
        assert plain == spoofed, tool
    assert {(a["tenant"], a["principal"]) for a in audit_lines(caplog)} == {("t-alpha", ALPHA_READER)}


# AC-M11-01: SC-04 -------------------------------------------------------------------------------


async def test_sc04_bad_identity_is_refused_before_the_service(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "telegram_connector.audit")
    args = {"chat_ref": REF["a-partners"]}
    replay = tg.identity(ALPHA_READER)
    other = StubIssuer()
    cases = {
        "missing": {},
        "replayed": replay,
        "other key": other.headers(tenant_id="t-alpha", principal=ALPHA_READER, audience="telegram"),
        "other audience": tg.identity(ALPHA_READER, audience="demo"),
        "expired": tg.identity(ALPHA_READER, ttl_s=-60),
        "garbage": {"x-gxg-test-identity": "W10.e30.AA"},
    }
    assert (await tg.ok("telegram.thread.read", args, identity=replay))["messages"]
    calls = tg.service.calls.total()
    for name, identity in cases.items():
        err = await tg.error("telegram.thread.read", args, identity=identity)
        assert err == {"code": "unauthenticated", "message": "token rejected", "retryable": False}, name
    assert tg.service.calls.total() == calls  # refused before any request to the service
    assert all(a["outcome"] == "unauthenticated" and a.get("reason") for a in audit_lines(caplog)[1:])


# AC-M11-01: SC-05, SC-10; AC-M11-03 -------------------------------------------------------------


UNREADABLE = {
    "other tenant's chat": REF["b-public"],
    "no grant (in the service scope)": REF["a-hidden"],
    "expired grant": REF["a-secret"],
    "forged chat_ref": FORGED,
}


async def test_ac_m11_03_unauthorized_chat_never_reaches_the_service(tg: Tg) -> None:
    """Every tool, every kind of unreadable chat: `not_found`, byte-identical, 0 requests to the service port."""
    replies = []
    for ref in UNREADABLE.values():
        for tool, args in (
            ("telegram.thread.read", {"chat_ref": ref}),
            ("telegram.messages.collect", {"chat_ref": ref}),
            ("telegram.messages.search", {"query": "status", "chat_refs": [REF["a-partners"], ref]}),
        ):
            replies.append(await tg.call(tool, args))
    # collect needs its own grant: read on a-sales is not enough
    replies.append(await tg.call("telegram.messages.collect", {"chat_ref": REF["a-sales"]}))
    replies.append(await tg.call("telegram.thread.read", {"chat_ref": REF["a-partners"]}, principal=ALPHA_NOBODY))
    assert len({json.dumps(r, sort_keys=True) for r in replies}) == 1
    assert replies[0]["structuredContent"] == NOT_FOUND and replies[0]["isError"] is True
    assert tg.service.calls.total() == 0


async def test_sc05_chat_outside_the_service_scope_is_the_same_not_found(tg: Tg) -> None:
    """A mask grant (t-beta) admits any chat_ref; the service refuses one outside its scope with chat_forbidden."""
    beta = [
        await tg.call("telegram.thread.read", {"chat_ref": r}, principal=BETA_READER)
        for r in (REF["a-partners"], FORGED)
    ]
    alpha = await tg.call("telegram.thread.read", {"chat_ref": REF["a-hidden"]})
    assert beta[0] == beta[1] == alpha and alpha["structuredContent"] == NOT_FOUND
    assert tg.service.calls["read_thread"] == 2


async def test_sc10_search_sees_only_granted_chats_without_hidden_counts(tg: Tg) -> None:
    got = await tg.ok("telegram.messages.search", {"query": "budget", "limit": 50})
    want = {REF["a-partners"], REF["a-sales"], REF["a-gap"]}
    assert {i["id"].split(":")[0] for i in got["items"]} == want, len(got["items"])
    assert set(got["source_refs"]) == {f"telegram:tg-main/chat/{r}" for r in want}
    assert set(got) == {"items", "next_cursor", "complete", "partial_reasons", "source_refs"}  # no total
    assert got["partial_reasons"] == ["other"] and got["complete"] is False  # a-gap: backfilling with a gap
    # GR-16: the service was asked about the granted chats only
    seen: list[list[str]] = []
    orig = tg.service.search

    async def spy(connection: str, query: str, chat_refs: list[str], limit: int) -> Any:
        seen.append(chat_refs)
        return await orig(connection, query, chat_refs, limit)

    tg.service.search = spy  # type: ignore[method-assign]
    await tg.ok("telegram.messages.search", {"query": "status"})
    assert seen == [sorted(want)]


async def test_sc10_nothing_granted_means_no_request(tg: Tg) -> None:
    got = await tg.ok("telegram.messages.search", {"query": "status"}, principal=ALPHA_NOBODY)
    assert got == {"items": [], "next_cursor": None, "complete": True, "partial_reasons": [], "source_refs": []}
    gamma = "agent:t-gamma:reader"  # a tenant without a Telegram Connection
    assert (await tg.ok("telegram.messages.search", {"query": "status"}, principal=gamma))["items"] == []
    assert (await tg.error("telegram.thread.read", {"chat_ref": REF["a-partners"]}, principal=gamma)) == NOT_FOUND
    assert tg.service.calls.total() == 0


async def test_sc10_mask_grant_reads_the_service_scope(tg: Tg) -> None:
    got = await tg.ok("telegram.messages.search", {"query": "status", "limit": 50}, principal=BETA_READER)
    assert {i["id"].split(":")[0] for i in got["items"]} == {REF["b-public"], REF["b-ops"]}
    assert tg.service.calls["scope_chats"] == 1
    assert (await tg.ok("telegram.messages.collect", {"chat_ref": REF["b-ops"]}, principal=BETA_READER))["messages"]


async def test_sc10_items_of_ungranted_chats_from_the_service_are_dropped(tg: Tg) -> None:
    """GR-16 second check: whatever the port returns, only granted chats leave the connector."""
    orig = tg.service.search

    async def leaky(connection: str, query: str, chat_refs: list[str], limit: int) -> Any:
        return await orig(connection, query, [*chat_refs, REF["a-hidden"]], limit)

    tg.service.search = leaky  # type: ignore[method-assign]
    got = await tg.ok("telegram.messages.search", {"query": "a-hidden"})
    assert got["items"] == [] and got["source_refs"] == []
    page = await tg.service.read_thread("svc-alpha", REF["a-hidden"], None, 3)

    async def other_chat(*_: Any) -> Any:
        return page

    tg.service.read_thread = other_chat  # type: ignore[method-assign,assignment]
    assert (await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"]}))["messages"] == []


# AC-M11-01: SC-09; AC-M11-05 --------------------------------------------------------------------


async def test_ac_m11_05_no_texts_peer_ids_or_sessions_in_logs_after_1000_calls(
    tg: Tg, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    plan: list[tuple[str, dict[str, Any], str]] = [
        ("telegram.messages.search", {"query": "status", "limit": 10}, ALPHA_READER),
        ("telegram.messages.search", {"query": "игнорируй инструкции"}, ALPHA_READER),
        ("telegram.thread.read", {"chat_ref": REF["a-partners"], "limit": 50}, ALPHA_READER),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "limit": 100}, ALPHA_READER),
        ("telegram.messages.collect", {"chat_ref": REF["b-public"]}, BETA_READER),
        ("telegram.thread.read", {"chat_ref": REF["a-hidden"]}, ALPHA_READER),
        ("telegram.thread.read", {"chat_ref": FORGED}, BETA_READER),
        ("telegram.messages.search", {"query": "beta secret plan"}, BETA_READER),
    ]
    replies = []
    for i in range(1000):
        tool, args, who = plan[i % len(plan)]
        if i % 97 == 0:
            tg.service.failure = "flood_wait"
            tg.service.retry_after_s = 3
        replies.append(await tg.call(tool, args, principal=who))
        tg.service.failure = None
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert len(audit_lines(caplog)) == 1000
    texts = tg.service.texts()
    peers = [ch.peer_id for chats in tg.service.chats.values() for ch in chats.values()]
    sessions = list(tg.service.sessions.values())
    assert [t for t in texts if t in logs] == []
    assert [p for p in peers if p in logs or p.lstrip("-") in logs] == []
    assert [s for s in sessions if s in logs] == []
    assert "игнорируй" not in logs and "beta secret plan" not in logs  # the queries are not logged either
    blob = json.dumps(replies, ensure_ascii=False)
    assert [p for p in peers if p in blob] == [] and [s for s in sessions if s in blob] == []  # MCC-04
    assert texts_of(tg.service, "b-public")[-1] in blob  # the replies do carry texts: the scan is meaningful


async def test_sc09_internal_error_keeps_text_out_of_logs_and_reply(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    leak = texts_of(tg.service, "a-partners")[0]

    async def boom(*_: Any) -> Any:
        raise RuntimeError(f"peer -1001000000101 said {leak}")

    tg.service.read_thread = boom  # type: ignore[method-assign,assignment]
    r = await tg.call("telegram.thread.read", {"chat_ref": REF["a-partners"]})
    assert r["structuredContent"] == {"code": "internal_error", "message": "internal error", "retryable": False}
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert leak not in logs and "1001000000101" not in logs and "RuntimeError" in logs


async def test_authorization_header_reaching_the_connector_is_audited(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    """backlog M5 (2): the stand asserts `authorization_header: false` on every call that ContextForge forwards."""
    caplog.set_level(logging.INFO, "telegram_connector.audit")
    await tg.call("telegram.thread.read", {"chat_ref": REF["a-partners"]})
    await tg.call(
        "telegram.thread.read", {"chat_ref": REF["a-partners"]}, headers={"Authorization": "Bearer agent-tok"}
    )
    lines = audit_lines(caplog)
    assert [a["authorization_header"] for a in lines] == [False, True]
    assert "agent-tok" not in "\n".join(r.getMessage() for r in caplog.records)


# SEC-11, CON-TG-08: texts are data ---------------------------------------------------------------


async def test_control_characters_are_escaped_and_text_is_marked_untrusted(tg: Tg) -> None:
    got = await tg.ok("telegram.messages.search", {"query": "bidi"})
    (item,) = got["items"]
    assert item["excerpt"] == "bidi \\u{202E}evil\\u{202C} and zero\\u{200B}width\\u{0007} control"
    thread = await tg.ok("telegram.thread.read", {"chat_ref": REF["a-partners"], "limit": 3})
    assert thread["untrusted"] is True
    assert "\u202e" not in json.dumps(thread, ensure_ascii=False)
    assert tools.escape("a\nb\tc\u200d\x00") == "a\nb\tc\\u{200D}\\u{0000}"


async def test_sc08_injection_is_returned_as_data_and_no_effect_tool_exists(tg: Tg) -> None:
    got = await tg.ok("telegram.messages.search", {"query": "игнорируй инструкции"})
    assert [i["excerpt"] for i in got["items"]] == [texts_of(tg.service, "a-partners")[-2]]
    names = [t.name for t in (await tg.session.list_tools()).tools]
    assert names == ["telegram.messages.search", "telegram.thread.read", "telegram.messages.collect"]


async def test_long_text_is_truncated_and_flagged(tg: Tg) -> None:
    chat = tg.service.chats["svc-alpha"][REF["a-sales"]]
    import random

    tg.service._append("svc-alpha", REF["a-sales"], "x" * 5000, random.Random(1))  # noqa: S311
    thread = await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 2})
    first, second = thread["messages"]
    assert (len(first["text"]), first["truncated"], second["truncated"]) == (4000, True, False)
    assert first["message_id"] == chat.messages[-1].message_id
    got = await tg.ok("telegram.messages.search", {"query": "xxxx", "chat_refs": [REF["a-sales"]]})
    assert len(got["items"][0]["excerpt"]) == 500


# AC-M11-01: MC-02 -------------------------------------------------------------------------------


async def test_mc02_search_schema_pagination_and_cursor_binding(tg: Tg) -> None:
    out_schema = tools.load_schema("../../schemas/search-result.schema.json")
    seen: list[str] = []
    cursor = None
    for _ in range(30):
        page = await tg.ok(
            "telegram.messages.search", {"query": "status", "limit": 7} | ({"cursor": cursor} if cursor else {})
        )
        jsonschema.validate(page, SEARCH_RESULT)
        jsonschema.validate(page, out_schema)
        assert len(page["items"]) <= 7
        seen += search_ids(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    full = await tg.ok("telegram.messages.search", {"query": "status", "limit": 50})
    assert len(seen) == len(set(seen)) > 50 and seen[:50] == search_ids(full)
    first = await tg.ok("telegram.messages.search", {"query": "status", "limit": 7})
    for other in ({"query": "open", "limit": 7}, {"query": "status", "limit": 8}):
        err = await tg.error("telegram.messages.search", other | {"cursor": first["next_cursor"]})
        assert err["code"] == "invalid_input"
    err = await tg.error(
        "telegram.messages.search",
        {"query": "status", "limit": 7, "cursor": first["next_cursor"]},
        principal=BETA_READER,
    )
    assert err["code"] == "invalid_input"  # bound to the caller
    assert (await tg.error("telegram.messages.search", {"query": "status", "cursor": "1.zz"}))[
        "code"
    ] == "invalid_input"


async def test_mc02_search_stops_at_the_service_limit_with_truncated(tg: Tg) -> None:
    """The service returns at most 100 ranked results: the last page says so instead of inventing more."""
    cursor, pages = None, 0
    while True:
        page = await tg.ok(
            "telegram.messages.search", {"query": "message", "limit": 50} | ({"cursor": cursor} if cursor else {})
        )
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 2 and page["partial_reasons"] == ["other", "truncated"] and page["complete"] is False
    sales = await tg.ok("telegram.messages.search", {"query": "message", "chat_refs": [REF["a-sales"]], "limit": 50})
    assert (sales["partial_reasons"], sales["next_cursor"], len(sales["items"])) == ([], None, 12)


async def test_mc02_valid_empty_result_is_success(tg: Tg) -> None:
    empty = await tg.ok("telegram.messages.search", {"query": "zzz-nothing-matches", "chat_refs": [REF["a-sales"]]})
    assert empty == {"items": [], "next_cursor": None, "complete": True, "partial_reasons": [], "source_refs": []}
    assert tg.service.calls["search"] == 1  # one request, no retry or fallback


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("telegram.messages.search", {"query": "status", "limit": 51}),
        ("telegram.messages.search", {"query": ""}),
        ("telegram.messages.search", {"query": "   "}),
        ("telegram.messages.search", {"query": "x" * 201}),
        ("telegram.messages.search", {"query": "x", "chat_refs": [REF["a-sales"]] * 2}),
        ("telegram.messages.search", {"query": "x", "chat_refs": ["-1001000000101"]}),
        ("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 0}),
        ("telegram.thread.read", {"chat_ref": "-1001000000101"}),
        ("telegram.thread.read", {"chat_ref": REF["a-sales"], "before_message_id": 0}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "limit": 101}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "cursor": "tgk1.x.1"}),
    ],
)
async def test_mc02_size_and_shape_limits_are_invalid_input(tg: Tg, tool: str, args: dict[str, Any]) -> None:
    err = await tg.error(tool, args)
    assert (err["code"], err["retryable"]) == ("invalid_input", False)
    assert tg.service.calls.total() == 0


async def test_mc02_thread_read_pages_back_to_the_start(tg: Tg) -> None:
    chat = tg.service.chats["svc-alpha"][REF["a-sales"]]
    ids: list[int] = []
    before = None
    for _ in range(10):
        page = await tg.ok(
            "telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 5, "before_message_id": before}
        )
        jsonschema.validate(page, MESSAGES)
        ids += [m["message_id"] for m in page["messages"]]
        before = page["next_cursor"]
        assert page["more"] is (before is not None)
        if before is None:
            break
    assert ids == [m.message_id for m in reversed(chat.messages)]
    assert page["source_ref"] == f"telegram:tg-main/chat/{REF['a-sales']}" and page["complete"] is True


async def test_mc02_collect_follows_the_cursor_and_resumes(tg: Tg) -> None:
    ref = REF["a-partners"]
    chat = tg.service.chats["svc-alpha"][ref]
    got: list[int] = []
    cursor = None
    for _ in range(20):
        page = await tg.ok(
            "telegram.messages.collect", {"chat_ref": ref, "limit": 9} | ({"cursor": cursor} if cursor else {})
        )
        jsonschema.validate(page, MESSAGES)
        got += [m["message_id"] for m in page["messages"]]
        cursor = page["next_cursor"]
        if not page["more"]:
            break
    assert got == [m.message_id for m in chat.messages]
    # nothing new: an empty page keeps the caller's cursor (never a step back)
    idle = await tg.ok("telegram.messages.collect", {"chat_ref": ref, "cursor": cursor})
    assert (idle["messages"], idle["next_cursor"], idle["more"]) == ([], cursor, False)
    import random

    tg.service._append("svc-alpha", ref, "fresh Status: new", random.Random(2))  # noqa: S311
    fresh = await tg.ok("telegram.messages.collect", {"chat_ref": ref, "cursor": cursor})
    assert [m["text"] for m in fresh["messages"]] == ["fresh Status: new"]
    assert (await tg.ok("telegram.messages.collect", {"chat_ref": ref, "cursor": fresh["next_cursor"]}))[
        "messages"
    ] == []


async def test_mc02_gap_chat_reports_incomplete(tg: Tg) -> None:
    """a-gap is backfilling with a sync gap: the archive may miss messages, so the result is not complete."""
    for tool in ("telegram.thread.read", "telegram.messages.collect"):
        page = await tg.ok(tool, {"chat_ref": REF["a-gap"]})
        assert (page["complete"], page["partial_reasons"]) == (False, ["other"]), tool


# AC-M11-01: MC-03; AC-M11-04 --------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [1, 7, 3600, 2.2])
async def test_ac_m11_04_flood_wait_is_rate_limited_with_telegrams_delay(tg: Tg, seconds: float) -> None:
    tg.service.failure, tg.service.retry_after_s = "flood_wait", seconds
    for tool, args in (
        ("telegram.messages.search", {"query": "status"}),
        ("telegram.thread.read", {"chat_ref": REF["a-partners"]}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"]}),
    ):
        err = await tg.error(tool, args)
        assert (err["code"], err["retryable"]) == ("rate_limited", True), tool
        assert seconds <= err["retry_after_s"] <= seconds + 1, (tool, err)


def test_flood_wait_bounds() -> None:
    assert tools.service_error(ServiceError("flood_wait", 0)).retry_after_s == 1
    assert tools.service_error(ServiceError("flood_wait", None)).retry_after_s == 1
    assert tools.service_error(ServiceError("flood_wait", 10**9)).retry_after_s == 86400


@pytest.mark.parametrize(
    ("kind", "code", "retryable"),
    [
        ("busy", "temporarily_unavailable", True),
        ("auth_required", "temporarily_unavailable", True),
        ("provider_error", "temporarily_unavailable", True),
        ("peer_inaccessible", "not_found", False),
        ("chat_forbidden", "not_found", False),
    ],
)
async def test_mc03_service_failures_keep_codes_and_retryable(tg: Tg, kind: str, code: str, retryable: bool) -> None:
    tg.service.failure = kind  # type: ignore[assignment]
    for tool, args in (
        ("telegram.messages.search", {"query": "status"}),
        ("telegram.thread.read", {"chat_ref": REF["a-partners"]}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"]}),
    ):
        err = await tg.error(tool, args)
        assert (err["code"], err["retryable"]) == (code, retryable), tool
        assert "retry_after_s" not in err


async def test_mc03_timeout_after_the_request_was_sent(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service got the request and does not answer: the tool timeout (manifest limits.timeout_s) answers
    temporarily_unavailable, retryable for a read, never timeout_before_start."""
    manifest = yaml.safe_load((TG / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    assert {t["limits"]["timeout_s"] for t in manifest["tools"]} == {20}
    for t in manifest["tools"]:
        t["limits"]["timeout_s"] = 0.05
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(tools, "MANIFEST", tmp_path / "m.yaml")
    service = FakeTelegramService()
    service.delay_s = 0.5
    app, connector = server.build(env.settings, service)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        t = Tg(connector, http, session, env.issuer)
        err = await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]})
    assert err == {
        "code": "temporarily_unavailable",
        "message": "Telegram service did not answer in time",
        "retryable": True,
    }
    assert service.calls["read_thread"] == 1


# AC-M11-02 --------------------------------------------------------------------------------------


async def test_ac_m11_02_the_connector_opens_no_telegram_session(tg: Tg) -> None:
    before = tg.service.open_sessions
    for i in range(30):
        tg.service.failure = ("busy", None, "flood_wait")[i % 3]
        tg.service.retry_after_s = 5
        await tg.call("telegram.thread.read", {"chat_ref": REF["a-partners"]})
        await tg.call("telegram.messages.search", {"query": "status"}, principal=BETA_READER)
    assert tg.service.open_sessions == before == 2


def test_ac_m11_02_no_telegram_client_library_in_the_lockfile() -> None:
    lock = tomllib.loads((TG / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"] for p in lock["package"]}
    assert "fastmcp" in names  # the lockfile is the real one
    assert not names & {"telethon", "pyrogram", "pyrofork", "tdlib", "python-telegram", "tgcrypto", "kurigram"}
    source = "\n".join(p.read_text(encoding="utf-8") for p in (TG / "src").rglob("*.py"))
    assert "import telethon" not in source and "pyrogram" not in source


# manifest ---------------------------------------------------------------------------------------


def test_manifest_tools_are_read_only_and_schemas_exist() -> None:
    manifest = yaml.safe_load((TG / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    jsonschema.validate(manifest, json.loads((REPO / "schemas" / "gxg-manifest.schema.json").read_text()))
    assert {(t["name"], t["effect_class"], t["action"]) for t in manifest["tools"]} == {
        ("telegram.messages.search", "read", "read"),
        ("telegram.thread.read", "read", "read"),
        ("telegram.messages.collect", "read", "collect"),
    }
    for t in manifest["tools"]:
        jsonschema.Draft202012Validator.check_schema(tools.load_schema(t["input_schema"]))
        jsonschema.Draft202012Validator.check_schema(tools.load_schema(t["output_schema"]))


# configuration, bundle reload, audit and logging (server.py and TelegramTool.run are copies of demo's:
# tested the same way) ---------------------------------------------------------------------------

BASE = {"GXG_PROFILE": "test", "TELEGRAM_SERVICE": "fake"}
THREAD = "telegram.thread.read"


def jti(identity: dict[str, str]) -> str:
    payload = identity["x-gxg-test-identity"].split(".")[1]
    claim: str = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["jti"]
    return claim


def clean_env(tmp: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GXG_", "TELEGRAM_"))}
    return env | {"TELEGRAM_BUNDLE_DIR": str(tmp), "TELEGRAM_STATE_FILE": str(tmp / "state.json")}


# Settings -------------------------------------------------------------------------------------


def test_defaults_and_values_come_from_the_environment(tmp_path: Path) -> None:
    s = server.Settings.from_env(BASE)
    assert (s.profile, s.service, s.connections, s.test_controls, s.test_issuer_key, s.identity_keys) == (
        "test",
        "fake",
        {},
        False,
        None,
        "",
    )
    assert (s.bundle_dir, s.bundle_keys, s.state_file) == (
        Path("/bundle"),
        "",
        Path("/var/lib/telegram/bundle-state.json"),
    )
    path = tmp_path / "elsewhere" / "state.json"
    s = server.Settings.from_env(
        BASE
        | {
            "TELEGRAM_STATE_FILE": str(path),
            "TELEGRAM_BUNDLE_DIR": str(tmp_path),
            "TELEGRAM_BUNDLE_KEYS": "k=v",
            "TELEGRAM_TEST_CONTROLS": "1",
            "TELEGRAM_TEST_ISSUER_KEY": "i",
        }
    )
    assert (s.state_file, s.bundle_dir, s.bundle_keys, s.test_controls, s.test_issuer_key) == (
        path,
        tmp_path,
        "k=v",
        True,
        "i",
    )
    assert server.Settings.from_env(BASE | {"TELEGRAM_IDENTITY_KEYS": "k=v"}).identity_keys == "k=v"
    assert server.Settings.from_env(BASE | {"TELEGRAM_TEST_CONTROLS": "0"}).test_controls is False


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"TELEGRAM_TEST_ISSUER_KEY": "k"}, "test issuer and test controls are allowed only with GXG_PROFILE=test"),
        ({"TELEGRAM_TEST_CONTROLS": "1"}, "test issuer and test controls are allowed only with GXG_PROFILE=test"),
        (
            BASE | {"TELEGRAM_TEST_ISSUER_KEY": "k", "TELEGRAM_IDENTITY_KEYS": "k=v"},
            "TELEGRAM_TEST_ISSUER_KEY and TELEGRAM_IDENTITY_KEYS are exclusive",
        ),
        ({}, "TELEGRAM_SERVICE: only `fake` with GXG_PROFILE=test; no adapter to the service yet"),
        (
            {"TELEGRAM_SERVICE": "fake"},
            "TELEGRAM_SERVICE: only `fake` with GXG_PROFILE=test; no adapter to the service yet",
        ),
        ({"GXG_PROFILE": "test"}, "TELEGRAM_SERVICE: only `fake` with GXG_PROFILE=test; no adapter to the service yet"),
        (
            BASE | {"TELEGRAM_CONNECTIONS": "x"},
            "TELEGRAM_CONNECTIONS: expected tenant=hub_connection:service_connection, once per tenant",
        ),
        (BASE | {"TELEGRAM_BUNDLE_KEYS": "x"}, "TELEGRAM_BUNDLE_KEYS: expected kid=ed25519:<base64url>"),
        (BASE | {"TELEGRAM_IDENTITY_KEYS": "x"}, "TELEGRAM_IDENTITY_KEYS: expected kid=ed25519:<base64url>"),
        (BASE | {"TELEGRAM_IDENTITY_KEYS": "=ed25519:x"}, "TELEGRAM_IDENTITY_KEYS: expected kid=ed25519:<base64url>"),
    ],
)
def test_bad_configuration_does_not_start_and_says_why(
    env: dict[str, str], reason: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert server.main(clean_env(tmp_path) | env) == 2
    assert [json.loads(r.getMessage()) for r in caplog.records] == [{"event": "startup_refused", "reason": reason}]


def test_the_real_process_refuses_without_the_fake(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "telegram_connector"],
        check=False,
        env=clean_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["event"] == "startup_refused"


def test_bad_key_value_names_the_variable() -> None:
    with pytest.raises(server.ConfigError, match=r"^X: "):
        server.parse_keys("k=ed25519:short", "X")
    assert server.parse_keys(" , ", "X") == {}


def test_no_verifier_refuses_with_a_reason() -> None:
    with pytest.raises(TokenError) as exc:
        server.NoVerifier().verify({})
    assert exc.value.reason == "no identity verifier configured"


def test_servers_listen_where_the_spec_says(env: Env) -> None:
    app, connector = server.build(env.settings)
    assert isinstance(connector.service, FakeTelegramService)
    (only,) = server.configs(env.settings, app, connector)
    assert (only.host, only.port, only.log_level) == ("0.0.0.0", 8080, "warning")  # noqa: S104
    with_ctl = dataclasses.replace(env.settings, test_controls=True)
    ctl = server.configs(with_ctl, app, connector)[1]
    assert (ctl.host, ctl.port) == ("127.0.0.1", 8090)


# bundle, readiness ------------------------------------------------------------------------------


async def test_bundle_is_reloaded_when_its_files_change(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    good = {p.name: p.read_bytes() for p in env.settings.bundle_dir.iterdir()}
    shutil.rmtree(env.settings.bundle_dir)
    server.build(env.settings)  # a second build over the same state directory is fine
    service = FakeTelegramService()
    app, connector = server.build(env.settings, service)
    assert connector.service is service
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        t = Tg(connector, http, session, env.issuer)
        assert (await http.get("/readyz")).status_code == 503
        assert (await t.error(THREAD, {"chat_ref": REF["a-sales"]}))["code"] == "temporarily_unavailable"
        env.settings.bundle_dir.mkdir()
        for name, data in good.items():
            (env.settings.bundle_dir / name).write_bytes(data)
        assert (await http.get("/readyz")).status_code == 200
        assert (await t.ok(THREAD, {"chat_ref": REF["a-sales"]}))["messages"]
        (env.settings.bundle_dir / "grants-bundle.json").write_bytes(b"{}")  # a broken update keeps the previous one
        assert (await http.get("/readyz")).status_code == 200
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "telegram_connector"]
    assert {"event": "bundle_loaded", "bundle_version": "0" * 40} in events
    rejected = [e for e in events if e["event"] == "bundle_rejected"]
    assert len(rejected) == 1 and sorted(rejected[0]) == ["event", "reason"] and "signature" in rejected[0]["reason"]
    not_ready = [e for e in events if e["event"] == "not_ready"]
    assert not_ready and all(e["reasons"] for e in not_ready)


async def test_not_ready_service_is_logged_with_its_reason(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    tg.service.failure = "busy"
    assert (await tg.http.get("/readyz")).status_code == 503
    [line] = [json.loads(r.getMessage()) for r in caplog.records if r.name == "telegram_connector"]
    assert line == {"event": "not_ready", "reasons": ["telegram service is not available"]}


async def test_mcp_endpoint_is_stateless_json(tg: Tg) -> None:
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    r = await tg.http.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init}
    )
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["serverInfo"]["name"] == "telegram" and "mcp-session-id" not in r.headers
    r = await tg.http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r.json()["result"]["tools"]
    assert tools[0]["description"].startswith("Full-text search")
    assert all(t["outputSchema"]["type"] == "object" for t in tools)


# audit ------------------------------------------------------------------------------------------


async def test_audit_line_per_call(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    ids = [tg.identity(ALPHA_READER) for _ in range(5)]
    await tg.ok("telegram.messages.search", {"query": "status"}, identity=ids[0])
    await tg.ok(THREAD, {"chat_ref": REF["a-sales"]}, identity=ids[1])
    await tg.ok("telegram.messages.collect", {"chat_ref": REF["a-partners"]}, identity=ids[2])
    await tg.error(THREAD, {"chat_ref": REF["a-hidden"]}, identity=ids[3])
    tg.service.failure, tg.service.retry_after_s = "flood_wait", 9
    await tg.error(THREAD, {"chat_ref": REF["a-sales"]}, identity=ids[4])
    await tg.error(THREAD, {"chat_ref": REF["a-sales"]}, identity={})
    lines = audit_lines(caplog)
    assert all(isinstance(a.pop("duration_ms"), int) for a in lines)
    who = {"tenant": "t-alpha", "principal": ALPHA_READER, "authorization_header": False}
    anonymous = lines[5].pop("request_id")
    assert lines == [
        {
            "request_id": jti(ids[0]),
            **who,
            "tool": "telegram.messages.search",
            "grant_id": ["alpha-reader-chats"],
            "outcome": "ok",
        },
        {"request_id": jti(ids[1]), **who, "tool": THREAD, "grant_id": ["alpha-reader-chats"], "outcome": "ok"},
        {
            "request_id": jti(ids[2]),
            **who,
            "tool": "telegram.messages.collect",
            "grant_id": ["alpha-reader-collect"],
            "outcome": "ok",
        },
        {"request_id": jti(ids[3]), **who, "tool": THREAD, "outcome": "not_found"},
        {"request_id": jti(ids[4]), **who, "tool": THREAD, "service_error": "flood_wait", "outcome": "rate_limited"},
        {"tool": THREAD, "authorization_header": False, "outcome": "unauthenticated", "reason": "missing"},
    ]
    assert len(anonymous) == 36 and anonymous != jti(ids[0])


async def test_duration_is_in_milliseconds(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    tg.service.delay_s = 0.3
    await tg.ok(THREAD, {"chat_ref": REF["a-sales"]})
    assert 300 <= audit_lines(caplog)[0]["duration_ms"] < 5000


def cf_identity(key: Ed25519PrivateKey, tool: str, args: dict[str, Any], kid: str = "cf-1") -> str:
    now = int(time.time())
    claims = {
        "iss": cf.ISSUER,
        "aud": "telegram",
        "sub": ALPHA_READER,
        "tenant_id": "t-alpha",
        "tool": tool,
        "iat": now,
        "exp": now + 60,
        "jti": secrets.token_hex(16),
        "request_id": "cf-req",
        "ahs": cf.args_hash(args),
    }
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"typ": cf.TYP, "kid": kid})


async def test_contextforge_identity_is_verified_and_refusals_are_audited(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0001 C: TELEGRAM_IDENTITY_KEYS -> the kit's ContextForgeVerifier with aud `telegram`; SEC-03 reasons."""
    caplog.set_level(logging.INFO, logger="telegram_connector")
    key = Ed25519PrivateKey.generate()
    settings = dataclasses.replace(
        env.settings, test_issuer_key=None, identity_keys=f"cf-1={public_key(key.public_key())}"
    )
    app, connector = server.build(settings, FakeTelegramService())
    args = {"chat_ref": REF["a-sales"]}
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        t = Tg(connector, http, session, env.issuer)
        assert (await t.ok(THREAD, args, identity={"x-gxg-identity": cf_identity(key, THREAD, args)}))["messages"]
        for identity in (
            {"x-gxg-identity": cf_identity(Ed25519PrivateKey.generate(), THREAD, args, "cf-9")},
            {},
            {"x-gxg-identity": cf_identity(Ed25519PrivateKey.generate(), THREAD, args)},
        ):
            assert (await t.error(THREAD, args, identity=identity))["code"] == "unauthenticated"
    reasons = [(e["outcome"], e.get("reason")) for e in audit_lines(caplog)]
    assert reasons == [
        ("ok", None),
        ("unauthenticated", "kid"),
        ("unauthenticated", "missing"),
        ("unauthenticated", "signature"),
    ]
    assert "eyJ" not in caplog.text


async def test_unexpected_error_is_internal_error_with_frames_only(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")

    async def explode(*_: Any) -> Any:
        raise RuntimeError("service exploded: tg-secret-XYZ")

    tg.service.read_thread = explode  # type: ignore[method-assign,assignment]
    r = await tg.call(THREAD, {"chat_ref": REF["a-sales"]})
    assert r["structuredContent"] == {"code": "internal_error", "message": "internal error", "retryable": False}
    assert "tg-secret-XYZ" not in json.dumps(r) + caplog.text
    entry = audit_lines(caplog)[-1]
    assert (entry["outcome"], entry["error"]) == ("internal_error", "RuntimeError")
    [frames] = [r.getMessage() for r in caplog.records if r.name == "telegram_connector.errors"]
    head, *stack = frames.split("\n")
    assert head == f"internal_error {entry['request_id']} RuntimeError"
    assert stack[-1].endswith(" in explode") and all(re.fullmatch(r"/\S+\.py:\d+ in \S+", f) for f in stack), stack


CHILD = """
import asyncio, sys
import httpx2
from telegram_connector import server

def two_calls(configs):
    app = configs[0].app
    connector_service = None
    async def run():
        async with app.router.lifespan_context(app), httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://t"
        ) as http:
            for extra in ({}, {"x-boom": "1"}):
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "telegram.thread.read", "arguments": {"chat_ref": "tgc1_" + "A" * 32}}}
                accept = {"accept": "application/json, text/event-stream"}
                r = await http.post("/mcp", json=body, headers={**accept, **extra})
                assert r.json()["result"]["isError"] is True, r.text
    asyncio.run(run())

refuse = server.NoVerifier.verify
def verify(self, headers, **binding):
    if "x-boom" in headers:
        raise RuntimeError("verifier exploded: tg-secret-XYZ")
    return refuse(self, headers, **binding)

server.NoVerifier.verify = verify
server.serve = two_calls
sys.exit(server.main())
"""


def test_real_process_writes_audit_to_stdout_and_frames_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = clean_env(tmp_path) | BASE
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(server.__file__).parents[1]), env.get("PYTHONPATH")]))
    served: list[Any] = []
    monkeypatch.setattr(server, "serve", served.append)
    assert server.main(env | {"TELEGRAM_TEST_CONTROLS": "1"}) == 0
    assert [c.port for c in served[0]] == [8080, 8090]
    proc = subprocess.run(
        [sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    assert [(e.get("tool"), e.get("outcome"), e.get("error")) for e in lines if "tool" in e] == [
        ("telegram.thread.read", "unauthenticated", None),
        ("telegram.thread.read", "internal_error", "RuntimeError"),
    ]
    assert all(line.startswith("{") for line in proc.stdout.splitlines()), proc.stdout  # no SDK text lines on stdout
    req = next(e["request_id"] for e in lines if e.get("outcome") == "internal_error")
    assert re.search(rf"^internal_error {req} RuntimeError$", proc.stderr, re.M), proc.stderr
    assert re.search(r"^\S+:\d+ in verify$", proc.stderr, re.M), proc.stderr
    assert "tg-secret-XYZ" not in proc.stdout + proc.stderr
