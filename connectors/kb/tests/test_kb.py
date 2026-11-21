"""KB wrapper connector (M6), in-process: collection grants (AC-M6-02 logic, SC-05, SC-10 on KB), identity,
schemas, readiness. The same checks through ContextForge: stand/tests/test_kb.py.

The MCP client is the official SDK over Streamable HTTP on httpx2's ASGITransport (no TCP socket). The bundle
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
from kb_connector import server
from kb_connector import tools as tools_module
from kb_connector.backend import Backend, Document
from kb_connector.tools import Connector, load_schema

pytestmark = pytest.mark.anyio

REPO = next(p for p in Path(__file__).resolve().parents if (p / "policies").is_dir())
KB = REPO / "connectors" / "kb"
ALPHA_READER, ALPHA_NOBODY, BETA_READER = "agent:t-alpha:reader", "agent:t-alpha:nobody", "agent:t-beta:reader"
SALES = ["d-1f6c0a", "d-3c9d27", "d-82b4e1"]  # t-alpha sales, sorted by id
HR, PUBLIC, BETA_SALES = "d-a07e55", "d-e4419b", "d-5b2f90"
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
    def __init__(self, connector: Connector, http: httpx2.AsyncClient, session: ClientSession, issuer: StubIssuer):
        self.connector, self.backend = connector, connector.backend
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
async def kb(env: Env) -> AsyncIterator[Kb]:
    app, connector = server.build(env.settings, Backend())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        yield Kb(connector, http, session, env.issuer)


def serving(docs: list[Document]) -> Any:
    """A KB search that ignores the collections and returns `docs` (paged like the real port)."""

    async def search(tenant: str, collections: list[str], query: str, *, offset: int, limit: int) -> Any:
        return docs[offset : offset + limit], len(docs) > offset + limit

    return search


def ids(result: dict[str, Any]) -> list[str]:
    return [i["id"] for i in result["items"]]


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "kb_connector.audit"]


NOT_FOUND = {"code": "not_found", "message": "not found", "retryable": False}


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


async def test_readyz_and_healthz(kb: Kb, env: Env) -> None:
    assert (await kb.http.get("/healthz")).json() == {"status": "ok"}
    assert (await kb.http.get("/readyz")).status_code == 200
    for f in env.settings.bundle_dir.iterdir():
        f.unlink()
    settings = server.Settings(**{**vars(env.settings), "state_file": env.settings.state_file.with_name("other")})
    app, connector = server.build(settings, Backend())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        assert (await http.get("/readyz")).status_code == 503  # GR-12: no bundle, not ready
        k = Kb(connector, http, session, env.issuer)
        assert (await k.error(SEARCH, {"query": "contract"}))["code"] == "temporarily_unavailable"
    assert connector.backend.calls.total() == 0


async def test_stale_bundle_is_not_ready(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = build_bundle(tmp_path / "old", monkeypatch, built_at="2026-01-01T00:00:00Z")  # GR-13: older than 24 h
    settings = server.Settings(
        **{**vars(env.settings), "bundle_dir": tmp_path / "old", "bundle_keys": keys, "state_file": tmp_path / "s"}
    )
    app, _ = server.build(settings, Backend())
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
    # "contract" matches sales, hr (expired grant) and public (no grant) documents of t-alpha, and t-beta's sales.
    r = await kb.ok(SEARCH, {"query": "contract"})
    assert sorted(ids(r)) == ["d-1f6c0a", "d-82b4e1"]
    assert r["source_refs"] == ["kb:kb-main/collection/sales"]
    assert kb.backend.calls["search"] == 1
    assert kb.backend.calls["collections"] == 0  # exact grants: the KB is not asked for its collections


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
    every = kb.backend.documents["t-alpha"]
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
    assert sorted(ids(r)) == ["d-1f6c0a", "d-82b4e1"]
    assert (await kb.ok(SEARCH, {"query": "contract", "collections": []}))["items"] == []


async def test_get_other_collection_or_tenant_is_not_found_like_a_missing_one(kb: Kb) -> None:
    assert (await kb.ok(GET, {"document_id": SALES[0]}))["collection"] == "sales"
    replies = [await kb.call(GET, {"document_id": d}) for d in (HR, PUBLIC, BETA_SALES, "d-000000")]
    assert all(r == replies[-1] for r in replies), replies  # SC-05: byte for byte as for a missing document
    assert replies[-1]["structuredContent"] == NOT_FOUND


async def test_get_collection_comes_from_the_kb(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    hr = kb.backend.documents["t-alpha"][3]
    assert hr.collection == "hr"
    r = await kb.call(GET, {"document_id": HR, "collection": "sales"})  # GR-01: the argument is ignored
    assert r["structuredContent"] == NOT_FOUND


async def test_expired_grant_gives_nothing(kb: Kb) -> None:
    assert await kb.error(GET, {"document_id": HR}) == NOT_FOUND  # grant on hr expired 2026-01-01 (GR-06)
    assert HR not in ids(await kb.ok(SEARCH, {"query": "salary"}))


async def test_tenants_with_the_same_collection_name_do_not_mix(kb: Kb) -> None:
    beta = await kb.ok(SEARCH, {"query": "contract"}, principal=BETA_READER)
    assert ids(beta) == [BETA_SALES]
    assert kb.backend.calls["collections"] == 1  # mask collection/*: the KB lists t-beta's collections
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
    assert sorted(ids(await kb.ok(SEARCH, args))) == ["d-1f6c0a", "d-82b4e1"]  # SC-03


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
    docs = {d.id: d for d in kb.backend.documents["t-alpha"]}
    expected = "\n\n".join(f"## {docs[i].title}\n{docs[i].content}" for i in ("d-1f6c0a", "d-82b4e1"))
    assert full["context"] == expected
    assert full["complete"] is True and full["partial_reasons"] == []
    assert [d["id"] for d in full["documents"]] == ["d-1f6c0a", "d-82b4e1"]
    assert full["source_refs"] == ["kb:kb-main/collection/sales"]

    cut = await kb.ok(CONTEXT, {"query": "contract", "max_chars": 100})
    assert cut["context"] == expected[:100] and len(cut["context"]) == 100
    assert cut["complete"] is False and cut["partial_reasons"] == ["truncated"]

    one = await kb.ok(CONTEXT, {"query": "contract", "max_documents": 1})
    assert [d["id"] for d in one["documents"]] == ["d-1f6c0a"] and one["complete"] is False

    exact = len(f"## {docs['d-1f6c0a'].title}\n{docs['d-1f6c0a'].content}")
    assert exact < 100
    edge = await kb.ok(CONTEXT, {"query": "contract", "max_chars": 100})
    assert [d["id"] for d in edge["documents"]] == ["d-1f6c0a", "d-82b4e1"]  # the second one is cut, not dropped


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
    kb.backend.failure = "rate_limited"
    err = await kb.error(SEARCH, {"query": "contract"})
    assert err == {"code": "rate_limited", "message": "KB rate limit", "retryable": True, "retry_after_s": 5}
    kb.backend.failure = "temporarily_unavailable"
    assert (await kb.error(GET, {"document_id": SALES[0]}))["code"] == "temporarily_unavailable"


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
        (SEARCH, "ok", 1),
        (GET, "not_found", 1),
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


async def test_production_without_identity_keys_refuses_every_call(env: Env) -> None:
    settings = server.Settings(**{**vars(env.settings), "profile": "production", "test_issuer_key": None})
    app, connector = server.build(settings, Backend())
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
    docs = [Document(f"d-z{i:03}", "sales", f"Extra {i}", content, "2026-10-09T00:00:00Z") for i in range(n)]
    kb.backend.documents["t-alpha"] += docs


async def test_default_limits(kb: Kb) -> None:
    many(kb, 25)
    page = await kb.ok(SEARCH, {"query": "contract"})
    assert len(page["items"]) == 20 and page["next_cursor"] is not None  # limit default 20
    assert page["complete"] is True and page["partial_reasons"] == []
    ctx = await kb.ok(CONTEXT, {"query": "contract"})
    assert len(ctx["documents"]) == 5  # max_documents default 5
    kb.backend.documents["t-alpha"] = [Document("d-big", "sales", "Big", "x" * 9000, "2026-10-09T00:00:00Z")]
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
        "id": "d-1f6c0a",
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
    kb.backend.failure = "temporarily_unavailable"
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


async def test_identity_is_bound_to_the_tool_and_arguments(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    real = server.StubVerifier.verify

    def verify(self: Any, headers: Any, **binding: Any) -> Any:
        seen.append(binding)
        return real(self, headers, **binding)

    monkeypatch.setattr(server.StubVerifier, "verify", verify)
    app, connector = server.build(env.settings, Backend())
    async with AsyncExitStack() as stack:
        http, session = await connect(stack, app)
        await Kb(connector, http, session, env.issuer).ok(GET, {"document_id": SALES[0]})
    assert seen == [{"tool": GET, "arguments": {"document_id": SALES[0]}}]


async def test_no_bundle_get_does_not_reach_the_kb(env: Env) -> None:
    for f in env.settings.bundle_dir.iterdir():
        f.unlink()
    settings = server.Settings(**{**vars(env.settings), "state_file": env.settings.state_file.with_name("other")})
    app, connector = server.build(settings, Backend())
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


async def test_backend_counts_calls_and_knows_only_its_tenants() -> None:
    b = Backend()
    assert await b.list_collections("t-gamma") == [] and await b.get_document("t-gamma", SALES[0]) is None
    assert await b.search("t-gamma", ["sales"], "contract", offset=0, limit=5) == ([], False)
    page, more = await b.search("t-alpha", ["sales", "hr"], "contract", offset=1, limit=1)
    assert [d.id for d in page] == ["d-1f6c0a"] and more is True  # (collection, id): hr d-a07e55, sales d-1f6c0a…
    assert await b.search("t-alpha", ["sales", "hr"], "contract", offset=2, limit=1) == (
        [d for d in b.documents["t-alpha"] if d.id == "d-82b4e1"],
        False,
    )
    assert b.calls == {"collections": 1, "get": 1, "search": 3}


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
            "kb_calls": 1,
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


async def test_a_slow_kb_times_out_by_the_manifest(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = yaml.safe_load((KB / "gxg-manifest.yaml").read_text())
    for t in manifest["tools"]:
        t["limits"]["timeout_s"] = 0.2
    (tmp_path / "gxg-manifest.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setattr(tools_module, "MANIFEST", tmp_path / "gxg-manifest.yaml")
    backend = Backend()
    backend.delay_s = 5  # the async port is cancelled by asyncio.wait_for, a hung KB does not block the process
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
    long = Document("d-long", "sales", "T" * 400, "contract " + "x" * 30000, "2026-10-01T00:00:00Z")
    kb.backend.documents["t-alpha"].append(long)
    got = await kb.ok(GET, {"document_id": "d-long"})
    assert len(got["title"]) == 300 and len(got["content"]) == 20000 and got["truncated"] is True
    jsonschema.validate(got, load_schema("schemas/document-get.output.json"))
    found = await kb.ok(SEARCH, {"query": "contract", "limit": 50})
    item = next(i for i in found["items"] if i["id"] == "d-long")
    assert len(item["title"]) == 300 and len(item["excerpt"]) == 500
    jsonschema.validate(found, load_schema("../../schemas/search-result.schema.json"))
    ctx = await kb.ok(CONTEXT, {"query": "xxx", "max_chars": 8000})
    assert ctx["documents"] == [{"id": "d-long", "title": "T" * 300, "source_ref": "kb:kb-main/collection/sales"}]
    assert ctx["context"].startswith("## " + "T" * 300 + "\n") and len(ctx["context"]) == 8000
    jsonschema.validate(ctx, load_schema("schemas/context-get.output.json"))
    exact = Document("d-exact", "sales", "E", "y" * 20000, "2026-10-01T00:00:00Z")
    kb.backend.documents["t-alpha"].append(exact)
    assert (await kb.ok(GET, {"document_id": "d-exact"}))["truncated"] is False


async def test_readyz_needs_the_kb(kb: Kb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(kb.backend, "ready", lambda: False)
    r = await kb.http.get("/readyz")
    assert (r.status_code, r.json()) == (503, {"status": "not_ready"})
    events = [json.loads(m.getMessage()) for m in caplog.records if m.name == "kb_connector"]
    assert events[-1] == {"event": "not_ready", "reasons": ["kb_unreachable"]}
    monkeypatch.setattr(kb.backend, "ready", lambda: True)
    r = await kb.http.get("/readyz")
    assert (r.status_code, r.json()) == (200, {"status": "ready"})
    assert Backend().ready() is True


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


async def test_contextforge_identity_keys_select_the_kit_verifier(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    keys = f"cf-0={public_key(other.public_key())}, cf-1={public_key(key.public_key())}"
    settings = server.Settings(
        **{**vars(env.settings), "profile": "production", "test_issuer_key": None, "identity_keys": keys}
    )
    app, connector = server.build(settings, Backend())
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


def test_backend_is_fast_by_default() -> None:
    b = Backend()
    assert b.delay_s == 0

    async def three() -> None:
        await b.list_collections("t-alpha")
        await b.get_document("t-alpha", SALES[0])
        await b.search("t-alpha", ["sales"], "x", offset=0, limit=1)

    started = time.monotonic()
    asyncio.run(three())
    assert time.monotonic() - started < 0.5


async def test_nothing_readable_has_no_next_page(kb: Kb) -> None:
    r = await kb.ok(SEARCH, {"query": "contract", "limit": 1}, principal=ALPHA_NOBODY)
    assert r["items"] == [] and r["next_cursor"] is None


async def test_bundle_loaded_event_at_start(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    server.build(env.settings, Backend())
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
