"""KB wrapper connector (M6), in-process: collection grants (AC-M6-02 logic, SC-05, SC-10 on KB), identity,
schemas, readiness. The same checks through ContextForge: stand/tests/test_kb.py.

Every call goes through the real KB adapter (kb_connector.backend) to the fake-KB MCP server of the audited KB
contract (kb_connector.fake_kb), both in process. The MCP client is the official SDK over Streamable HTTP on
httpx2's ASGITransport (no TCP socket). The bundle
is built by `gxg-policy build` from fixtures/grants/ with a key made here; identities come from the kit's test
issuer (GXG_PROFILE=test).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import time
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
from kb_connector import backend as backend_module
from kb_connector import server
from kb_connector import tools as tools_module
from kb_connector.backend import Backend, Connection, Document
from kb_connector.fake_kb import FakeKb, Key
from kb_connector.tools import Connector, load_schema

pytestmark = pytest.mark.anyio

REPO = next(p for p in Path(__file__).resolve().parents if (p / "policies").is_dir())
KB = REPO / "connectors" / "kb"
ALPHA_READER, ALPHA_NOBODY, BETA_READER = "agent:t-alpha:reader", "agent:t-alpha:nobody", "agent:t-beta:reader"
SALES = ["101", "102", "103"]  # t-alpha published sales, in KB order
HR, PUBLIC, DRAFT, ARCHIVED, BETA_SALES = "104", "105", "106", "107", "201"
WS_ALPHA, WS_BETA = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
KEYS = {"alpha-read": Key(WS_ALPHA, "read"), "beta-read": Key(WS_BETA, "read"), "alpha-write": Key(WS_ALPHA, "write")}
CONNECTIONS = {
    "t-alpha": Connection("kb-main", "http://fake-kb/mcp", WS_ALPHA, "alpha-read"),
    "t-beta": Connection("kb-main", "http://fake-kb/mcp", WS_BETA, "beta-read"),
}
KID = "kb-test"
SEARCH, GET, CONTEXT = "kb.documents.search", "kb.documents.get", "kb.context.get"


def public_key(key: Any) -> str:
    return "ed25519:" + base64.urlsafe_b64encode(key.public_bytes_raw()).rstrip(b"=").decode()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() must never serve in a test: a missed refusal fails at once instead of hanging on a real port."""

    def run(app: Any, **kw: Any) -> None:
        raise AssertionError("uvicorn.run reached")

    monkeypatch.setattr(server.uvicorn, "run", run)


def build_bundle(out: Path, monkeypatch: pytest.MonkeyPatch, *, built_at: str | None = None) -> str:
    """`gxg-policy build` over fixtures/grants/ + the example platform registry; returns KB_BUNDLE_KEYS."""
    root = out.parent / f"{out.name}-policies"
    (root / "tenants").mkdir(parents=True)
    (root / "platform").mkdir()
    for f in (KB / "fixtures" / "grants").glob("t-*.yaml"):
        (root / "tenants" / f.name).write_bytes(f.read_bytes())
    (root / "platform" / "registry.yaml").write_bytes((REPO / "policies/platform/registry.example.yaml").read_bytes())
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    monkeypatch.setenv(policy_cli.KEY_ENV, base64.urlsafe_b64encode(seed).rstrip(b"=").decode())
    args = ["build", str(root), "--out", str(out), "--bundle-version", "0" * 40, "--kid", KID]
    args += ["--manifest", str(KB / "gxg-manifest.yaml")] + (["--built-at", built_at] if built_at else [])
    assert policy_cli.main(args) == 0
    return f"{KID}={public_key(Ed25519PrivateKey.from_private_bytes(seed).public_key())}"


@dataclass
class Env:
    settings: server.Settings
    issuer: StubIssuer


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("GXG_PROFILE", "test")
    keys = build_bundle(tmp_path / "bundle", monkeypatch)
    issuer = StubIssuer()
    settings = server.Settings.from_env(
        {
            "GXG_PROFILE": "test",
            "KB_BUNDLE_DIR": str(tmp_path / "bundle"),
            "KB_BUNDLE_KEYS": keys,
            "KB_STATE_FILE": str(tmp_path / "state" / "deep" / "bundle-state.json"),  # parents are created
            "KB_TEST_ISSUER_KEY": public_key(issuer.public_key),
        }
    )
    return Env(settings, issuer)


class Kb:
    def __init__(
        self,
        connector: Connector,
        http: httpx2.AsyncClient,
        session: ClientSession,
        issuer: StubIssuer,
        fake: Any = None,
    ):
        self.connector, self.backend = connector, connector.backend
        self.fake: FakeKb = fake

        self.http, self.session, self.issuer = http, session, issuer

    def identity(self, principal: str, **kw: Any) -> dict[str, str]:
        return self.issuer.headers(tenant_id=principal.split(":")[1], principal=principal, audience="kb", **kw)

    async def call(
        self, tool: str, args: dict[str, Any], *, principal: str = ALPHA_READER, identity: dict[str, str] | None = None
    ) -> dict[str, Any]:
        extra = self.identity(principal) if identity is None else identity
        self.http.headers.update(extra)
        try:
            result = await self.session.call_tool(tool, args)
        finally:
            for k in extra:
                del self.http.headers[k]
        dumped: dict[str, Any] = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        return dumped

    async def ok(self, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        r = await self.call(tool, args, **kw)
        assert r.get("isError") is not True, r
        content: dict[str, Any] = r["structuredContent"]
        return content

    async def error(self, tool: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        r = await self.call(tool, args, **kw)
        assert r["isError"] is True, r
        content: dict[str, Any] = r["structuredContent"]
        return content


async def connect(stack: AsyncExitStack, app: Any) -> tuple[httpx2.AsyncClient, ClientSession]:
    await stack.enter_async_context(app.router.lifespan_context(app))
    http = await stack.enter_async_context(
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://kb")
    )
    read, write = await stack.enter_async_context(streamable_http_client("http://kb/mcp", http_client=http))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return http, session


@pytest.fixture
async def fake() -> AsyncIterator[FakeKb]:
    kb = FakeKb(dict(KEYS))
    async with kb.http.router.lifespan_context(kb.http):
        yield kb


def to(kb: FakeKb) -> Any:
    """The adapter's HTTP client, on the fake-KB's ASGI app instead of a socket."""

    def client(conn: Connection) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=kb), headers={"Authorization": f"Bearer {conn.key}"}
        )

    return client


@pytest.fixture
def backend(fake: FakeKb) -> Backend:
    return Backend(dict(CONNECTIONS), to(fake))


@pytest.fixture
async def kb(env: Env, backend: Backend, fake: FakeKb) -> AsyncIterator[Kb]:
    app, connector = server.build(env.settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        yield Kb(connector, http, session, env.issuer, fake)


def entity(id: int, title: str, body: str, *, type_id: int = 1, ws: str = WS_ALPHA, **kw: Any) -> dict[str, Any]:
    """A kb.entities row of the fake-KB."""
    return {
        "id": id,
        "workspace_id": ws,
        "entity_type_id": type_id,
        "title": title,
        "summary": "s",
        "body_md": body,
        "status": "published",
        "archived_at": None,
        "updated_at": "2026-10-09T00:00:00Z",
    } | kw


def serving(docs: list[Document]) -> Any:
    """A KB search that ignores the collections and returns `docs` (paged like the real port)."""

    async def search(tenant: str, collections: list[str], query: str, *, offset: int, limit: int) -> Any:
        return docs[offset : offset + limit], len(docs) > offset + limit, True

    return search


def ids(result: dict[str, Any]) -> list[str]:
    return [i["id"] for i in result["items"]]


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "kb_connector.audit"]


NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}
INTERNAL = {"code": "internal_error", "message": "internal error", "retryable": False}


# Tools, schemas, readiness --------------------------------------------------------------------------


async def test_three_read_tools_with_the_schema_files(kb: Kb) -> None:
    listed = {t.name: t for t in (await kb.session.list_tools()).tools}
    assert sorted(listed) == [CONTEXT, GET, SEARCH]
    for t in yaml.safe_load((KB / "gxg-manifest.yaml").read_text(encoding="utf-8"))["tools"]:
        assert listed[t["name"]].input_schema == load_schema(t["input_schema"])
        assert listed[t["name"]].output_schema == load_schema(t["output_schema"])
        assert listed[t["name"]].description == load_schema(t["input_schema"])["description"]
    for f in (KB / "schemas").glob("*.json"):
        jsonschema.Draft202012Validator.check_schema(load_schema(f"schemas/{f.name}"))
    assert {t.name: t.timeout for t in kb.connector.tools(server.NoVerifier(), lambda: None)} == {
        SEARCH: 15.0,
        GET: 10.0,
        CONTEXT: 15.0,
    }


async def test_results_match_the_output_schemas(kb: Kb) -> None:
    search = json.loads((REPO / "schemas" / "search-result.schema.json").read_text())
    jsonschema.validate(await kb.ok(SEARCH, {"query": "contract"}), search)
    get = json.loads((KB / "schemas" / "document-get.output.json").read_text())
    jsonschema.validate(await kb.ok(GET, {"document_id": SALES[0]}), get)
    ctx = json.loads((KB / "schemas" / "context-get.output.json").read_text())
    jsonschema.validate(await kb.ok(CONTEXT, {"query": "contract", "max_chars": 100}), ctx)


async def test_readyz_and_healthz(kb: Kb, env: Env, backend: Backend) -> None:
    assert (await kb.http.get("/healthz")).json() == {"status": "ok"}
    assert (await kb.http.get("/readyz")).status_code == 200
    for f in env.settings.bundle_dir.iterdir():
        f.unlink()
    settings = server.Settings(**{**vars(env.settings), "state_file": env.settings.state_file.with_name("other")})
    app, connector = server.build(settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        assert (await http.get("/readyz")).status_code == 503  # GR-12: no bundle, not ready
        k = Kb(connector, http, session, env.issuer)
        assert (await k.error(SEARCH, {"query": "contract"}))["code"] == "temporarily_unavailable"
    assert connector.backend.calls.total() == 0


async def test_stale_bundle_is_not_ready(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    keys = build_bundle(tmp_path / "old", monkeypatch, built_at="2026-01-01T00:00:00Z")  # GR-13: older than 24 h
    settings = server.Settings(
        **{**vars(env.settings), "bundle_dir": tmp_path / "old", "bundle_keys": keys, "state_file": tmp_path / "s"}
    )
    app, _ = server.build(settings, backend)
    async with AsyncExitStack() as stack:
        http, _ = await connect(stack, app)
        assert (await http.get("/readyz")).status_code == 503


async def test_input_is_validated(kb: Kb) -> None:
    for tool, args in ((SEARCH, {}), (SEARCH, {"query": "x", "limit": 51}), (GET, {"document_id": ""})):
        err = await kb.error(tool, args)
        assert err["code"] == "invalid_input", (tool, args)
    assert kb.backend.calls.total() == 0


# AC-M6-02: only granted collections ---------------------------------------------------------------


async def test_search_returns_only_the_granted_collection(kb: Kb) -> None:
    # "contract" matches sales, hr (expired grant), public (no grant), a sales draft and an archived sales row of
    # t-alpha, and t-beta's sales. The KB returns the draft and the archived row too (G8): the wrapper drops them (Q3).
    r = await kb.ok(SEARCH, {"query": "contract"})
    assert ids(r) == ["101", "102"]
    assert r["source_refs"] == ["kb:kb-main/collection/sales"]
    assert kb.fake.calls == {"kb_search": 1, "kb_list_entity_types": 1}  # types once: entity_type_id -> collection
    await kb.ok(SEARCH, {"query": "contract"})
    assert kb.fake.calls == {"kb_search": 2, "kb_list_entity_types": 1}  # the types are cached


async def test_search_query_to_the_kb_is_narrowed(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], int, int]] = []
    real = kb.backend.search

    async def search(tenant: str, collections: list[str], query: str, *, offset: int, limit: int) -> Any:
        seen.append((collections, offset, limit))
        return await real(tenant, collections, query, offset=offset, limit=limit)

    monkeypatch.setattr(kb.backend, "search", search)
    first = await kb.ok(SEARCH, {"query": "e", "limit": 2})
    await kb.ok(SEARCH, {"query": "e", "limit": 2, "cursor": first["next_cursor"]})
    await kb.ok(CONTEXT, {"query": "e", "max_documents": 3})
    # GR-16: the filter goes to the KB, not only after it; the KB returns one page, not every match (F5)
    assert seen == [(["sales"], 0, 2), (["sales"], 2, 2), (["sales"], 0, 3)]


async def test_every_search_item_is_checked_again(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    every = [
        Document(d, c, "t", "contract", "2026-10-01T00:00:00Z")
        for d, c in zip(["101", "102", "103", "104", "105"], ["sales", "sales", "sales", "hr", "public"], strict=True)
    ]
    monkeypatch.setattr(kb.backend, "search", serving(every))  # a KB that ignores the filter
    r = await kb.ok(SEARCH, {"query": "anything", "limit": 50})
    assert sorted(ids(r)) == SALES
    ctx = await kb.ok(CONTEXT, {"query": "anything"})
    assert sorted(d["id"] for d in ctx["documents"]) == SALES


async def test_item_of_a_granted_name_but_failing_check_is_dropped(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = kb.connector.grants.check

    def check(*a: Any) -> Any:
        calls["n"] += 1
        return real(*a) if calls["n"] == 1 else real(a[0], a[1], a[2], "kb:kb-main/collection/none")

    monkeypatch.setattr(kb.connector.grants, "check", check)
    assert (await kb.ok(SEARCH, {"query": "contract"}))["items"] == []


async def test_named_collection_without_grant_is_not_found_before_the_kb(kb: Kb) -> None:
    for collections in (["hr"], ["public"], ["sales", "hr"], ["nope"], ["sales/../hr"], ["*"]):
        assert await kb.error(SEARCH, {"query": "contract", "collections": collections}) == NOT_FOUND, collections
        assert await kb.error(CONTEXT, {"query": "contract", "collections": collections}) == NOT_FOUND
    assert kb.backend.calls.total() == 0  # GR-17 and zero KB calls on refusals
    r = await kb.ok(SEARCH, {"query": "contract", "collections": ["sales", "sales"]})
    assert sorted(ids(r)) == ["101", "102"]
    assert (await kb.ok(SEARCH, {"query": "contract", "collections": []}))["items"] == []


async def test_get_other_collection_or_tenant_is_not_found_like_a_missing_one(kb: Kb) -> None:
    assert (await kb.ok(GET, {"document_id": SALES[0]}))["collection"] == "sales"
    replies = [await kb.call(GET, {"document_id": d}) for d in (HR, PUBLIC, BETA_SALES, "999")]
    assert all(r == replies[-1] for r in replies), replies  # SC-05: byte for byte as for a missing document
    assert replies[-1]["structuredContent"] == NOT_FOUND


async def test_get_collection_comes_from_the_kb(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await kb.backend.get_document("t-alpha", HR)).collection == "hr"  # type: ignore[union-attr]
    r = await kb.call(GET, {"document_id": HR, "collection": "sales"})  # GR-01: the argument is ignored
    assert r["structuredContent"] == NOT_FOUND


async def test_expired_grant_gives_nothing(kb: Kb) -> None:
    assert await kb.error(GET, {"document_id": HR}) == NOT_FOUND  # grant on hr expired 2026-01-01 (GR-06)
    assert HR not in ids(await kb.ok(SEARCH, {"query": "salary"}))


async def test_tenants_with_the_same_collection_name_do_not_mix(kb: Kb) -> None:
    beta = await kb.ok(SEARCH, {"query": "contract"}, principal=BETA_READER)
    assert ids(beta) == [BETA_SALES]  # t-beta's own `sales`: its key and workspace (Q2)
    assert kb.fake.calls["kb_list_entity_types"] == 1  # mask collection/*: the collections come from the KB
    assert await kb.error(GET, {"document_id": SALES[0]}, principal=BETA_READER) == NOT_FOUND
    alpha = await kb.ok(SEARCH, {"query": "beta"})
    assert BETA_SALES not in ids(alpha)


async def test_nobody_does_not_reach_the_kb(kb: Kb) -> None:
    assert (await kb.ok(SEARCH, {"query": "contract"}, principal=ALPHA_NOBODY))["items"] == []
    ctx = await kb.ok(CONTEXT, {"query": "contract"}, principal=ALPHA_NOBODY)
    assert ctx["documents"] == [] and ctx["context"] == ""
    assert await kb.error(GET, {"document_id": SALES[0]}, principal=ALPHA_NOBODY) == NOT_FOUND
    assert kb.backend.calls.total() == 0  # no grant on any collection: not even the collection lookup


async def test_tenant_without_kb_connection_sees_nothing(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(kb.backend.connections, "t-alpha", "other")  # not a connection of t-alpha in the bundle
    assert (await kb.ok(SEARCH, {"query": "contract"}))["items"] == []
    assert await kb.error(SEARCH, {"query": "contract", "collections": ["sales"]}) == NOT_FOUND
    assert await kb.error(GET, {"document_id": SALES[0]}) == NOT_FOUND


async def test_identity_in_arguments_changes_nothing(kb: Kb) -> None:
    args = {"query": "contract", "tenant_id": "t-beta", "agent_id": BETA_READER, "principal": BETA_READER}
    assert sorted(ids(await kb.ok(SEARCH, args))) == ["101", "102"]  # SC-03


async def test_bad_identity_is_refused_before_the_kb(kb: Kb) -> None:
    bad = {
        "none": {},
        "foreign issuer": StubIssuer().headers(tenant_id="t-alpha", principal=ALPHA_READER, audience="kb"),
        "expired": kb.identity(ALPHA_READER, now=lambda: time.time() - 3600),
        "other audience": kb.issuer.headers(tenant_id="t-alpha", principal=ALPHA_READER, audience="demo"),
    }
    for name, identity in bad.items():
        for tool, args in ((SEARCH, {"query": "contract"}), (GET, {"document_id": SALES[0]})):
            err = await kb.error(tool, args, identity=identity)
            assert err["code"] == "unauthenticated", name
    assert kb.backend.calls.total() == 0  # SC-04


# Pagination, context, KB failures, audit ------------------------------------------------------------


async def test_pages_and_cursor_binding(kb: Kb) -> None:
    first = await kb.ok(SEARCH, {"query": "e", "limit": 2}, principal=ALPHA_READER)
    assert ids(first) == SALES[:2] and first["next_cursor"] is not None
    last = await kb.ok(SEARCH, {"query": "e", "limit": 2, "cursor": first["next_cursor"]})
    assert ids(last) == SALES[2:] and last["next_cursor"] is None
    for args, principal in (
        ({"query": "other", "limit": 2}, ALPHA_READER),
        ({"query": "e", "limit": 3}, ALPHA_READER),
        ({"query": "e", "limit": 2}, ALPHA_NOBODY),
        ({"query": "e", "limit": 2, "collections": ["sales"]}, ALPHA_READER),
    ):
        err = await kb.error(SEARCH, {**args, "cursor": first["next_cursor"]}, principal=principal)
        assert err["code"] == "invalid_input", args
    assert (await kb.error(SEARCH, {"query": "e", "cursor": "1.zz"}))["code"] == "invalid_input"
    exact = await kb.ok(SEARCH, {"query": "e", "limit": 3})
    assert ids(exact) == SALES and exact["next_cursor"] is None


async def test_context_joins_excerpts_within_the_budget(kb: Kb) -> None:
    full = await kb.ok(CONTEXT, {"query": "contract"})
    docs = {str(e["id"]): e for e in kb.fake.entities}
    expected = "\n\n".join(f"## {docs[i]['title']}\n{docs[i]['body_md']}" for i in ("101", "102"))
    assert full["context"] == expected
    assert full["complete"] is True and full["partial_reasons"] == []
    assert [d["id"] for d in full["documents"]] == ["101", "102"]
    assert full["source_refs"] == ["kb:kb-main/collection/sales"]

    cut = await kb.ok(CONTEXT, {"query": "contract", "max_chars": 100})
    assert cut["context"] == expected[:100] and len(cut["context"]) == 100
    assert cut["complete"] is False and cut["partial_reasons"] == ["truncated"]

    one = await kb.ok(CONTEXT, {"query": "contract", "max_documents": 1})
    assert [d["id"] for d in one["documents"]] == ["101"] and one["complete"] is False

    exact = len(f"## {docs['101']['title']}\n{docs['101']['body_md']}")
    assert exact < 100
    edge = await kb.ok(CONTEXT, {"query": "contract", "max_chars": 100})
    assert [d["id"] for d in edge["documents"]] == ["101", "102"]  # the second one is cut, not dropped


async def test_context_stops_when_the_budget_is_spent(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    long = Document("d-long", "sales", "T", "x" * 200, "2026-10-01T00:00:00Z")
    monkeypatch.setattr(kb.backend, "search", serving([long, long]))
    r = await kb.ok(CONTEXT, {"query": "x", "max_chars": 100})
    assert len(r["documents"]) == 1 and r["context"] == f"## T\n{'x' * 200}"[:100]
    assert r["complete"] is False
    short = Document("d-short", "sales", "T", "a" * 96, "2026-10-01T00:00:00Z")  # "## T\naaa…": 101 characters
    monkeypatch.setattr(kb.backend, "search", serving([short, short, short]))
    r = await kb.ok(CONTEXT, {"query": "x", "max_chars": 101 + 2 + 1})  # room for 1 character of the second one
    assert len(r["documents"]) == 2 and r["context"] == f"## T\n{'a' * 96}\n\n#" and r["complete"] is False
    r = await kb.ok(CONTEXT, {"query": "x", "max_chars": 101 * 3 + 2 * 2})  # exactly three
    assert len(r["documents"]) == 3 and r["complete"] is True


async def test_kb_failures_are_mcc05_codes(kb: Kb) -> None:
    kb.fake.http_status = 429  # with Retry-After: 7
    err = await kb.error(SEARCH, {"query": "contract"})
    assert err == {"code": "rate_limited", "message": "KB rate limit", "retryable": True, "retry_after_s": 7}
    for status in (502, 503, 504, 500):
        kb.fake.http_status = status
        assert await kb.error(GET, {"document_id": SALES[0]}) == {
            "code": "temporarily_unavailable",
            "message": "KB is not available",
            "retryable": True,
        }
    for status in (401, 403):  # the wrapper's own key is refused: not the caller's identity
        kb.fake.http_status = status
        assert await kb.error(GET, {"document_id": SALES[0]}) == INTERNAL
    kb.fake.http_status, kb.fake.retry_after = 429, None
    assert (await kb.error(GET, {"document_id": SALES[0]}))["retry_after_s"] == 5  # no Retry-After: the default
    kb.fake.retry_after = "soon"
    assert (await kb.error(GET, {"document_id": SALES[0]}))["retry_after_s"] == 5


async def test_unexpected_error_is_internal_error_without_its_text(
    kb: Kb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*_: Any) -> Any:
        raise RuntimeError("service key kbk_live_SECRET leaked")

    monkeypatch.setattr(kb.backend, "get_document", boom)
    caplog.set_level(logging.INFO)
    err = await kb.error(GET, {"document_id": SALES[0]})
    assert err == {"code": "internal_error", "message": "internal error", "retryable": False}
    assert "SECRET" not in caplog.text


async def test_audit_line_per_call_without_query_or_content(kb: Kb, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    await kb.ok(SEARCH, {"query": "pipeline"})
    await kb.error(GET, {"document_id": HR})
    await kb.error(SEARCH, {"query": "pipeline", "collections": ["hr"]})
    lines = audit_lines(caplog)
    assert [(a["tool"], a["outcome"], a["kb_calls"]) for a in lines] == [
        (SEARCH, "ok", 2),  # kb_list_entity_types (first call, then cached) + kb_search
        (GET, "not_found", 1),  # kb_get: the KB names the collection (hr), then no grant
        (SEARCH, "not_found", 0),
    ]
    assert lines[0]["grant_id"] == ["alpha-reader-kb-sales"]
    assert lines[0]["tenant"] == "t-alpha" and lines[0]["principal"] == ALPHA_READER
    assert "pipeline" not in caplog.text and "Pipeline review notes" not in caplog.text


# Configuration ----------------------------------------------------------------------------------------


def test_configuration_is_checked() -> None:
    with pytest.raises(server.ConfigError, match=r"^the test issuer is allowed only with GXG_PROFILE=test$"):
        server.Settings.from_env({"KB_TEST_ISSUER_KEY": "x"})
    with pytest.raises(server.ConfigError, match=r"^KB_TEST_ISSUER_KEY and KB_IDENTITY_KEYS are exclusive$"):
        server.Settings.from_env({"GXG_PROFILE": "test", "KB_TEST_ISSUER_KEY": "x", "KB_IDENTITY_KEYS": "a=b"})
    s = server.Settings.from_env({})
    assert (s.profile, s.bundle_dir) == ("production", Path("/bundle"))
    assert s.state_file == Path("/var/lib/kb/bundle-state.json")
    with pytest.raises(server.ConfigError, match=r"^KB_BUNDLE_KEYS: expected kid=ed25519:<base64url>$"):
        server.parse_keys("nokey", "KB_BUNDLE_KEYS")
    with pytest.raises(server.ConfigError, match=r"^KB_IDENTITY_KEYS: \S"):
        server.parse_keys("a=ed25519:AAAA", "KB_IDENTITY_KEYS")
    with pytest.raises(TokenError):
        server.NoVerifier().verify({})


async def test_production_without_identity_keys_refuses_every_call(env: Env, backend: Backend) -> None:
    settings = server.Settings(**{**vars(env.settings), "profile": "production", "test_issuer_key": None})
    app, connector = server.build(settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        err = await Kb(connector, http, session, env.issuer).error(SEARCH, {"query": "contract"})
        assert err["code"] == "unauthenticated"
    assert connector.backend.calls.total() == 0


def test_main_refuses_to_start_on_bad_config(caplog: pytest.LogCaptureFixture) -> None:
    assert server.main({"KB_TEST_ISSUER_KEY": "x"}) == 2
    assert "startup_refused" in caplog.text


# Exact envelopes, defaults and audit fields (DoD-4: mutmut) ---------------------------------------


def many(kb: Kb, n: int, content: str = "contract") -> None:
    """n more sales documents of t-alpha, ids after the fixtures."""
    kb.fake.entities += [entity(300 + i, f"Extra {i}", content) for i in range(n)]


async def test_default_limits(kb: Kb) -> None:
    many(kb, 25)
    page = await kb.ok(SEARCH, {"query": "contract"})
    assert len(page["items"]) == 20 and page["next_cursor"] is not None  # limit default 20
    assert page["complete"] is True and page["partial_reasons"] == []
    ctx = await kb.ok(CONTEXT, {"query": "contract"})
    assert len(ctx["documents"]) == 5  # max_documents default 5
    kb.fake.entities[:] = [entity(900, "Big", "x" * 9000)]
    big = await kb.ok(CONTEXT, {"query": "x"})
    assert len(big["context"]) == 4000  # max_chars default 4000


async def test_context_with_exactly_max_documents_is_complete(kb: Kb) -> None:
    r = await kb.ok(CONTEXT, {"query": "contract", "max_documents": 2})
    assert len(r["documents"]) == 2 and r["complete"] is True


async def test_context_no_room_left_stops_without_a_separator(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = Document("d-short", "sales", "T", "a" * 96, "2026-10-01T00:00:00Z")  # 101 characters
    monkeypatch.setattr(kb.backend, "search", serving([doc, doc]))
    r = await kb.ok(CONTEXT, {"query": "x", "max_chars": 103})  # 2 left: exactly the separator, no room
    assert r["context"] == f"## T\n{'a' * 96}" and len(r["documents"]) == 1 and r["complete"] is False


async def test_get_result_and_errors_exactly(kb: Kb) -> None:
    got = await kb.ok(GET, {"document_id": SALES[0]})
    assert got == {
        "id": "101",
        "collection": "sales",
        "title": "Pricing 2027",
        "content": "Pricing plan: three tiers. Contract renewal in Q2.",
        "truncated": False,
        "updated_at": "2026-10-01T09:00:00Z",
        "source_ref": "kb:kb-main/collection/sales",
    }
    assert await kb.error(SEARCH, {"query": "x", "limit": 0}) == {
        "code": "invalid_input",
        "message": "arguments do not match the input schema",
        "retryable": False,
        "details": {"path": "/limit"},
    }
    assert (await kb.error(SEARCH, {"query": "x", "collections": [""]}))["details"] == {"path": "/collections/0"}
    assert await kb.error(SEARCH, {"query": "x", "cursor": "0." + "0" * 64}) == {
        "code": "invalid_input",
        "message": "cursor is not valid for this caller and query",
        "retryable": False,
    }
    kb.fake.http_status = 503
    assert await kb.error(SEARCH, {"query": "contract"}) == {
        "code": "temporarily_unavailable",
        "message": "KB is not available",
        "retryable": True,
    }


async def test_cursor_offset_is_signed(kb: Kb) -> None:
    first = await kb.ok(SEARCH, {"query": "e", "limit": 1})
    offset, mac = first["next_cursor"].split(".")
    assert offset == "1"
    assert (await kb.error(SEARCH, {"query": "e", "limit": 1, "cursor": f"2.{mac}"}))["code"] == "invalid_input"


async def test_tool_timeout_from_the_manifest(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []

    async def wait_for(aw: Any, timeout: float | None) -> Any:
        seen.append(timeout)
        aw.close()
        raise TimeoutError

    monkeypatch.setattr(tools_module.asyncio, "wait_for", wait_for)
    assert await kb.error(GET, {"document_id": SALES[0]}) == {
        "code": "temporarily_unavailable",
        "message": "KB did not answer in time",
        "retryable": True,
    }
    assert seen == [10.0]


async def test_identity_is_bound_to_the_tool_and_arguments(
    env: Env, monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    seen: list[dict[str, Any]] = []
    real = server.StubVerifier.verify

    def verify(self: Any, headers: Any, **binding: Any) -> Any:
        seen.append(binding)
        return real(self, headers, **binding)

    monkeypatch.setattr(server.StubVerifier, "verify", verify)
    app, connector = server.build(env.settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        await Kb(connector, http, session, env.issuer).ok(GET, {"document_id": SALES[0]})
    assert seen == [{"tool": GET, "arguments": {"document_id": SALES[0]}}]


async def test_no_bundle_get_does_not_reach_the_kb(env: Env, backend: Backend) -> None:
    for f in env.settings.bundle_dir.iterdir():
        f.unlink()
    settings = server.Settings(**{**vars(env.settings), "state_file": env.settings.state_file.with_name("other")})
    app, connector = server.build(settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        k = Kb(connector, http, session, env.issuer)
        assert (await k.error(GET, {"document_id": SALES[0]}))["code"] == "temporarily_unavailable"
        del connector.backend.connections["t-alpha"]  # even where no grant lookup would happen
        err = await k.error(SEARCH, {"query": "x", "collections": ["sales"]})
        assert err["code"] == "temporarily_unavailable"
    assert connector.backend.calls.total() == 0  # GR-12 before the KB lookup of the collection


async def test_tenant_without_a_kb(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(kb.backend.connections, "t-alpha")
    assert await kb.error(GET, {"document_id": SALES[0]}) == NOT_FOUND
    assert (await kb.ok(SEARCH, {"query": "contract"}))["items"] == []
    assert await kb.error(SEARCH, {"query": "contract", "collections": ["sales"]}) == NOT_FOUND
    assert (await kb.ok(SEARCH, {"query": "contract", "collections": []}))["items"] == []
    assert kb.backend.calls.total() == 0


async def test_backend_counts_calls_and_knows_only_its_tenants(backend: Backend) -> None:
    b = backend
    assert await b.list_collections("t-gamma") == [] and await b.get_document("t-gamma", SALES[0]) is None
    assert await b.search("t-gamma", ["sales"], "contract", offset=0, limit=5) == ([], False, True)
    assert b.calls.total() == 0  # no connection for the tenant: no KB call
    # KB order (id): 101, 102 sales, 104 hr, 106 draft, 107 archived; the wrapper keeps 101, 102, 104
    page, more, complete = await b.search("t-alpha", ["sales", "hr"], "contract", offset=1, limit=1)
    assert [d.id for d in page] == ["102"] and more is True and complete is True
    page, more, complete = await b.search("t-alpha", ["sales", "hr"], "contract", offset=2, limit=1)
    assert [(d.id, d.collection) for d in page] == [("104", "hr")] and more is False and complete is True
    assert b.calls == {"kb_list_entity_types": 1, "kb_search": 2}


async def test_audit_lines_exactly(kb: Kb, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    ident = kb.identity(ALPHA_READER)
    request_id = json.loads(base64.urlsafe_b64decode(ident["x-gxg-test-identity"].split(".")[1] + "=="))["jti"]
    await kb.ok(GET, {"document_id": SALES[0]}, identity=ident)
    await kb.ok(CONTEXT, {"query": "contract"})
    await kb.error(GET, {"document_id": SALES[0]}, identity={})
    lines = audit_lines(caplog)
    assert all(isinstance(a["duration_ms"], int) and 0 <= a["duration_ms"] < 5000 for a in lines)
    assert [{k: v for k, v in a.items() if k != "duration_ms"} for a in lines[:1]] == [
        {
            "request_id": request_id,
            "tool": GET,
            "tenant": "t-alpha",
            "principal": ALPHA_READER,
            "bundle_version": "0" * 40,  # GR-14: the bundle that allowed it
            "grant_id": ["alpha-reader-kb-sales"],
            "outcome": "ok",
            "kb_calls": 2,  # kb_list_entity_types + kb_get
        }
    ]
    assert lines[1]["grant_id"] == ["alpha-reader-kb-sales"] and lines[1]["outcome"] == "ok"
    refused = lines[2]
    assert refused["outcome"] == "unauthenticated" and refused["reason"] == "missing"
    assert re.fullmatch(r"[0-9a-f-]{36}", refused["request_id"]) and "tenant" not in refused
    assert "bundle_version" not in refused  # refused before the bundle check


async def test_slow_call_duration_is_in_milliseconds(
    kb: Kb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = kb.backend.get_document

    async def slow(*a: Any) -> Any:
        await asyncio.sleep(0.3)
        return await real(*a)

    monkeypatch.setattr(kb.backend, "get_document", slow)
    caplog.set_level(logging.INFO)
    await kb.ok(GET, {"document_id": SALES[0]})
    assert 300 <= audit_lines(caplog)[-1]["duration_ms"] < 2000


async def test_internal_error_frames_go_to_the_errors_log(
    kb: Kb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*_: Any) -> Any:
        raise KeyError("x")

    monkeypatch.setattr(kb.backend, "get_document", boom)
    caplog.set_level(logging.INFO)
    await kb.error(GET, {"document_id": SALES[0]})
    [line] = audit_lines(caplog)
    assert line["outcome"] == "internal_error" and line["error"] == "KeyError"
    [record] = [r for r in caplog.records if r.name == "kb_connector.errors"]
    head, frames = record.getMessage().split("\n", 1)
    assert head == f"internal_error {line['request_id']} KeyError"
    lines = frames.split("\n")
    assert len(lines) >= 2 and all(re.fullmatch(r"\S+:\d+ in \S+", f) for f in lines), lines
    assert all(f.startswith("/") for f in lines), lines  # one "<absolute path>:<line> in <name>" per line
    assert lines[-1].endswith(" in boom") and "/test_kb.py:" in lines[-1]


# Cycle 2: a slow KB, long documents, readiness, the production verifier, logging, main() ---------------


async def test_a_slow_kb_times_out_by_the_manifest(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: Backend
) -> None:
    manifest = yaml.safe_load((KB / "gxg-manifest.yaml").read_text())
    for t in manifest["tools"]:
        t["limits"]["timeout_s"] = 0.2
    (tmp_path / "gxg-manifest.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(tools_module, "MANIFEST", tmp_path / "gxg-manifest.yaml")
    backend._client = to(slow := FakeKb(dict(KEYS)))
    slow.delay_s = 5  # the async port is cancelled by asyncio.wait_for, a hung KB does not block the process
    app, connector = server.build(env.settings, backend)
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        k = Kb(connector, http, session, env.issuer)
        started = time.monotonic()
        for tool, args in ((GET, {"document_id": SALES[0]}), (SEARCH, {"query": "x"}), (CONTEXT, {"query": "x"})):
            assert await k.error(tool, args) == {
                "code": "temporarily_unavailable",
                "message": "KB did not answer in time",
                "retryable": True,
            }
        assert time.monotonic() - started < 3


async def test_long_kb_documents_are_cut_to_the_output_schemas(kb: Kb) -> None:
    kb.fake.entities.append(entity(900, "T" * 400, "contract " + "x" * 30000))
    got = await kb.ok(GET, {"document_id": "900"})
    assert len(got["title"]) == 300 and len(got["content"]) == 20000 and got["truncated"] is True
    jsonschema.validate(got, load_schema("schemas/document-get.output.json"))
    found = await kb.ok(SEARCH, {"query": "contract", "limit": 50})
    item = next(i for i in found["items"] if i["id"] == "900")
    assert len(item["title"]) == 300 and len(item["excerpt"]) == 500
    jsonschema.validate(found, load_schema("../../schemas/search-result.schema.json"))
    ctx = await kb.ok(CONTEXT, {"query": "xxx", "max_chars": 8000})
    assert ctx["documents"] == [{"id": "900", "title": "T" * 300, "source_ref": "kb:kb-main/collection/sales"}]
    assert ctx["context"].startswith("## " + "T" * 300 + "\n") and len(ctx["context"]) == 8000
    jsonschema.validate(ctx, load_schema("schemas/context-get.output.json"))
    kb.fake.entities.append(entity(901, "E", "y" * 20000))
    assert (await kb.ok(GET, {"document_id": "901"}))["truncated"] is False


async def test_readyz_needs_the_kb(kb: Kb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    kb.fake.http_status = 503
    r = await kb.http.get("/readyz")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})
    events = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kb_connector"]
    assert events[-1] == {"event": "not_ready", "reasons": ["kb_not_ready"]}
    kb.fake.http_status = None
    r = await kb.http.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})


def cf_identity(key: Ed25519PrivateKey, tool: str, args: dict[str, Any], *, aud: str = "kb", kid: str = "cf-1") -> str:
    now = int(time.time())
    claims = {
        "iss": cf.ISSUER,
        "aud": aud,
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


async def test_contextforge_identity_keys_select_the_kit_verifier(
    env: Env, caplog: pytest.LogCaptureFixture, backend: Backend
) -> None:
    caplog.set_level(logging.INFO)
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    keys = f"cf-0={public_key(other.public_key())}, cf-1={public_key(key.public_key())}"
    settings = server.Settings(
        **{**vars(env.settings), "profile": "production", "test_issuer_key": None, "identity_keys": keys}
    )
    app, connector = server.build(settings, backend)
    args = {"document_id": SALES[0]}
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        k = Kb(connector, http, session, env.issuer)
        assert (await k.ok(GET, args, identity={"x-gxg-identity": cf_identity(key, GET, args)}))["id"] == SALES[0]
        second = cf_identity(other, GET, args, kid="cf-0")  # every configured kid is accepted
        assert (await k.ok(GET, args, identity={"x-gxg-identity": second}))["id"] == SALES[0]
        for bad in (
            cf_identity(key, GET, args, aud="demo"),  # the connector id is the audience
            cf_identity(key, SEARCH, args),  # signed for another tool
            cf_identity(key, GET, {"document_id": SALES[1]}),  # for other arguments
            cf_identity(Ed25519PrivateKey.generate(), GET, args),
        ):
            assert (await k.error(GET, args, identity={"x-gxg-identity": bad}))["code"] == "unauthenticated"
        assert (await k.error(GET, args))["code"] == "unauthenticated"  # the test issuer is not accepted
    reasons = [a.get("reason") for a in audit_lines(caplog) if a["outcome"] == "unauthenticated"]
    assert reasons[0] == "aud" and len(reasons) == 5


async def test_bundle_is_reloaded_when_its_files_change(
    kb: Kb, env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    assert (await kb.http.get("/readyz")).status_code == 200
    bundle = env.settings.bundle_dir / server.BUNDLE_FILE
    good = bundle.read_bytes()
    bundle.write_bytes(good.replace(b"alpha-reader-kb-sales", b"alpha-reader-kb-SALES"))  # signature now fails
    os.utime(bundle, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert (await kb.http.get("/readyz")).status_code == 200  # the previous bundle stays loaded
    events = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kb_connector"]
    assert events[-1]["event"] == "bundle_rejected" and "signature" in events[-1]["reason"]
    assert (await kb.ok(GET, {"document_id": SALES[0]}))["id"] == SALES[0]
    os.utime(bundle, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
    await kb.http.get("/readyz")
    assert (
        sum(
            json.loads(m.getMessage()).get("event") == "bundle_rejected"
            for m in caplog.records
            if m.name == "kb_connector"
        )
        == 2
    )  # a new mtime is read again
    await kb.http.get("/readyz")
    assert (
        sum(
            json.loads(m.getMessage()).get("event") == "bundle_rejected"
            for m in caplog.records
            if m.name == "kb_connector"
        )
        == 2
    )  # the same files are not read again
    keys = build_bundle(env.settings.bundle_dir.parent / "fresh", monkeypatch)
    assert keys
    for name in (server.BUNDLE_FILE, server.SIG_FILE):  # a valid bundle under another key: rejected too
        (env.settings.bundle_dir / name).write_bytes((env.settings.bundle_dir.parent / "fresh" / name).read_bytes())
    await kb.http.get("/readyz")
    assert [json.loads(m.getMessage())["event"] for m in caplog.records if m.name == "kb_connector"][-1] == (
        "bundle_rejected"
    )


def test_bundle_files_missing_is_not_an_error(tmp_path: Path) -> None:
    store = server.BundleStore({}, state_file=tmp_path / "s")
    files = server.BundleFiles(store, tmp_path)
    files.refresh()  # nothing there: no exception, nothing loaded
    assert not store.readiness().ready
    (tmp_path / server.BUNDLE_FILE).write_text("{}")
    files.refresh()  # only one of the two files: still nothing
    assert not store.readiness().ready


def test_log_handlers_split_audit_and_frames(capsys: pytest.CaptureFixture[str]) -> None:
    out, err = server.log_handlers()
    for name, msg in (("kb_connector.audit", '{"a": 1}'), ("kb_connector.errors", "internal_error x\nf.py:1 in g")):
        record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)
        for h in (out, err):
            if h.filter(record):
                h.handle(record)
    captured = capsys.readouterr()
    assert captured.out == '{"a": 1}\n' and captured.err == "internal_error x\nf.py:1 in g\n"
    # basicConfig gives a handler without a formatter "LEVEL:name:message": each one has its own, message only
    record = logging.LogRecord("kb_connector.audit", logging.INFO, __file__, 1, '{"b": 2}', None, None)
    assert [h.formatter is not None and h.formatter.format(record) for h in (out, err)] == ['{"b": 2}'] * 2


def test_main_starts_the_app_with_logging(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr(server.logging, "basicConfig", lambda **kw: configured.append(kw))
    env_vars = {
        "GXG_PROFILE": "test",
        "KB_BUNDLE_DIR": str(env.settings.bundle_dir),
        "KB_BUNDLE_KEYS": env.settings.bundle_keys,
        "KB_STATE_FILE": str(env.settings.state_file),
        "KB_TEST_ISSUER_KEY": env.settings.test_issuer_key or "",
    }
    assert server.main(env_vars) == 0
    [(app, kw)] = calls
    assert kw == {"host": "0.0.0.0", "port": 8080, "log_level": "warning"}  # noqa: S104
    assert app.__class__.__name__ == "StarletteWithLifespan" or hasattr(app, "router")
    [cfg] = configured
    assert cfg["level"] == logging.INFO and len(cfg["handlers"]) == 2


def test_main_refuses_bad_keys(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: started.append(app))  # a refusal never serves
    base = {"KB_STATE_FILE": str(tmp_path / "s.json"), "KB_BUNDLE_DIR": str(tmp_path)}
    for name in ("KB_IDENTITY_KEYS", "KB_BUNDLE_KEYS"):
        assert server.main(base | {name: "x"}) == 2
        assert json.loads(caplog.records[-1].getMessage()) == {
            "event": "startup_refused",
            "reason": f"{name}: expected kid=ed25519:<base64url>",
        }
    assert server.main(base | {"KB_IDENTITY_KEYS": "=ed25519:AAAA"}) == 2  # an empty kid
    assert started == []


def test_settings_from_the_environment() -> None:
    s = server.Settings.from_env(
        {"KB_BUNDLE_DIR": "/b", "KB_BUNDLE_KEYS": "k", "KB_STATE_FILE": "/s", "KB_IDENTITY_KEYS": "i"}
    )
    assert (s.bundle_dir, s.bundle_keys, s.state_file, s.identity_keys, s.test_issuer_key) == (
        Path("/b"),
        "k",
        Path("/s"),
        "i",
        None,
    )
    assert server.Settings.from_env({"GXG_PROFILE": "test", "KB_TEST_ISSUER_KEY": "t"}).test_issuer_key == "t"
    assert server.Settings.from_env({"KB_TEST_ISSUER_KEY": ""}).test_issuer_key is None  # empty = not set
    assert server.Settings.from_env({}).identity_keys == ""
    assert server.parse_keys(" , ", "K") == {}
    key = public_key(Ed25519PrivateKey.generate().public_key())
    assert list(server.parse_keys(f"a={key}, b={key}", "K")) == ["a", "b"]
    with pytest.raises(server.ConfigError, match=r"^K: \S"):
        server.parse_keys(f"a=b={key}", "K")  # the kid ends at the first "="


def test_no_verifier_refuses_with_a_reason() -> None:
    with pytest.raises(TokenError) as exc:
        server.NoVerifier().verify({}, tool=GET, arguments={})
    assert exc.value.code == "unauthenticated" and exc.value.reason == "no identity verifier configured"


async def test_nothing_readable_has_no_next_page(kb: Kb) -> None:
    r = await kb.ok(SEARCH, {"query": "contract", "limit": 1}, principal=ALPHA_NOBODY)
    assert r["items"] == [] and r["next_cursor"] is None


async def test_bundle_loaded_event_at_start(env: Env, caplog: pytest.LogCaptureFixture, backend: Backend) -> None:
    caplog.set_level(logging.INFO)
    server.build(env.settings, backend)
    events = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kb_connector"]
    assert events == [{"event": "bundle_loaded", "bundle_version": "0" * 40}]


async def test_mcp_endpoint_is_stateless_json_named_kb(kb: Kb) -> None:
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    r = await kb.http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init},
        headers={"accept": "application/json, text/event-stream"},
    )
    assert r.headers["content-type"].startswith("application/json")  # json_response
    assert "mcp-session-id" not in r.headers  # stateless_http: one request, one answer, no session
    assert r.json()["result"]["serverInfo"]["name"] == "kb"


# Cycle 3: the adapter on the audited KB contract (docs/audits/kb-gap-audit.md) --------------------------


async def test_adapter_sends_the_audited_arguments_with_the_read_key(backend: Backend, fake: FakeKb) -> None:
    await backend.search("t-alpha", ["sales"], "contract", offset=0, limit=2)
    await backend.search("t-beta", ["hr", "sales"], "contract", offset=40, limit=20)
    await backend.get_document("t-alpha", "101")
    window = {"limit": 50, "include_body": True}  # always the KB's whole window: it has no offset (G9)
    assert fake.requests == [
        ("kb_list_entity_types", WS_ALPHA, {}),
        ("kb_search", WS_ALPHA, {"query": "contract", "entity_types": ["sales"]} | window),
        ("kb_list_entity_types", WS_BETA, {}),  # cached per connection
        ("kb_search", WS_BETA, {"query": "contract", "entity_types": ["hr", "sales"]} | window),
        ("kb_get", WS_ALPHA, {"id": 101}),  # the id as the KB's integer
    ]


async def test_the_kb_window_cut_is_reported(kb: Kb) -> None:
    many(kb, 60)  # 62 published sales matches for "contract": more than the KB's 50-row window
    first = await kb.ok(SEARCH, {"query": "contract", "limit": 20})
    assert first["complete"] is True and first["next_cursor"] is not None
    deep = await kb.ok(SEARCH, {"query": "contract", "limit": 20, "cursor": first["next_cursor"]})
    deeper = await kb.ok(SEARCH, {"query": "contract", "limit": 20, "cursor": deep["next_cursor"]})
    assert deeper["complete"] is False and deeper["partial_reasons"] == ["truncated"]  # G9: the KB cut it
    assert deeper["next_cursor"] is None
    ctx = await kb.ok(CONTEXT, {"query": "contract", "max_documents": 2})
    assert ctx["complete"] is False  # more matches than max_documents


async def test_rows_of_another_workspace_drafts_and_archived_are_dropped(kb: Kb) -> None:
    # kb_get does not check the workspace (G6): t-alpha asking for t-beta's row by id gets not_found
    assert await kb.error(GET, {"document_id": BETA_SALES}) == NOT_FOUND
    assert kb.fake.calls["kb_get"] == 1
    for hidden in (DRAFT, ARCHIVED):  # Q3: readers get published, not archived
        assert await kb.error(GET, {"document_id": hidden}) == NOT_FOUND
    kb.fake.entities.append(entity(902, "Other ws contract", "contract", ws=WS_BETA))
    kb.fake.keys["alpha-read"] = Key(WS_BETA, "read")  # a KB that ignores the key's workspace in search
    r = await kb.ok(SEARCH, {"query": "contract", "limit": 50})
    assert r["items"] == []  # every row is of another workspace than the connection's


async def test_ids_the_kb_cannot_have_are_not_found_without_a_call(kb: Kb) -> None:
    for bad in ("0", "-1", "abc", "1e3", "01", "1" * 19, "d-101"):
        assert await kb.error(GET, {"document_id": bad}) == NOT_FOUND, bad
    assert kb.fake.calls.total() == 0


async def test_unknown_or_inactive_entity_type_is_not_found(kb: Kb) -> None:
    kb.fake.entities.append(entity(903, "Retired", "contract", type_id=4))  # type 4 is not active
    kb.fake.entities.append(entity(904, "Unknown", "contract", type_id=99))
    for i in ("903", "904"):
        assert await kb.error(GET, {"document_id": i}) == NOT_FOUND


async def test_contract_drift_is_internal_error_without_the_kb_text(kb: Kb, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    await kb.backend.list_collections("t-alpha")  # types cached: `answer` replaces the search result only
    for answer in ({"unexpected": "shape kb-secret-text"}, [{"id": "101"}], [{"id": True}], ["row"], "not a list"):
        kb.fake.answer = answer
        assert await kb.error(SEARCH, {"query": "contract"}) == INTERNAL, answer
    kb.fake.answer = None
    kb.backend._types.clear()
    kb.fake.entity_types.append({"id": "x", "key": "bad", "label": "Bad", "is_active": True})
    assert await kb.error(SEARCH, {"query": "contract"}) == INTERNAL
    assert "kb-secret-text" not in caplog.text


async def test_kb_tool_errors_and_non_json_are_internal_errors(kb: Kb, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    kb.fake.text = "not json"
    assert await kb.error(GET, {"document_id": SALES[0]}) == INTERNAL
    kb.fake.text, kb.fake.tool_error = None, True
    assert await kb.error(GET, {"document_id": SALES[0]}) == INTERNAL
    assert await kb.error(SEARCH, {"query": "contract"}) == INTERNAL
    assert "kb-secret-text" not in caplog.text  # the KB's text never leaves the adapter (SEC-18)


async def test_structured_answers_and_list_wrappers_are_accepted(kb: Kb) -> None:
    rows = [e for e in kb.fake.entities if e["id"] in (101, 102)]
    await kb.backend.list_collections("t-alpha")  # the entity types are cached: `answer` applies to the next calls
    for answer in ({"items": rows}, {"entities": rows}):
        kb.fake.answer = answer
        assert ids(await kb.ok(SEARCH, {"query": "contract"})) == ["101", "102"]
    kb.fake.answer = {"item": rows[0]}
    assert (await kb.ok(GET, {"document_id": "101"}))["id"] == "101"


async def test_readyz_needs_the_three_tools_and_no_write_tool(kb: Kb) -> None:
    assert (await kb.http.get("/readyz")).status_code == 200
    kb.backend._conns["t-alpha"] = Connection("kb-main", "http://fake-kb/mcp", WS_ALPHA, "alpha-write")
    assert (await kb.http.get("/readyz")).status_code == 503  # a write key lists kb_create (G4, Q4)
    kb.backend._conns["t-alpha"] = CONNECTIONS["t-alpha"]
    assert (await kb.http.get("/readyz")).status_code == 200
    kb.fake.mcp.disable(names={"kb_list_entity_types"})
    assert (await kb.http.get("/readyz")).status_code == 503  # a needed tool is missing (G15)


async def test_no_connections_is_not_ready(env: Env) -> None:
    app, _ = server.build(env.settings, Backend({}))
    async with AsyncExitStack() as stack:
        http, _ = await connect(stack, app)
        assert (await http.get("/readyz")).status_code == 503


async def test_calls_to_one_connection_are_sequential(backend: Backend, fake: FakeKb) -> None:
    fake.delay_s = 0.02
    await asyncio.gather(*(backend.get_document("t-alpha", "101") for _ in range(4)))
    assert fake.max_active == 1  # one request at a time per connection (G13)
    fake.max_active = 0
    await asyncio.gather(backend.get_document("t-alpha", "101"), backend.get_document("t-beta", "201"))
    assert fake.max_active == 2  # the lock is per connection, not global


async def test_transport_errors_are_temporarily_unavailable(env: Env) -> None:
    def refused(conn: Connection) -> httpx2.AsyncClient:
        def fail(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(fail))

    b = Backend(dict(CONNECTIONS), refused)
    with pytest.raises(backend_module.GxgError) as exc:
        await b.get_document("t-alpha", "101")
    assert exc.value.to_dict() == {
        "code": "temporarily_unavailable",
        "message": "KB is not available",
        "retryable": True,
    }
    assert await b.ready() is False


def test_connections_file(tmp_path: Path) -> None:
    (tmp_path / "alpha.key").write_text("alpha-read\n")
    (tmp_path / "c.json").write_text(
        json.dumps(
            {
                "t-alpha": {
                    "connection_id": "kb-main",
                    "url": "http://kb/mcp",
                    "workspace_id": WS_ALPHA,
                    "key_file": str(tmp_path / "alpha.key"),
                }
            }
        )
    )
    assert backend_module.load_connections(tmp_path / "c.json") == {
        "t-alpha": Connection("kb-main", "http://kb/mcp", WS_ALPHA, "alpha-read")
    }
    s = server.Settings.from_env({"KB_CONNECTIONS_FILE": str(tmp_path / "c.json")})
    assert s.connections_file == tmp_path / "c.json"
    assert server.Settings.from_env({}).connections_file is None


def test_default_http_client_sends_the_key_and_a_connect_timeout() -> None:
    c = backend_module.http_client(CONNECTIONS["t-alpha"])
    assert c.headers["authorization"] == "Bearer alpha-read" and c.timeout.connect == 5.0
    assert c.timeout.read is None and c.follow_redirects is False


async def test_the_fake_refuses_writes_with_a_read_key(fake: FakeKb) -> None:
    """The shape of AC-M6-04 on the fake (not on the KB): a read key does not see or run kb_create."""

    async def call(key: str) -> Any:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=fake), headers={"authorization": f"Bearer {key}"}
        ) as h:
            async with streamable_http_client("http://fake-kb/mcp", http_client=h) as (r, w), ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool(
                    "kb_create", {"title": "probe", "summary": "s", "body_md": "b", "entity_type_id": 1}
                )
                return names, res.is_error

    before = len(fake.entities)
    assert await call("alpha-read") == ({"kb_search", "kb_get", "kb_list_entity_types"}, True)
    assert len(fake.entities) == before
    assert (await call("alpha-write"))[1] is False and len(fake.entities) == before + 1


async def test_kb_key_refusal_is_its_own_internal_error(kb: Kb, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    for status in (401, 403, 404, 400):
        kb.fake.http_status = status
        await kb.error(GET, {"document_id": SALES[0]})
    errors = [a["error"] for a in audit_lines(caplog)]
    assert errors[:2] == ["KeyRefused", "KeyRefused"]  # an operator sees our key was refused
    assert "KeyRefused" not in errors[2:]


async def test_entity_types_are_cached_for_the_ttl(
    backend: Backend, fake: FakeKb, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: now[0])
    await backend.list_collections("t-alpha")
    now[0] += backend_module.TYPES_TTL_S - 1
    await backend.list_collections("t-alpha")
    assert fake.calls["kb_list_entity_types"] == 1
    now[0] += 1  # exactly the TTL: fetched again
    await backend.list_collections("t-alpha")
    assert fake.calls["kb_list_entity_types"] == 2


async def test_only_active_entity_types_are_collections(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": 1, "key": "sales", "is_active": True}, {"id": 2, "key": "hr", "is_active": False}]

    async def call(tenant: str, tool: str, args: dict[str, Any]) -> Any:
        assert (tool, args) == ("kb_list_entity_types", {})
        return rows

    monkeypatch.setattr(backend, "_call", call)
    assert await backend.list_collections("t-alpha") == ["sales"]
    backend._types.clear()
    rows.append({"id": 3, "key": "public"})  # no is_active: not the contract
    with pytest.raises(backend_module.ContractError):
        await backend.list_collections("t-alpha")


async def test_missing_fields_become_empty_and_summary_stands_in_for_the_body(kb: Kb) -> None:
    bare = {"id": 905, "workspace_id": WS_ALPHA, "entity_type_id": 1, "status": "published", "archived_at": None}
    kb.fake.entities += [bare, entity(906, "Only summary", "", summary="the summary")]
    got = await kb.ok(GET, {"document_id": "905"})
    assert (got["title"], got["content"], got["updated_at"]) == ("", "", "")
    assert (await kb.ok(GET, {"document_id": "906"}))["content"] == "the summary"


async def test_build_reads_the_connections_file(env: Env, tmp_path: Path) -> None:
    (tmp_path / "k").write_text("alpha-read")
    (tmp_path / "c.json").write_text(
        json.dumps(
            {
                "t-alpha": {
                    "connection_id": "kb-main",
                    "url": "http://kb/mcp",
                    "workspace_id": WS_ALPHA,
                    "key_file": str(tmp_path / "k"),
                }
            }
        )
    )
    settings = server.Settings(**{**vars(env.settings), "connections_file": tmp_path / "c.json"})
    _, connector = server.build(settings)
    assert connector.backend.connections == {"t-alpha": "kb-main"}
    assert connector.backend._conns["t-alpha"].key == "alpha-read"
    _, none = server.build(server.Settings(**{**vars(env.settings), "connections_file": None}))
    assert none.backend.connections == {}
