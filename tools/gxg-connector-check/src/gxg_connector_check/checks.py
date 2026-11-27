"""The checks of `check-connectors`, one Conformance Matrix ID each (docs/specs/conformance-matrix-v1.md).

In every refused case the provider is not called (the matrix: «ноль обращений к провайдеру»): each check counts.
Not here, because they need the stand with ContextForge (stand/, M5): SC-01, SC-06, SC-07, MC-01 through
ContextForge, MC-04, MC-05. SC-08 (instructions in data give no rights) follows from SC-02…SC-05: identity and
grants never come from data.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import jsonschema
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import effects, exec_token, mcp
from gxg_connector_kit.errors import ERROR_CODES, GxgError
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.identity import StubIssuer

from . import bundle
from .harness import READ_VERBS, Call, Running, item_key, read_verb_ok, registry_read
from .plugin import Client, Env

pytestmark = pytest.mark.anyio
ENVELOPE_KEYS = {"code", "message", "retryable", "retry_after_s", "request_id", "details"}
SPOOF_HEADERS = {"x-tenant-id": "{tenant}", "x-forwarded-user-teams": "{tenant}", "x-gxg-identity": "forged"}


def specs(env: Env) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in env.manifest["tools"]}


def envelope(result: dict[str, Any]) -> dict[str, Any]:
    """MCC-05: a tool error with a valid `{code, message, retryable, …}` structuredContent and a text duplicate."""
    assert result.get("isError") is True, f"not a tool error: {result}"
    sc = result.get("structuredContent")
    assert isinstance(sc, dict) and set(sc) <= ENVELOPE_KEYS and sc.get("code") in ERROR_CODES, f"no envelope: {result}"
    rebuilt = GxgError(
        sc["code"], sc["message"], retryable=sc["retryable"], retry_after_s=sc.get("retry_after_s"),
        request_id=sc.get("request_id"), details=sc.get("details"),
    ).to_dict()  # fmt: skip
    assert rebuilt == sc, f"envelope is not error.schema.json: {sc}"
    assert result.get("content"), "no text content for older clients (MCC-04)"
    return sc


def code(result: dict[str, Any]) -> str:
    c: str = envelope(result)["code"]
    return c


def ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is not True, f"expected a result: {result}"
    sc: dict[str, Any] = result["structuredContent"]
    return sc


def wrong_type(env: Env, call: Call) -> dict[str, Any]:
    """The call's arguments with one property of a non-object type set to an object: never schema-valid."""
    schema = mcp.load_schema(env.harness.root, specs(env)[call.tool]["input_schema"])
    name = next(k for k, v in schema.get("properties", {}).items() if v.get("type") not in (None, "object"))
    return {**call.args, name: {"gxg_check": True}}


def single(env: Env) -> list[Call]:
    return [c for c in readers(env) if specs(env)[c.tool]["resource"].get("scope") != "list"]


def readers(env: Env) -> list[Call]:
    """The calls a reader makes: every grants check (SC-05, SC-10) runs on these."""
    return [c for c in env.harness.calls if c.caller == "reader"]


def listing(env: Env) -> list[Call]:
    return [c for c in readers(env) if specs(env)[c.tool]["resource"].get("scope") == "list"]


def input_props(env: Env, tool: str) -> dict[str, Any]:
    props: dict[str, Any] = mcp.load_schema(env.harness.root, specs(env)[tool]["input_schema"]).get("properties", {})
    return props


REQUIRED_FAILURES = {"rate_limited", "temporarily_unavailable", "crash"}


async def started(env: Env, **overrides: Any) -> tuple[Running, Any]:
    running = env.start(**overrides)
    return running, env.client(running)


# --- MC: contract -----------------------------------------------------------------------------------------------


@pytest.mark.check("MC-01")
async def test_manifest_names_schemas_and_discovery(env: Env) -> None:
    """MCC-01: a valid manifest; published tools are exactly its public tools, with its schema files; verbs match
    the effect class."""
    root = env.harness.root
    tools = mcp.load_manifest(root / "gxg-manifest.yaml", root)
    for t in tools:
        verb = t["name"].rsplit(".", 1)[1]
        if t["effect_class"] == "read":
            assert read_verb_ok(t["name"]), (
                f"{t['name']}: a read tool's verb is one of {sorted(READ_VERBS)}, or <domain>.ext.describe/call"
            )
        if t["effect_class"] in ("external_effect", "high_risk"):
            assert verb == "propose" or verb.startswith("propose_"), f"{t['name']}: effects are published as propose"
    assert {c.tool for c in env.harness.calls} >= {t["name"] for t in tools if t["effect_class"] == "read"}
    _, client = await started(env)
    async with client as c:
        listed = {t.name: t for t in (await c.session.list_tools()).tools}
    assert sorted(listed) == sorted(t["name"] for t in tools)
    for t in tools:
        assert listed[t["name"]].input_schema == mcp.load_schema(root, t["input_schema"]), t["name"]
        assert listed[t["name"]].output_schema == mcp.load_schema(root, t["output_schema"]), t["name"]
        assert listed[t["name"]].description, t["name"]


@pytest.mark.check("MC-02")
async def test_results_are_typed_and_bounded(env: Env) -> None:
    """MCC-04: results match the output schema and the size limit; a list result has `limits.max_results` and an
    input `limit` bounded by it; `limit` above the bound is `invalid_input`."""
    root, by_name = env.harness.root, specs(env)
    _, client = await started(env)
    async with client as c:
        for call in env.harness.calls:
            spec = by_name[call.tool]
            out_schema = mcp.load_schema(root, spec["output_schema"])
            got = ok(await c.call(call.tool, call.args))
            jsonschema.Draft202012Validator(out_schema).validate(got)
            assert len(json.dumps(got).encode()) <= spec["limits"].get("max_bytes", mcp.MAX_RESULT_BYTES)
            lists = [k for k, v in out_schema.get("properties", {}).items() if v.get("type") == "array"]
            if spec["resource"].get("scope") == "list" and "items" in lists:
                limit = mcp.load_schema(root, spec["input_schema"]).get("properties", {}).get("limit")
                bound = spec["limits"].get("max_results")
                assert isinstance(bound, int) and limit and limit.get("maximum", bound + 1) <= bound, call.tool
                assert len(got["items"]) <= bound
                assert code(await c.call(call.tool, {**call.args, "limit": bound + 1})) == "invalid_input"
                assert call.empty is not None, f"{call.tool}: set Call.empty (a call that finds nothing)"
                nothing = ok(await c.call(call.tool, call.empty))  # an empty result is a success, not an error
                assert nothing["items"] == [] and nothing.get("complete", True) is True, nothing
                if "next_cursor" in out_schema.get("properties", {}):
                    await pages(env, c, call, got, item_key(call.tool, out_schema["properties"]["items"]))


async def pages(env: Env, c: Client, call: Call, full: dict[str, Any], key: str) -> None:
    """MCC-04: `limit=1` gives a cursor; the next page is the next item; the cursor is bound to its caller."""
    assert len(full["items"]) >= 2, f"{call.tool}: Call.args must find at least 2 items"
    first = ok(await c.call(call.tool, {**call.args, "limit": 1}))
    assert len(first["items"]) == 1 and first["next_cursor"], f"{call.tool}: a cut page has a next_cursor"
    second = ok(await c.call(call.tool, {**call.args, "limit": 1, "cursor": first["next_cursor"]}))
    assert [i[key] for i in first["items"] + second["items"]] == [i[key] for i in full["items"][:2]], call.tool
    theirs = await c.call(
        call.tool, {**call.args, "limit": 1, "cursor": first["next_cursor"]}, principal=env.harness.stranger
    )
    assert code(theirs) == "invalid_input", f"{call.tool}: another principal's cursor"


@pytest.mark.check("MC-03")
async def test_provider_failures_have_their_codes(env: Env) -> None:
    """MCC-05: 429 -> `rate_limited` with `retry_after_s`; unavailable/timeout before start -> retryable."""
    missing = REQUIRED_FAILURES - set(env.harness.failures)
    assert not missing, f"Harness.failures must include {sorted(missing)}: the provider's failure modes"
    running, client = await started(env)
    call = env.harness.calls[0]
    async with client as c:
        for mode in env.harness.failures:
            if mode == "crash":
                continue
            running.fail(mode)
            sc = envelope(await c.call(call.tool, call.args))
            assert sc["code"] == mode and sc["retryable"] is True, sc
            if mode == "rate_limited":
                assert isinstance(sc.get("retry_after_s"), int) and sc["retry_after_s"] >= 1, sc
            running.fail(None)
        ok(await c.call(call.tool, call.args))


@pytest.mark.check("MCC-05")
async def test_every_refusal_is_the_error_envelope(env: Env) -> None:
    """Unexpected exception -> `internal_error` without its text; refusals are tool errors with the envelope."""
    running, client = await started(env)
    call = next(c for c in env.harness.calls if mcp.load_schema(env.harness.root, specs(env)[c.tool]["input_schema"])
                .get("properties"))  # fmt: skip
    async with client as c:
        assert code(await c.call(call.tool, call.args, identity={})) == "unauthenticated"
        assert code(await c.call(call.tool, wrong_type(env, call))) == "invalid_input"
        running.fail("crash")
        r = await c.call(call.tool, call.args)
        assert envelope(r) == {"code": "internal_error", "message": "internal error", "retryable": False}
        assert env.secret not in json.dumps(r)


@pytest.mark.check("MCC-09")
async def test_health_and_readiness(env: Env, tmp_path: Path) -> None:
    """`/healthz` 200; `/readyz` 200 with a valid bundle, 503 without one and without its reasons; without a bundle
    a call is `temporarily_unavailable` and does not reach the provider (GR-12)."""
    for bundle_dir, status, body in (
        (None, 200, {"status": "ready"}),
        (tmp_path / "none", 503, {"status": "not_ready"}),
    ):
        running, client = await started(env, **({"bundle_dir": bundle_dir} if bundle_dir else {}))
        async with client as c:
            health = await c.http.get("/healthz")
            assert (health.status_code, health.json()) == (200, {"status": "ok"})
            ready = await c.http.get("/readyz")
            assert (ready.status_code, ready.json()) == (status, body)
            if status == 503:
                call = env.harness.calls[0]
                assert code(await c.call(call.tool, call.args)) == "temporarily_unavailable"
                assert running.calls() == 0
            else:  # MCC-09: the backend is part of readiness
                running.fail("temporarily_unavailable")
                down = await c.http.get("/readyz")
                assert (down.status_code, down.json()) == (503, {"status": "not_ready"}), "ready with the backend down"
                running.fail(None)
                assert (await c.http.get("/readyz")).status_code == 200


# --- SC: identity, tenant, resources ----------------------------------------------------------------------------


@pytest.mark.check("SC-02")
async def test_no_or_bad_identity_is_unauthenticated(env: Env) -> None:
    """Anonymous, expired, another audience -> `unauthenticated` on every tool (SEC-01, SEC-02)."""
    _, client = await started(env)
    reader = env.harness.reader
    old = time.time() - 3600
    bad = {
        "anonymous": {},
        "expired": env.identity(reader, now=lambda: old),
        "audience": env.issuer.headers(tenant_id=reader.split(":")[1], principal=reader, audience="other-connector"),
    }
    async with client as c:
        for call in env.harness.calls:
            for why, identity in bad.items():
                assert code(await c.call(call.tool, call.args, identity=identity)) == "unauthenticated", why


@pytest.mark.check("SC-03")
async def test_identity_is_not_taken_from_arguments_headers_or_meta(env: Env) -> None:
    """A caller of another tenant claims the reader's tenant in arguments, headers and `_meta`: refused
    (`invalid_input`) or ignored — the call acts as the caller, never as the claimed tenant (MCC-02). A registry read
    (`<domain>.ext.search`/`.describe`, MCC-11) may answer both tenants alike: the same registry."""
    h = env.harness
    tenant = h.reader.split(":")[1]
    _, client = await started(env)
    async with client as c:
        for call in h.calls:
            mine = await c.call(call.tool, call.args)
            plain = await c.call(call.tool, call.args, principal=h.stranger)
            spoof = {k: v.format(tenant=tenant) for k, v in SPOOF_HEADERS.items()}
            meta = {"tenant_id": tenant, "principal": h.reader, "user": h.reader}
            tries = {
                **{f"argument {k}": ({**call.args, k: v}, None, None)
                   for k, v in (("tenant_id", tenant), ("agent_id", h.reader), ("principal", h.reader))},
                "headers and _meta": (call.args, spoof, meta),
            }  # fmt: skip
            for why, (args, headers, m) in tries.items():
                r = await c.call(call.tool, args, principal=h.stranger, headers=headers, meta=m)
                refused = r.get("isError") is True and r["structuredContent"].get("code") == "invalid_input"
                assert refused or r == plain, f"{call.tool}: {why} changed whose call it is"
            if registry_read(call.tool):  # registry entries, the same for tenants with the same registry (MCC-11)
                continue
            assert plain != mine, f"{call.tool}: another tenant's caller gets the reader's result"
            if call in single(env):
                assert plain == await c.call(call.tool, call.missing or {}, principal=h.stranger), call.tool
            elif plain.get("isError") is not True:
                assert not h.rids(call.tool, ok(plain)) & h.rids(call.tool, ok(mine)), call.tool


@pytest.mark.check("SC-04")
async def test_forged_or_replayed_identity_never_reaches_the_provider(env: Env) -> None:
    """Another issuer's signature, a replayed assertion: `unauthenticated`; no provider call for any refused
    identity, also not to look up the named resource first (SEC-03)."""
    running, client = await started(env)
    h = env.harness
    stranger_issuer = StubIssuer()
    async with client as c:
        for call in h.calls:
            for args in filter(None, (call.args, call.missing, call.hidden)):
                forged = stranger_issuer.headers(
                    tenant_id=h.reader.split(":")[1], principal=h.reader, audience=env.connector_id
                )
                assert code(await c.call(call.tool, args, identity=forged)) == "unauthenticated"
                assert code(await c.call(call.tool, args, identity={})) == "unauthenticated"
        assert running.calls() == 0, "the provider was called before the identity was verified"
        call = h.calls[0]
        replayed = env.identity(env.caller(call.tool))
        ok(await c.call(call.tool, call.args, identity=replayed))
        assert code(await c.call(call.tool, call.args, identity=replayed)) == "unauthenticated"


@pytest.mark.check("SC-05")
async def test_hidden_and_foreign_resources_are_not_found_like_missing_ones(env: Env) -> None:
    """A resource without a grant, of another tenant, or that does not exist: the same `not_found`, byte for byte
    (GR-17, TEN-02)."""
    h = env.harness
    named = single(env) + [c for c in listing(env) if any(k.endswith("_ids") for k in input_props(env, c.tool))]
    assert named, "the harness names no resource (Call.missing): SC-05 cannot be checked"
    _, client = await started(env)
    async with client as c:
        for call in named:
            assert call.missing is not None and call.hidden is not None, f"{call.tool}: set missing and hidden"
            missing = await c.call(call.tool, call.missing)
            assert code(missing) == "not_found", f"{call.tool}: a missing named resource is not_found"
            assert await c.call(call.tool, call.hidden) == missing, f"{call.tool}: a hidden resource differs"
            if call in single(env):
                foreign = await c.call(call.tool, call.args, principal=h.stranger)
                assert foreign == missing, f"{call.tool}: foreign differs"


@pytest.mark.check("SC-09")
async def test_no_secret_token_or_payload_in_responses_and_logs(
    env: Env, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    """The provider credential, identity tokens and the caller's free text (a query, a message) appear in no
    response and no log line, on the success path and on every failure path (SEC-17, SEC-18)."""
    caplog.set_level(logging.INFO)  # the production level (gxg_connector_kit.mcp.setup_logging)
    running, client = await started(env)
    h = env.harness
    seen: list[Any] = []
    tokens: list[str] = []
    marker = f"gxg-marker-{uuid.uuid4().hex}"
    texts = [  # every free-text argument of every call, set to the marker
        (call, {**call.args, prop: marker})
        for call in [*h.calls, *([h.propose] if h.propose is not None else [])]
        for prop, schema in input_props(env, call.tool).items()
        if schema.get("type") == "string" and not {"format", "pattern", "enum", "const"} & set(schema)
    ]
    async with client as c:
        for mode in (None, *h.failures):
            running.fail(mode)
            for call in h.calls:
                for who in (h.reader, h.nobody, h.stranger):
                    identity = env.identity(who)
                    tokens += identity.values()
                    seen.append(await c.call(call.tool, call.args, identity=identity))
            for call, args in texts:
                await c.call(call.tool, args, principal=h.actor if call is h.propose and h.actor else h.reader)
        running.fail(None)
        env.approval.down = True  # the failure path of propose: the Approval Service is unreachable
        for call, args in texts:
            if call is h.propose:
                await c.call(call.tool, args, principal=h.actor)
        env.approval.down = False
    out, err = capfd.readouterr()
    blob = json.dumps(seen) + caplog.text + out + err
    assert env.secret not in blob, "the provider credential leaked"
    assert not [t for t in tokens if t in blob], "an identity token is in a response or a log"
    assert marker not in caplog.text + out + err, "a caller's text (query, message) is in a log line (SEC-18)"


@pytest.mark.check("SC-10")
async def test_grants_filter_and_no_provider_call_without_grants(env: Env) -> None:
    """A principal without grants gets empty results or `not_found` and the provider is not called; a reader sees
    only granted resources (GR-02, GR-06, GR-16); another tenant sees none of this tenant's. The registry reads
    `<domain>.ext.search`/`.describe` (MCC-11) return entries, not resources: only the first part applies to them."""
    h = env.harness
    store = BundleStore(
        mcp.parse_keys(bundle.keys_setting(env.bundle_signer), "keys"), state_file=env.tmp / "sc10.json"
    )
    store.load(*(Path(env.tmp / "bundle" / f).read_bytes() for f in (mcp.BUNDLE_FILE, mcp.SIG_FILE)))
    tenant = h.reader.split(":")[1]
    running, client = await started(env)
    async with client as c:
        for call in h.calls:
            r = await c.call(call.tool, call.args, principal=h.nobody)
            refusals = {"not_found", "permission_denied"} if call.caller == "operator" else {"not_found"}
            assert (code(r) in refusals) if r.get("isError") else not h.rids(call.tool, ok(r)), call.tool
        assert running.calls() == 0, "the provider was called for a principal without any grant"
        for call in readers(env):
            if registry_read(call.tool):  # registry entries: no provider call, no resource (MCC-11)
                ok(await c.call(call.tool, call.args))
                continue
            before = running.calls()
            rids = h.rids(call.tool, ok(await c.call(call.tool, call.args)))
            assert running.calls() > before, f"{call.tool}: Running.calls() does not count provider calls"
            assert rids, f"{call.tool}: Call.args must return at least one resource"
            assert all(store.check(tenant, h.reader, "read", rid).allowed for rid in rids), call.tool
            r = await c.call(call.tool, call.args, principal=h.stranger)
            assert r.get("isError") or not (h.rids(call.tool, ok(r)) & rids), call.tool


@pytest.mark.check("SC-10")
async def test_operator_only_tools_answer_nobody_else(env: Env) -> None:
    """A tool for the operator (`Call.caller == "operator"`): the operator gets data; the reader and a principal
    without grants of the same tenant get `permission_denied` or `not_found`, no data, and the provider is not
    called. (Another tenant's principal may be that tenant's operator; tenants are kept apart by SC-03 and SC-10.)"""
    h = env.harness
    calls = [c for c in h.calls if c.caller == "operator"]
    running, client = await started(env)
    async with client as c:
        for call in calls:
            before = running.calls()
            assert h.rids(call.tool, ok(await c.call(call.tool, call.args, principal=h.operator))), call.tool
            assert running.calls() > before, f"{call.tool}: Running.calls() does not count provider calls"
            for who in (h.reader, h.nobody):
                before = running.calls()
                r = await c.call(call.tool, call.args, principal=who)
                assert r.get("isError") is True, f"{call.tool} answered {who}: an operator-only tool"
                assert code(r) in ("permission_denied", "not_found"), r
                assert running.calls() == before, f"{call.tool}: the provider was called for {who}"


# --- EX: effects ------------------------------------------------------------------------------------------------


async def proposed(env: Env, c: Client) -> dict[str, Any]:
    """`propose` through MCP; returns the intent body the connector sent to the Approval Service."""
    call = env.harness.propose
    assert call is not None and env.harness.actor is not None, "an effects connector sets Harness.propose and actor"
    got = ok(await c.call(call.tool, call.args, principal=env.harness.actor))
    assert got["status"] == "pending_approval"
    return env.approval.intents[got["intent_id"]]


def token(env: Env, intent_id: str, body: dict[str, Any], **change: Any) -> tuple[str, str]:
    """(execution token, execution_id) for the intent as created; `change` overrides claims; `key`, `now`."""
    signer: Ed25519PrivateKey = change.pop("key", env.executor)
    now = change.pop("now", time.time)
    execution = str(uuid.uuid4())
    claims = {
        "aud": env.connector_id, "sub": intent_id, "jti": execution, "ten": body["requester"]["tenant_id"],
        "req": body["requester"]["principal"], "tool": f"{body['tool_key']}@{body['tool_version']}",
        "con": body["connection_id"], "res": body["resource_id"], "ahs": body["canonical_args_hash"],
        "idk": body["idempotency_key"], **change,
    }  # fmt: skip
    return exec_token.sign(signer, claims, now=now), execution


def retarget(env: Env, intent_id: str, body: dict[str, Any], **change: str) -> tuple[str, str]:
    """A token whose `ahs` is recomputed for the changed target, so only the target check can refuse it."""
    target = {"ten": body["requester"]["tenant_id"], "con": body["connection_id"], "res": body["resource_id"]} | change
    ahs = effects.args_hash(body["tool_key"], body["tool_version"], target["ten"], target["con"], target["res"],
                            body["canonical_args"])  # fmt: skip
    return token(env, intent_id, body, ahs=ahs, **change)


async def execute(env: Env, running: Running, intent_id: str, body: dict[str, Any], **kw: Any) -> httpx2.Response:
    """POST /internal/v1/execute/<executes_via> as the executor; kw: token, execution, args, svc (Authorization)."""
    tok, execution = kw.get("token") or token(env, intent_id, body)
    via = specs(env)[body["tool_key"]]["executes_via"]
    payload = {"intent_id": intent_id, "execution_id": execution, "execution_token": tok,
               "args": kw.get("args", body["canonical_args"])}  # fmt: skip
    if kw.get("drop_token"):
        del payload["execution_token"]
    auth = kw.get("svc", f"Bearer {env.executor_jwt()}")
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=running.internal), base_url="http://int") as h:
        headers = {"authorization": auth} if auth else {}
        return await h.post(f"/internal/v1/execute/{via}", json=payload, headers=headers)


def intent_of(env: Env) -> tuple[str, dict[str, Any]]:
    [(intent_id, body)] = env.approval.intents.items()
    return intent_id, body


@pytest.mark.effects
@pytest.mark.check("EX-01")
async def test_execute_is_not_an_mcp_tool(env: Env) -> None:
    """`*.execute`/`*.reconcile` are never listed and cannot be called through MCP (EFF-02, MCC-07)."""
    running, client = await started(env)
    internal = {t["executes_via"] for t in env.manifest["tools"] if "executes_via" in t}
    async with client as c:
        listed = {t.name for t in (await c.session.list_tools()).tools}
        assert not listed & internal and not any(n.endswith((".execute", ".reconcile")) for n in listed)
        for name in internal:
            r = await c.call(name, {}, principal=env.harness.actor)
            assert r.get("isError") is True, f"{name} answered through MCP"
    assert running.calls() == 0 and not env.approval.dispatched


@pytest.mark.effects
@pytest.mark.check("EX-02")
async def test_approved_in_arguments_gives_no_authority(env: Env) -> None:
    """`approved: true` or a foreign `intent_id` in `propose` arguments or `_meta`: refused (`invalid_input`) or
    ignored — the intent is an ordinary `pending_approval` of the verified caller (MCC-02, EFF-09)."""
    call, actor = env.harness.propose, env.harness.actor
    assert call is not None and actor is not None
    running, client = await started(env)
    async with client as c:
        for extra in ({"approved": True}, {"intent_id": str(uuid.uuid4())}, {"approval_id": "x"}):
            r = await c.call(call.tool, {**call.args, **extra}, principal=actor)
            assert r.get("isError") is not True or code(r) == "invalid_input", r
        ok(await c.call(call.tool, call.args, principal=actor, meta={"approved": True, "user": "someone"}))
    assert env.approval.intents
    for body in env.approval.intents.values():
        assert body["requester"] == {"principal": actor, "tenant_id": actor.split(":")[1]}
        assert "approved" not in json.dumps(body) and "approval_id" not in json.dumps(body)
    assert running.calls() == 0 and not env.approval.dispatched


@pytest.mark.effects
@pytest.mark.check("EX-03")
async def test_execute_without_a_valid_token_is_refused_before_the_provider(env: Env) -> None:
    """No service JWT, not an executor, no execution token, another `aud`, unknown `kid`, expired (EFF-18, SEC-10)."""
    running, client = await started(env)
    async with client as c:
        await proposed(env, c)
    intent_id, body = intent_of(env)
    other = Ed25519PrivateKey.generate()
    unserved = body["resource_id"].rsplit("/", 1)[0] + "/gxg-check-unserved"
    # the actor may propose on it: only the connector's target check (EFF-18 п.3) can refuse the token
    grant = {"id": "gxg-check-unserved", "subjects": [body["requester"]["principal"]], "actions": ["read", "propose"],
             "resources": [unserved]}  # fmt: skip
    granted = [{**d, "grants": [*d["grants"], grant]} if d["tenant"] == body["requester"]["tenant_id"] else d
               for d in env.grants]  # fmt: skip
    env.write_bundle(granted, datetime.now(UTC))
    cases: dict[str, tuple[dict[str, Any], int]] = {
        "no service JWT": ({"svc": ""}, 401),
        "service JWT of an unknown key": ({"svc": f"Bearer {env.executor_jwt(signer=other)}"}, 401),
        "service JWT of a non-executor": (
            {"svc": f"Bearer {env.executor_jwt(signer=env.svc, iss=env.connector_id)}"},
            401,
        ),
        "no execution token": ({"drop_token": True}, 422),
        "another aud": ({"token": token(env, intent_id, body, aud="other-connector")}, 401),
        "unknown kid": ({"token": token(env, intent_id, body, key=other)}, 401),
        "expired": ({"token": token(env, intent_id, body, now=lambda: time.time() - 600)}, 401),
        # EFF-18 п.3: a valid token for a target this connector does not serve (the hash agrees with it)
        "a resource it does not serve": ({"token": retarget(env, intent_id, body, res=unserved)}, 403),
        "a tenant it does not serve": (
            {"token": retarget(env, intent_id, body, ten="t-gxg-unknown", req="agent:t-gxg-unknown:x")},
            403,
        ),
    }
    for why, (kw, status) in cases.items():
        r = await execute(env, running, intent_id, body, **kw)
        assert r.status_code == status, f"{why}: {r.status_code} {r.text}"
    assert running.calls() == 0 and not env.approval.dispatched


@pytest.mark.effects
@pytest.mark.check("EX-04")
async def test_one_token_dispatches_once(env: Env) -> None:
    """The same token twice: one `dispatch`, one provider call (EFF-18, EFF-20)."""
    running, client = await started(env)
    async with client as c:
        await proposed(env, c)
    intent_id, body = intent_of(env)
    tok = token(env, intent_id, body)
    first = await execute(env, running, intent_id, body, token=tok)
    assert first.status_code == 200 and first.json()["status"] == "succeeded", first.text
    again = await execute(env, running, intent_id, body, token=tok)
    assert again.status_code in (401, 409), again.text
    assert env.approval.dispatched == [tok[1]] and running.calls() == 1
    # the connector's own jti cache refuses the replay: no second dispatch request (EFF-18 п.1)
    assert env.approval.dispatch_requests == [tok[1]], "the replay reached dispatch"


@pytest.mark.effects
@pytest.mark.check("EX-05")
async def test_args_other_than_approved_are_refused(env: Env) -> None:
    """Args that differ from the approved hash by one character: refused before `dispatch` (EFF-07, EFF-18)."""
    running, client = await started(env)
    async with client as c:
        await proposed(env, c)
    intent_id, body = intent_of(env)
    for key, value in body["canonical_args"].items():
        if isinstance(value, str):
            args = {**body["canonical_args"], key: value + "x"}
            r = await execute(env, running, intent_id, body, args=args)
            assert r.status_code == 422, f"{key}: {r.status_code} {r.text}"
    assert running.calls() == 0 and not env.approval.dispatched


@pytest.mark.effects
@pytest.mark.check("EX-11")
async def test_lost_answer_is_outcome_unknown_and_reconciled(env: Env) -> None:
    """The provider executed and the answer was lost: `outcome_unknown`, no second call; reconcile finds it."""
    running, client = await started(env)
    async with client as c:
        await proposed(env, c)
    intent_id, body = intent_of(env)
    running.fail("commit_then_drop")
    r = await execute(env, running, intent_id, body)
    result = r.json()
    assert r.status_code == 200 and result["status"] == "outcome_unknown" and result["reconciliation_required"]
    assert running.calls() == 1
    via = specs(env)[body["tool_key"]]["executes_via"]
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=running.internal), base_url="http://int") as h:
        rec = await h.post(
            f"/internal/v1/reconcile/{via}",
            json={"intent_id": intent_id, "execution_id": env.approval.dispatched[0],
                  "idempotency_key": body["idempotency_key"]},
            headers={"authorization": f"Bearer {env.executor_jwt()}"},
        )  # fmt: skip
    assert rec.status_code == 200 and rec.json()["finding"] == "executed", rec.text
    assert running.calls() == 1


@pytest.mark.effects
@pytest.mark.check("EX-14")
async def test_revoked_propose_right_refuses_execute(env: Env) -> None:
    """The requester lost `propose` between approve and execute: refused, no provider call (EFF-18)."""
    running, client = await started(env)
    async with client as c:
        await proposed(env, c)
    intent_id, body = intent_of(env)
    actor = env.harness.actor
    revoked = [
        {**d, "grants": [{**g, "subjects": [s for s in g["subjects"] if s != actor]} for g in d["grants"]
                         if [s for s in g["subjects"] if s != actor]]}
        for d in env.grants
    ]  # fmt: skip
    env.write_bundle(revoked, datetime.now(UTC))
    r = await execute(env, running, intent_id, body)
    assert r.status_code == 403, r.text
    assert running.calls() == 0 and not env.approval.dispatched


@pytest.mark.effects
@pytest.mark.check("EX-16")
async def test_approval_service_down(env: Env) -> None:
    """Approval Service unavailable: `propose` is a retryable error and creates nothing; `execute` without a
    successful `dispatch` does not call the provider (EFF-18)."""
    call, actor = env.harness.propose, env.harness.actor
    assert call is not None and actor is not None
    running, client = await started(env)
    async with client as c:
        env.approval.down = True
        sc = envelope(await c.call(call.tool, call.args, principal=actor))
        assert (sc["code"], sc["retryable"]) == ("temporarily_unavailable", True) and not env.approval.intents
        env.approval.down = False
        await proposed(env, c)
    intent_id, body = intent_of(env)
    for status in (503, 423):
        env.approval.dispatch_status = status
        r = await execute(env, running, intent_id, body)
        assert r.status_code in (409, 423, 503) and r.json().get("status") != "succeeded", r.text
    assert running.calls() == 0


@pytest.mark.effects
@pytest.mark.check("MCC-09")
async def test_effects_readiness_needs_the_executor_jwks(env: Env) -> None:
    """With effect tools, `/readyz` also needs the executor JWKS of the Approval Service (MCC-09)."""
    env.approval.down = True
    _, client = await started(env)
    async with client as c:
        ready = await c.http.get("/readyz")
    assert (ready.status_code, ready.json()) == (503, {"status": "not_ready"})
