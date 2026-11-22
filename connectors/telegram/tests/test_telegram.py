"""Telegram connector, read-only (milestone M11): AC-M11-01…05 in-process; the same through ContextForge in
stand/tests/test_telegram.py.

The MCP client is the official SDK over Streamable HTTP on httpx2's ASGITransport (no TCP socket). The bundle is
built by `gxg-policy build` from fixtures/grants/ with a key made here; identities come from the kit's test issuer.
The data source is a real PostgreSQL (TELEGRAM_PG_BIN, default /usr/lib/postgresql/16/bin, with pgvector) on a
unix socket: the vendored telegram_archive migrations of gxg_agents_v2, the reader role and synthetic fixtures
(archive/seed.py), one database per test. No Telegram, no real account (ADR-0004).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import itertools
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import jwt
import psycopg
import pytest
import seed
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gxg_connector_kit import TokenError
from gxg_connector_kit import cf_identity as cf
from gxg_connector_kit.identity import StubIssuer
from gxg_policy import cli as policy_cli
from telegram_connector import server, tools
from telegram_connector.archive import ArchiveService
from telegram_connector.service import ServiceError
from telegram_connector.tools import Connector

pytestmark = pytest.mark.anyio

REPO = next(p for p in Path(__file__).resolve().parents if (p / "policies").is_dir())
TG = REPO / "connectors" / "telegram"
ALPHA_READER, ALPHA_NOBODY, BETA_READER = "agent:t-alpha:reader", "agent:t-alpha:nobody", "agent:t-beta:reader"
CONNECTIONS = "t-alpha=tg-main:svc-alpha,t-beta=tg-main:svc-beta"
KID = "telegram-test"
REF = seed.REF
FORGED = "tgc1_" + "A" * 32  # well-formed, issued for no chat
NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}
SEARCH_RESULT = json.loads((REPO / "schemas" / "search-result.schema.json").read_text())
ERROR = json.loads((REPO / "schemas" / "error.schema.json").read_text())
MESSAGES = json.loads((TG / "schemas" / "messages.output.json").read_text())
PG_BIN = Path(os.environ.get("TELEGRAM_PG_BIN", "/usr/lib/postgresql/16/bin"))
READER_PASSWORD = "reader-" + secrets.token_hex(12)  # in the DSN: must never reach a log or a reply


class Pg:
    """A throw-away PostgreSQL cluster on a unix socket; `tpl` holds the archive schema, the role and the fixtures.

    shortcut: own initdb/pg_ctl fixture; pytest-postgresql connects over TCP, which the root run forbids.
    """

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="gxg-pg-"))
        self.root.chmod(0o755)
        self.as_postgres = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []  # initdb refuses root
        if self.as_postgres:
            shutil.chown(self.root, "postgres")
        self._n = itertools.count()

    def run(self, *cmd: str) -> None:
        subprocess.run([*self.as_postgres, *cmd], check=True, capture_output=True, timeout=120)

    def start(self) -> None:
        data = str(self.root / "data")
        self.run(str(PG_BIN / "initdb"), "-D", data, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-sync")
        opts = f"-k {self.root} -c listen_addresses='' -c fsync=off -c log_connections=on"
        self.run(str(PG_BIN / "pg_ctl"), "-D", data, "-o", opts, "-l", str(self.root / "log"), "-w", "start")
        self.sql("postgres", "CREATE DATABASE tpl")
        self.sql("tpl", seed.schema_sql() + seed.seed_sql())
        self.sql("tpl", f"ALTER ROLE {seed.READER} PASSWORD '{READER_PASSWORD}'")

    def stop(self) -> None:
        self.run(str(PG_BIN / "pg_ctl"), "-D", str(self.root / "data"), "-m", "immediate", "stop")
        shutil.rmtree(self.root, ignore_errors=True)

    def dsn(self, db: str, user: str = "postgres", password: str = "") -> str:
        auth = f"{user}:{password}" if password else user
        return f"postgresql://{auth}@/{db}?host={self.root}"

    def sql(self, db: str, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.dsn(db), autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else []

    def database(self) -> str:
        name = f"t{os.getpid()}_{next(self._n)}"  # mutmut forks workers that share one cluster
        for _ in range(100):  # a concurrent CREATE ... TEMPLATE tpl holds the template for a moment
            try:
                self.sql("postgres", f"CREATE DATABASE {name} TEMPLATE tpl")
                return name
            except psycopg.errors.ObjectInUse:
                time.sleep(0.05)
        raise AssertionError("template database stayed busy")


@pytest.fixture(scope="session")
def pg() -> Iterator[Pg]:
    cluster = Pg()
    cluster.start()
    yield cluster
    cluster.stop()


@dataclass
class Db:
    """The test's own archive database: `sql()` as the owner, `dsn` of the connector's read-only role."""

    pg: Pg
    name: str

    @property
    def dsn(self) -> str:
        return self.pg.dsn(self.name, seed.READER, READER_PASSWORD)

    def sql(self, statement: str, params: Any = None) -> list[tuple[Any, ...]]:
        return self.pg.sql(self.name, statement, params)

    def add(self, chat: str, text: str | None, caption: str | None = None, media: str | None = None) -> int:
        """Append a message to a fixture chat; returns its id."""
        conn = next(c for c, n, *_ in seed.CHATS if n == chat)
        rows = self.sql(
            """INSERT INTO telegram_archive.messages (connection_id, peer_id, message_id, sent_at, text,
                caption, media_type) SELECT %(c)s, %(p)s, coalesce(max(message_id), 0) + 1, now(), %(t)s, %(cap)s, %(m)s
                FROM telegram_archive.messages WHERE connection_id = %(c)s AND peer_id = %(p)s RETURNING message_id""",
            {"c": conn, "p": seed.PEER[chat], "t": text, "cap": caption, "m": media},
        )
        return int(rows[0][0])

    def defer(self, connection: str, seconds: float) -> None:
        """Telegram flood wait recorded by the service's sync: the Connection is deferred for `seconds`."""
        self.sql(
            """INSERT INTO telegram_archive.connection_state (connection_id, retry_after)
            VALUES (%s, clock_timestamp() + make_interval(secs => %s)) ON CONFLICT (connection_id)
            DO UPDATE SET retry_after = excluded.retry_after""",
            (connection, seconds),
        )


@pytest.fixture
def db(pg: Pg) -> Db:
    return Db(pg, pg.database())


class Counting(ArchiveService):
    """The archive port with a counter of requests (AC-M11-03): every public method counts once."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self.calls: dict[str, int] = {}

    def total(self) -> int:
        return sum(self.calls.values())

    def __getattribute__(self, name: str) -> Any:
        if name in ("scope_chats", "search", "read_thread", "collect"):
            calls = object.__getattribute__(self, "calls")
            calls[name] = calls.get(name, 0) + 1
        return object.__getattribute__(self, name)


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
        "TELEGRAM_ARCHIVE_DSN": "postgresql://unused@/x",
        "TELEGRAM_CONNECTIONS": CONNECTIONS,
        "TELEGRAM_BUNDLE_DIR": str(tmp / "bundle"),
        "TELEGRAM_BUNDLE_KEYS": keys,
        "TELEGRAM_STATE_FILE": str(tmp / "state" / "deep" / "bundle-state.json"),  # parents are created
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
        assert isinstance(connector.service, Counting)
        self.service: Counting = connector.service

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


async def open_tg(stack: AsyncExitStack, env: Env, db: Db, settings: server.Settings | None = None) -> Tg:
    app, connector = server.build(settings or env.settings, Counting(db.dsn))
    http, session = await connect(stack, app)
    return Tg(connector, http, session, env.issuer)


@pytest.fixture
async def tg(env: Env, db: Db) -> AsyncIterator[Tg]:
    async with AsyncExitStack() as stack:
        yield await open_tg(stack, env, db)


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "telegram_connector.audit"]


def texts_of(chat: str) -> list[str]:
    return [m["text"] or m["caption"] for m in seed.messages() if m["peer_id"] == seed.PEER[chat]]


def search_ids(content: dict[str, Any]) -> list[str]:
    return [i["id"] for i in content["items"]]


# health, configuration -------------------------------------------------------------------------


async def test_healthz_and_readyz(tg: Tg, db: Db) -> None:
    assert (await tg.http.get("/healthz")).status_code == 200
    r = await tg.http.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})
    tg.service.dsn = tg.service.dsn.replace(db.name, "no_such_db")  # the archive cannot be read
    r = await tg.http.get("/readyz")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})  # MCC-09: no reasons in the body


async def test_readyz_503_and_calls_refused_without_a_valid_bundle(env: Env, db: Db) -> None:
    (env.settings.bundle_dir / "grants-bundle.json.sig").write_text("{}")
    async with AsyncExitStack() as stack:
        t = await open_tg(stack, env, db)
        assert (await t.http.get("/readyz")).status_code == 503
        err = await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]})
        assert (err["code"], err["retryable"]) == ("temporarily_unavailable", True)
        assert t.service.total() == 0


@pytest.mark.parametrize(
    "change",
    [
        {"TELEGRAM_ARCHIVE_DSN": ""},  # the archive is the only data source: no silent default
        {"TELEGRAM_ARCHIVE_DSN": "http://archive"},
        {"GXG_PROFILE": "production"},  # the test issuer only in the test profile
        {"TELEGRAM_IDENTITY_KEYS": "k=ed25519:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},  # with the test issuer
        {"TELEGRAM_CONNECTIONS": "t-alpha=tg-main"},
        {"TELEGRAM_CONNECTIONS": "t-alpha=tg-main:svc-alpha,t-alpha=tg-main:svc-beta"},
    ],
)
def test_settings_refuse_to_start(env: Env, change: dict[str, str]) -> None:
    with pytest.raises(server.ConfigError):
        server.Settings.from_env(env.raw | change)


def test_production_profile_starts_with_an_archive_dsn() -> None:
    s = server.Settings.from_env({"TELEGRAM_ARCHIVE_DSN": "postgres://r@h/a", "TELEGRAM_IDENTITY_KEYS": "k=v"})
    assert (s.profile, s.archive_dsn, s.test_issuer_key) == ("production", "postgres://r@h/a", None)


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


async def test_no_identity_keys_refuse_every_call(env: Env, db: Db) -> None:
    settings = server.Settings.from_env(env.raw | {"TELEGRAM_TEST_ISSUER_KEY": ""})
    async with AsyncExitStack() as stack:
        t = await open_tg(stack, env, db, settings)
        assert (await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]}))["code"] == "unauthenticated"
        assert t.service.total() == 0


def test_identity_keys_select_the_contextforge_verifier(env: Env) -> None:
    key = public_key(Ed25519PrivateKey.generate().public_key())
    settings = server.Settings.from_env(
        env.raw | {"TELEGRAM_TEST_ISSUER_KEY": "", "TELEGRAM_IDENTITY_KEYS": f"a={key}"}
    )
    app, connector = server.build(settings)
    assert isinstance(connector.service, ArchiveService) and connector.service.dsn == settings.archive_dsn
    assert [(c.host, c.port) for c in server.configs(app)] == [("0.0.0.0", 8080)]  # noqa: S104
    assert connector.connections == {"t-alpha": ("tg-main", "svc-alpha"), "t-beta": ("tg-main", "svc-beta")}


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
    calls = tg.service.total()
    for name, identity in cases.items():
        err = await tg.error("telegram.thread.read", args, identity=identity)
        assert err == {"code": "unauthenticated", "message": "token rejected", "retryable": False}, name
    assert tg.service.total() == calls  # refused before any request to the service
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
    assert tg.service.total() == 0


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
    assert tg.service.total() == 0


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


async def test_ac_m11_05_no_texts_peer_ids_or_secrets_in_logs_after_1000_calls(
    tg: Tg, db: Db, caplog: pytest.LogCaptureFixture
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
    dsn = tg.service.dsn
    for i in range(1000):
        tool, args, who = plan[i % len(plan)]
        if i % 97 == 0:
            db.defer("svc-alpha", 3)  # flood-wait deferral: rate_limited / partial results
        if i % 89 == 0:
            tg.service.dsn = dsn.replace(db.name, "no_such_db")  # the archive is down: its error text quotes the db
        replies.append(await tg.call(tool, args, principal=who))
        tg.service.dsn = dsn
        db.sql("DELETE FROM telegram_archive.connection_state")
    # everything logged in this process except the test's own MCP client
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("mcp.client"))
    assert len(audit_lines(caplog)) == 1000
    texts = seed.texts()
    peers = [str(p) for p in seed.PEER.values()]
    assert [t for t in texts if t in logs] == []
    assert [p for p in peers if p in logs or p.lstrip("-") in logs] == []
    assert READER_PASSWORD not in logs and seed.READER not in logs and "no_such_db" not in logs  # DSN, role, errors
    assert "игнорируй" not in logs and "beta secret plan" not in logs  # the queries are not logged either
    blob = json.dumps(replies, ensure_ascii=False)
    assert [p for p in peers if p in blob] == [] and READER_PASSWORD not in blob  # MCC-04, SEC-17
    assert texts_of("b-public")[-1] in blob  # the replies do carry texts: the scan is meaningful
    outcomes = {a["outcome"] for a in audit_lines(caplog)}
    assert {"ok", "not_found", "rate_limited", "temporarily_unavailable"} <= outcomes


async def test_sc09_internal_error_keeps_text_out_of_logs_and_reply(tg: Tg, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    leak = texts_of("a-partners")[0]

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
    assert [i["excerpt"] for i in got["items"]] == [texts_of("a-partners")[-2]]
    names = [t.name for t in (await tg.session.list_tools()).tools]
    assert names == ["telegram.messages.search", "telegram.thread.read", "telegram.messages.collect"]


async def test_long_text_is_truncated_and_flagged(tg: Tg, db: Db) -> None:
    long_id = db.add("a-sales", "lorem ipsum " * 400)  # 4800 characters
    thread = await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 2})
    first, second = thread["messages"]
    assert (first["message_id"], len(first["text"]), first["truncated"], second["truncated"]) == (
        long_id,
        4000,
        True,
        False,
    )
    got = await tg.ok("telegram.messages.search", {"query": "lorem", "chat_refs": [REF["a-sales"]]})
    assert len(got["items"][0]["excerpt"]) == 400


async def test_text_is_cut_before_it_is_escaped(tg: Tg, db: Db) -> None:
    """3999 characters with two bidi marks are not truncated; escaping never cuts a `\\u{...}` in half."""
    db.add("a-sales", "\u202e" + "a" * 3997 + "\u202c")
    (m,) = (await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 1}))["messages"]
    assert (m["truncated"], m["text"][:8], m["text"][-8:], len(m["text"])) == (
        False,
        "\\u{202E}",
        "\\u{202C}",
        3997 + 16,
    )
    db.add("a-sales", "\u202e" * 4001)
    (m,) = (await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 1}))["messages"]
    assert (m["truncated"], len(m["text"])) == (True, 4000 * 8)
    jsonschema.validate(
        {
            "chat_ref": REF["a-sales"],
            "source_ref": "x",
            "untrusted": True,
            "messages": [m],
            "next_cursor": None,
            "more": False,
            "complete": True,
            "partial_reasons": [],
        },
        MESSAGES,
    )


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
    for _ in range(5):
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
        ("telegram.messages.search", {"query": "x", "chat_refs": ["not-a-chat-ref"]}),
        ("telegram.thread.read", {"chat_ref": REF["a-sales"], "limit": 0}),
        ("telegram.thread.read", {"chat_ref": "not-a-chat-ref"}),
        ("telegram.thread.read", {"chat_ref": REF["a-sales"], "before_message_id": 0}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "limit": 101}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"], "cursor": "tgk1.x.1"}),
    ],
)
async def test_mc02_size_and_shape_limits_are_invalid_input(tg: Tg, tool: str, args: dict[str, Any]) -> None:
    err = await tg.error(tool, args)
    assert (err["code"], err["retryable"]) == ("invalid_input", False)
    assert tg.service.total() == 0


async def test_mc02_thread_read_pages_back_to_the_start(tg: Tg) -> None:
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
    assert ids == list(range(len(texts_of("a-sales")), 0, -1))
    assert page["source_ref"] == f"telegram:tg-main/chat/{REF['a-sales']}" and page["complete"] is True


async def test_mc02_collect_follows_the_cursor_and_resumes(tg: Tg, db: Db) -> None:
    ref = REF["a-partners"]
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
    assert got == list(range(1, len(texts_of("a-partners")) + 1))
    # nothing new: an empty page keeps the caller's cursor (never a step back)
    idle = await tg.ok("telegram.messages.collect", {"chat_ref": ref, "cursor": cursor})
    assert (idle["messages"], idle["next_cursor"], idle["more"]) == ([], cursor, False)
    db.add("a-partners", "fresh Status: new")
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


@pytest.mark.parametrize("seconds", [1, 7, 3600])
async def test_ac_m11_04_flood_wait_is_rate_limited_with_telegrams_delay(tg: Tg, db: Db, seconds: int) -> None:
    """The service's sync records Telegram's flood wait as `connection_state.retry_after`: collect (Signals) is
    rate_limited with the remaining delay; search and thread.read still answer from the archive, flagged."""
    db.defer("svc-alpha", seconds)
    err = await tg.error("telegram.messages.collect", {"chat_ref": REF["a-partners"]})
    assert (err["code"], err["retryable"], err["message"]) == ("rate_limited", True, "Telegram flood wait")
    assert seconds - 1 <= err["retry_after_s"] <= seconds, err
    for tool, args in (
        ("telegram.messages.search", {"query": "status", "chat_refs": [REF["a-sales"]]}),
        ("telegram.thread.read", {"chat_ref": REF["a-sales"]}),
    ):
        page = await tg.ok(tool, args)
        assert (page["complete"], page["partial_reasons"]) == (False, ["rate_limited"]), tool
    assert (await tg.ok("telegram.messages.collect", {"chat_ref": REF["b-ops"]}, principal=BETA_READER))["messages"]
    db.sql("UPDATE telegram_archive.connection_state SET retry_after = clock_timestamp() - interval '1 s'")
    assert (await tg.ok("telegram.messages.collect", {"chat_ref": REF["a-partners"]}))["messages"]  # it passed


def test_flood_wait_bounds() -> None:
    assert tools.service_error(ServiceError("flood_wait", 0)).retry_after_s == 1
    assert tools.service_error(ServiceError("flood_wait", None)).retry_after_s == 1
    assert tools.service_error(ServiceError("flood_wait", 2.2)).retry_after_s == 3
    assert tools.service_error(ServiceError("flood_wait", 10**9)).retry_after_s == 86400


async def test_mc03_archive_down_is_temporarily_unavailable(tg: Tg, db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "telegram_connector.audit")
    tg.service.dsn = tg.service.dsn.replace(db.name, "no_such_db")
    for tool, args in (
        ("telegram.messages.search", {"query": "status"}),
        ("telegram.thread.read", {"chat_ref": REF["a-partners"]}),
        ("telegram.messages.collect", {"chat_ref": REF["a-partners"]}),
    ):
        err = await tg.error(tool, args)
        assert err == {
            "code": "temporarily_unavailable",
            "message": "Telegram archive is not available",
            "retryable": True,
        }, tool
    assert {(a["service_error"], a["error"]) for a in audit_lines(caplog)} == {("unavailable", "OperationalError")}


async def test_mc03_chat_outside_the_scope_is_not_found(tg: Tg) -> None:
    assert (await tg.error("telegram.messages.collect", {"chat_ref": FORGED}, principal=BETA_READER)) == NOT_FOUND


async def test_mc03_timeout_after_the_request_was_sent(
    env: Env, db: Db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive got the query and does not answer (a lock): the tool timeout (manifest limits.timeout_s)
    answers temporarily_unavailable, retryable for a read, never timeout_before_start."""
    manifest = yaml.safe_load((TG / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    assert {t["limits"]["timeout_s"] for t in manifest["tools"]} == {20}
    for t in manifest["tools"]:
        t["limits"]["timeout_s"] = 0.5
    (tmp_path / "m.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(tools, "MANIFEST", tmp_path / "m.yaml")
    with psycopg.connect(db.pg.dsn(db.name)) as locker:
        locker.execute("LOCK TABLE telegram_archive.messages IN ACCESS EXCLUSIVE MODE")
        async with AsyncExitStack() as stack:
            t = await open_tg(stack, env, db)
            started = time.monotonic()
            err = await t.error("telegram.thread.read", {"chat_ref": REF["a-partners"]})
            took = time.monotonic() - started
    assert err == {
        "code": "temporarily_unavailable",
        "message": "Telegram archive did not answer in time",
        "retryable": True,
    }
    assert t.service.calls["read_thread"] == 1 and took < 5


# AC-M11-02 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO telegram_archive.connection_state (connection_id, retry_after) VALUES ('svc-alpha', now())",
        "UPDATE telegram_archive.messages SET text = 'x' WHERE message_id = 1",
        "DELETE FROM telegram_archive.chats",
        "SELECT telegram_archive.apply_chat_batch('{}'::jsonb)",
        "SELECT author_id FROM telegram_archive.messages LIMIT 1",  # column-level: not granted
        "SELECT embedding FROM telegram_archive.messages LIMIT 1",
        "SELECT metadata FROM capability_hub.connections LIMIT 1",
        "CREATE TABLE telegram_archive.x (a int)",
    ],
)
def test_ac_m11_02_the_archive_role_cannot_write_or_read_beyond_its_columns(db: Db, statement: str) -> None:
    """ADR-0004: the connector reads with gxg_telegram_reader only; the archive and the session stay the service's.
    Even with the read-only default switched off, the grants refuse."""
    with psycopg.connect(db.dsn, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        assert conn.execute("SELECT count(*) FROM telegram_archive.messages").fetchone() == (len(seed.messages()),)


def test_ac_m11_02_reader_transactions_are_read_only(db: Db) -> None:
    with psycopg.connect(db.dsn, autocommit=True) as conn, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("DELETE FROM telegram_archive.chats")


def test_vendored_schema_is_the_pinned_upstream() -> None:
    """The archive schema is the contract with gxg_agents_v2 (ADR-0004): the vendored migrations are byte-for-byte
    the ones archive/SOURCE.md pins (the upstream comparison itself: archive/check_upstream.sh)."""
    import hashlib

    source = (TG / "archive" / "SOURCE.md").read_text(encoding="utf-8")
    pinned = dict(line.split()[::-1] for line in re.findall(r"^[0-9a-f]{64}  \S+$", source, re.M))
    assert set(pinned) == {"20261105010000_telegram_archive.sql", "20261106010000_telegram_archive_semantic.sql"}
    for name, digest in pinned.items():
        assert hashlib.sha256((TG / "archive" / name).read_bytes()).hexdigest() == digest, name


def test_contract_the_columns_the_adapter_reads_exist_with_their_types(db: Db) -> None:
    """If gxg_agents_v2 renames or retypes a column the adapter reads, re-vendoring the migrations fails here."""
    want = {
        ("chats", "connection_id", "text"),
        ("chats", "peer_id", "bigint"),
        ("chats", "chat_ref", "text"),
        ("chats", "state", "text"),
        ("chats", "gap", "boolean"),
        ("chats", "synced_at", "timestamp with time zone"),
        ("messages", "connection_id", "text"),
        ("messages", "peer_id", "bigint"),
        ("messages", "message_id", "bigint"),
        ("messages", "sent_at", "timestamp with time zone"),
        ("messages", "edited_at", "timestamp with time zone"),
        ("messages", "text", "text"),
        ("messages", "caption", "text"),
        ("messages", "reply_to", "bigint"),
        ("messages", "media_type", "text"),
        ("messages", "tsv", "tsvector"),
        ("messages", "archived_at", "timestamp with time zone"),
        ("connection_state", "connection_id", "text"),
        ("connection_state", "retry_after", "timestamp with time zone"),
    }
    rows = db.sql("""SELECT table_name, column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'telegram_archive'""")
    assert want <= {tuple(r) for r in rows}
    granted = db.sql(
        """SELECT table_name, column_name FROM information_schema.column_privileges
        WHERE grantee = %s AND privilege_type = 'SELECT'""",
        (seed.READER,),
    )
    assert {(t, c) for t, c, _ in want} == {tuple(r) for r in granted}  # exactly what the adapter reads


async def test_chat_taken_out_of_scope_is_not_found_on_every_tool(tg: Tg, db: Db) -> None:
    """CON-TG-02/06: the service's trigger purges a chat from the archive in the transaction that removes it from
    the Connection's scope; from then on the connector answers not_found, also for a mask grant."""
    ref = REF["b-ops"]
    assert (await tg.ok("telegram.thread.read", {"chat_ref": ref}, principal=BETA_READER))["messages"]
    db.sql(
        """UPDATE capability_hub.connections SET metadata = jsonb_set(metadata, '{scope,chats}',
        (SELECT coalesce(jsonb_agg(e), '[]') FROM jsonb_array_elements(metadata #> '{scope,chats}') e
         WHERE e ->> 'chat_ref' <> %s)) WHERE id = 'svc-beta'""",
        (ref,),
    )
    assert db.sql("SELECT count(*) FROM telegram_archive.chats WHERE chat_ref = %s", (ref,)) == [(0,)]
    for tool, args in (
        ("telegram.thread.read", {"chat_ref": ref}),
        ("telegram.messages.collect", {"chat_ref": ref}),
        ("telegram.messages.search", {"query": "status", "chat_refs": [ref]}),
    ):
        assert await tg.error(tool, args, principal=BETA_READER) == NOT_FOUND, tool
    got = await tg.ok("telegram.messages.search", {"query": "status", "limit": 50}, principal=BETA_READER)
    assert {i["id"].split(":")[0] for i in got["items"]} == {REF["b-public"]}


async def test_mask_grant_and_a_chat_outside_the_scope_is_not_found_on_every_tool(tg: Tg) -> None:
    """t-beta has a mask grant: chat_refs of t-alpha's Connection or forged ones are not in its archive."""
    for ref in (REF["a-partners"], FORGED):
        for tool, args in (
            ("telegram.thread.read", {"chat_ref": ref}),
            ("telegram.messages.collect", {"chat_ref": ref}),
            ("telegram.messages.search", {"query": "status", "chat_refs": [ref]}),
        ):
            assert await tg.error(tool, args, principal=BETA_READER) == NOT_FOUND, (ref, tool)


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

DSN = "postgresql://gxg_telegram_reader:not-a-real-password@archive.invalid/archive"
BASE = {"TELEGRAM_ARCHIVE_DSN": DSN}
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
    assert (s.profile, s.archive_dsn, s.connections, s.test_issuer_key, s.identity_keys) == (
        "production",
        DSN,
        {},
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
            "GXG_PROFILE": "test",
            "TELEGRAM_STATE_FILE": str(path),
            "TELEGRAM_BUNDLE_DIR": str(tmp_path),
            "TELEGRAM_BUNDLE_KEYS": "k=v",
            "TELEGRAM_TEST_ISSUER_KEY": "i",
        }
    )
    assert (s.profile, s.state_file, s.bundle_dir, s.bundle_keys, s.test_issuer_key) == (
        "test",
        path,
        tmp_path,
        "k=v",
        "i",
    )
    assert server.Settings.from_env(BASE | {"TELEGRAM_IDENTITY_KEYS": "k=v"}).identity_keys == "k=v"
    assert server.Settings.from_env({"TELEGRAM_ARCHIVE_DSN": "postgres://x"}).archive_dsn == "postgres://x"


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        (BASE | {"TELEGRAM_TEST_ISSUER_KEY": "k"}, "the test issuer is allowed only with GXG_PROFILE=test"),
        (
            BASE | {"GXG_PROFILE": "test", "TELEGRAM_TEST_ISSUER_KEY": "k", "TELEGRAM_IDENTITY_KEYS": "k=v"},
            "TELEGRAM_TEST_ISSUER_KEY and TELEGRAM_IDENTITY_KEYS are exclusive",
        ),
        ({}, "TELEGRAM_ARCHIVE_DSN: expected postgresql://..."),
        ({"TELEGRAM_ARCHIVE_DSN": "host=archive password=hunter2"}, "TELEGRAM_ARCHIVE_DSN: expected postgresql://..."),
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
    assert "hunter2" not in caplog.text  # a bad DSN is never echoed


def test_the_real_process_refuses_without_an_archive(tmp_path: Path) -> None:
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
    app, _ = server.build(env.settings)
    (only,) = server.configs(app)
    assert (only.host, only.port, only.log_level) == ("0.0.0.0", 8080, "warning")  # noqa: S104


# bundle, readiness ------------------------------------------------------------------------------


async def test_bundle_is_reloaded_when_its_files_change(env: Env, db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    good = {p.name: p.read_bytes() for p in env.settings.bundle_dir.iterdir()}
    shutil.rmtree(env.settings.bundle_dir)
    server.build(env.settings)  # a second build over the same state directory is fine
    async with AsyncExitStack() as stack:
        t = await open_tg(stack, env, db)
        http = t.http
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


async def test_not_ready_archive_is_logged_with_its_reason(tg: Tg, db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    tg.service.dsn = tg.service.dsn.replace(db.name, "no_such_db")
    assert (await tg.http.get("/readyz")).status_code == 503
    [line] = [json.loads(r.getMessage()) for r in caplog.records if r.name == "telegram_connector"]
    assert line == {"event": "not_ready", "reasons": ["telegram archive is not available"]}


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


async def test_audit_line_per_call(tg: Tg, db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    ids = [tg.identity(ALPHA_READER) for _ in range(5)]
    await tg.ok("telegram.messages.search", {"query": "status"}, identity=ids[0])
    await tg.ok(THREAD, {"chat_ref": REF["a-sales"]}, identity=ids[1])
    await tg.ok("telegram.messages.collect", {"chat_ref": REF["a-partners"]}, identity=ids[2])
    await tg.error(THREAD, {"chat_ref": REF["a-hidden"]}, identity=ids[3])
    db.defer("svc-alpha", 9)
    await tg.error("telegram.messages.collect", {"chat_ref": REF["a-partners"]}, identity=ids[4])
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
        {
            "request_id": jti(ids[4]),
            **who,
            "tool": "telegram.messages.collect",
            "service_error": "flood_wait",
            "outcome": "rate_limited",
        },
        {"tool": THREAD, "authorization_header": False, "outcome": "unauthenticated", "reason": "missing"},
    ]
    assert len(anonymous) == 36 and anonymous != jti(ids[0])


async def test_duration_is_in_milliseconds(tg: Tg, db: Db, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_connector")
    with psycopg.connect(db.pg.dsn(db.name)) as locker:
        locker.execute("LOCK TABLE telegram_archive.messages IN ACCESS EXCLUSIVE MODE")
        task = asyncio.create_task(tg.ok(THREAD, {"chat_ref": REF["a-sales"]}))
        await asyncio.sleep(0.3)
    await task  # the lock is released when the block closes the locker's transaction
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
    env: Env, db: Db, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR-0001 C: TELEGRAM_IDENTITY_KEYS -> the kit's ContextForgeVerifier with aud `telegram`; SEC-03 reasons."""
    caplog.set_level(logging.INFO, logger="telegram_connector")
    key = Ed25519PrivateKey.generate()
    settings = dataclasses.replace(
        env.settings, test_issuer_key=None, identity_keys=f"cf-1={public_key(key.public_key())}"
    )
    app, connector = server.build(settings, Counting(db.dsn))
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
    assert server.main(env) == 0
    assert [c.port for c in served[0]] == [8080]
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


# DoD-4: the exact behaviour mutmut probes -------------------------------------------------------------


def test_errors_are_exact() -> None:
    flood = tools.service_error(ServiceError("flood_wait", 2.5)).to_dict()
    assert flood == {"code": "rate_limited", "message": "Telegram flood wait", "retryable": True, "retry_after_s": 3}
    down = tools.service_error(ServiceError("unavailable")).to_dict()
    assert down == {
        "code": "temporarily_unavailable",
        "message": "Telegram archive is not available",
        "retryable": True,
    }
    assert tools.service_error(ServiceError("chat_forbidden")).to_dict() == NOT_FOUND
    assert tools.other(["sync_gap"]) == tools.other(["not_synced"]) == ["other"]
    assert tools.other(["flood_wait"]) == ["rate_limited"]
    assert tools.other(["sync_gap", "flood_wait"]) == ["other", "rate_limited"]
    assert tools.other(["result_limit"]) == []


async def test_invalid_input_names_the_path(tg: Tg) -> None:
    err = await tg.error("telegram.messages.search", {"query": "x", "chat_refs": ["ok", "-1"]})
    assert err == {
        "code": "invalid_input",
        "message": "arguments do not match the input schema",
        "retryable": False,
        "details": {"path": "/chat_refs/0"},
    }


async def test_defaults_page_sizes(tg: Tg) -> None:
    assert len((await tg.ok("telegram.messages.search", {"query": "message"}))["items"]) == 20
    assert len((await tg.ok(THREAD, {"chat_ref": REF["a-partners"]}))["messages"]) == 20
    assert len((await tg.ok("telegram.messages.collect", {"chat_ref": REF["a-partners"]}))["messages"]) == 50
    assert len((await tg.ok("telegram.messages.collect", {"chat_ref": REF["a-partners"], "limit": 9}))["messages"]) == 9


async def test_media_without_text_and_text_at_the_budget(tg: Tg, db: Db) -> None:
    ref = REF["a-sales"]
    exact_id = db.add("a-sales", "y" * 4000)
    media_id = db.add("a-sales", None, None, "photo")
    caption_id = db.add("a-sales", None, "a caption about the budget", "document")
    caption, newest, exact = (await tg.ok(THREAD, {"chat_ref": ref, "limit": 3}))["messages"]
    assert (caption["message_id"], caption["text"], caption["media_type"]) == (
        caption_id,
        "a caption about the budget",
        "document",
    )
    assert (newest["message_id"], newest["text"], newest["truncated"]) == (media_id, "", False)
    assert (exact["message_id"], len(exact["text"]), exact["truncated"]) == (exact_id, 4000, False)
    got = await tg.ok("telegram.messages.search", {"query": "caption", "chat_refs": [ref]})
    assert got["items"] == [
        {
            "id": f"{ref}:{caption_id}",
            "excerpt": "a caption about the budget",
            "source_ref": f"telegram:tg-main/chat/{ref}",
        }
    ]
    orig = tg.service.read_thread

    async def no_text(*a: Any) -> Any:
        page = await orig(*a)
        return dataclasses.replace(page, items=[dataclasses.replace(m, text=None, caption=None) for m in page.items])

    tg.service.read_thread = no_text  # type: ignore[method-assign,assignment]
    assert {m["text"] for m in (await tg.ok(THREAD, {"chat_ref": ref}))["messages"]} == {""}


async def test_items_of_another_granted_chat_are_dropped_when_chats_are_named(tg: Tg) -> None:
    """GR-16: a granted chat the caller did not name must not come back either."""
    orig = tg.service.search

    async def wider(connection: str, query: str, chat_refs: list[str], limit: int) -> Any:
        mine = await orig(connection, query, chat_refs, limit)
        other = await orig(connection, query, [REF["a-partners"]], limit)
        return dataclasses.replace(mine, items=other.items[:5] + mine.items)

    tg.service.search = wider  # type: ignore[method-assign]
    got = await tg.ok("telegram.messages.search", {"query": "status", "chat_refs": [REF["a-sales"]], "limit": 50})
    assert got["items"] and {i["id"].split(":")[0] for i in got["items"]} == {REF["a-sales"]}


async def test_more_than_128_scope_chats_is_truncated(tg: Tg, db: Db) -> None:
    db.sql("""INSERT INTO telegram_archive.chats (connection_id, peer_id, chat_ref, state)
        SELECT 'svc-beta', -1003000000000 - i, 'tgc1_' || lpad(i::text, 32, '0'), 'synced'
        FROM generate_series(1, 128) i""")
    seen: list[int] = []
    orig = tg.service.search

    async def spy(connection: str, query: str, chat_refs: list[str], limit: int) -> Any:
        seen.append(len(chat_refs))
        return await orig(connection, query, chat_refs, limit)

    tg.service.search = spy  # type: ignore[method-assign]
    got = await tg.ok("telegram.messages.search", {"query": "status"}, principal=BETA_READER)
    assert seen == [128] and got["partial_reasons"] == ["truncated"] and got["complete"] is False
    db.sql(
        "DELETE FROM telegram_archive.chats WHERE chat_ref IN ('tgc1_' || lpad('1', 32, '0'),"
        " 'tgc1_' || lpad('2', 32, '0'))"
    )
    assert (await tg.ok("telegram.messages.search", {"query": "status"}, principal=BETA_READER))[
        "partial_reasons"
    ] == []  # 128 exactly is not truncated


async def test_cursor_offset_cannot_be_changed(tg: Tg) -> None:
    first = await tg.ok("telegram.messages.search", {"query": "status", "limit": 5})
    offset, mac = first["next_cursor"].split(".")
    err = await tg.error("telegram.messages.search", {"query": "status", "limit": 5, "cursor": f"40.{mac}"})
    assert err == {
        "code": "invalid_input",
        "message": "cursor is not valid for this caller and query",
        "retryable": False,
    }
    assert offset == "5"


async def test_collect_with_a_broken_port_answer_is_internal_error(tg: Tg) -> None:
    orig = tg.service.collect

    async def broken(*a: Any) -> Any:
        got = await orig(*a)
        return dataclasses.replace(got, positions=got.positions[:-1])

    tg.service.collect = broken  # type: ignore[method-assign,assignment]
    err = await tg.error("telegram.messages.collect", {"chat_ref": REF["a-partners"]})
    assert err["code"] == "internal_error"


async def test_no_bundle_refuses_even_a_tenant_without_a_connection(env: Env, db: Db) -> None:
    shutil.rmtree(env.settings.bundle_dir)
    async with AsyncExitStack() as stack:
        t = await open_tg(stack, env, db)
        err = await t.error("telegram.messages.search", {"query": "x"}, principal="agent:t-gamma:reader")
        assert err["code"] == "temporarily_unavailable"


def test_keys_split_at_the_first_equals_sign() -> None:
    key = public_key(Ed25519PrivateKey.generate().public_key())
    assert list(server.parse_keys(f"a={key}", "X")) == ["a"]
    with pytest.raises(server.ConfigError):
        server.parse_keys(f"a=b={key}", "X")


def test_main_quiets_the_sdk_loggers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "serve", lambda _: None)
    names = ("mcp", "fastmcp", "httpx", "httpx2")  # uvicorn: its own log_level="warning" (configs)
    for name in names:
        logging.getLogger(name).setLevel(logging.NOTSET)
    assert server.main(clean_env(tmp_path) | BASE) == 0
    assert {n: logging.getLogger(n).level for n in names} == dict.fromkeys(names, logging.WARNING)


async def test_last_page_before_the_service_limit_is_not_truncated(tg: Tg) -> None:
    """offset + 2 * limit > 100 makes a page the last one; it is `truncated` only if the service had more."""
    granted = {seed.PEER[c] for c in ("a-partners", "a-sales", "a-gap")}
    total = sum(1 for m in seed.messages() if m["peer_id"] in granted and "budget" in (m["text"] or m["caption"]))
    assert 34 < total < 68  # fixtures: page 2 at limit 34 is the last one and holds the rest
    first = await tg.ok("telegram.messages.search", {"query": "budget", "limit": 34})
    second = await tg.ok("telegram.messages.search", {"query": "budget", "limit": 34, "cursor": first["next_cursor"]})
    assert len(first["items"]) + len(second["items"]) == total
    assert (second["next_cursor"], "truncated" in second["partial_reasons"]) == (None, False)


# the archive port itself (archive.py): exact rows, page boundaries, completeness, how it connects ---------------


async def test_port_rows_carry_edits_replies_and_utc_times(db: Db) -> None:
    db.sql(
        """UPDATE telegram_archive.messages SET edited_at = '2026-11-20T10:00:00+03:00', reply_to = 3
        WHERE peer_id = %s AND message_id = 5""",
        (seed.PEER["a-sales"],),
    )
    page = await ArchiveService(db.dsn).read_thread("svc-alpha", REF["a-sales"], 6, 1)
    (m,) = page.items
    sent = db.sql(
        "SELECT sent_at FROM telegram_archive.messages WHERE peer_id = %s AND message_id = 5", (seed.PEER["a-sales"],)
    )[0][0]
    assert (m.message_id, m.edited_at, m.reply_to) == (5, "2026-11-20T07:00:00Z", 3)
    assert m.sent_at == sent.isoformat().replace("+00:00", "Z") and m.sent_at.endswith("Z")
    newer = (await ArchiveService(db.dsn).read_thread("svc-alpha", REF["a-sales"], 7, 1)).items[0]
    assert (newer.message_id, newer.edited_at) == (6, None)


@pytest.mark.parametrize("method", ["search", "read_thread", "collect"])
async def test_port_more_is_exactly_past_the_page(db: Db, method: str) -> None:
    archive, ref, total = ArchiveService(db.dsn), REF["a-sales"], len(texts_of("a-sales"))

    async def page(limit: int) -> Any:
        if method == "search":
            return await archive.search("svc-alpha", "message", [ref], limit)
        if method == "read_thread":
            return await archive.read_thread("svc-alpha", ref, None, limit)
        return await archive.collect("svc-alpha", ref, None, limit)

    full, short = await page(total), await page(total - 1)
    assert (len(full.items), full.more, len(short.items), short.more) == (total, False, total - 1, True)
    if method == "search":
        assert (full.partial_reasons, short.partial_reasons) == ([], ["result_limit"])


async def test_port_completeness_names_each_reason(db: Db) -> None:
    archive = ArchiveService(db.dsn)
    gap = await archive.read_thread("svc-alpha", REF["a-gap"], None, 1)
    assert gap.partial_reasons == ["not_synced", "sync_gap"]
    db.sql("UPDATE telegram_archive.chats SET gap = false WHERE chat_ref = %s", (REF["a-gap"],))
    assert (await archive.read_thread("svc-alpha", REF["a-gap"], None, 1)).partial_reasons == ["not_synced"]
    db.sql("UPDATE telegram_archive.chats SET gap = true, state = 'synced' WHERE chat_ref = %s", (REF["a-gap"],))
    assert (await archive.read_thread("svc-alpha", REF["a-gap"], None, 1)).partial_reasons == ["sync_gap"]
    db.defer("svc-alpha", 30)
    assert (await archive.search("svc-alpha", "message", [REF["a-sales"]], 50)).partial_reasons == ["flood_wait"]
    with pytest.raises(ServiceError) as exc:
        await archive.collect("svc-alpha", REF["a-sales"], None, 5)
    assert exc.value.kind == "flood_wait" and 28 < float(exc.value.retry_after_seconds or 0) <= 30


async def test_port_connects_as_the_connector_with_its_application_name(tg: Tg, db: Db) -> None:
    """AC-M11-03's stand counter counts `application_name=gxg-telegram` connections; readiness uses its own name."""
    log = db.pg.root / "log"
    before = log.read_text(errors="replace").count("application_name=gxg-telegram")
    await tg.ok("telegram.thread.read", {"chat_ref": REF["a-sales"]})
    assert (await tg.http.get("/readyz")).status_code == 200
    lines = [ln for ln in log.read_text(errors="replace").splitlines() if "application_name=gxg-telegram" in ln]
    new = lines[before:]
    assert [ln.split("application_name=")[1] for ln in new] == ["gxg-telegram", "gxg-telegram-ready"]
    assert all(f"user={seed.READER}" in ln for ln in new)
