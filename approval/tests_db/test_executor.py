"""M8: executor ↔ `demo` ↔ Approval Service end to end on PostgreSQL, HTTP over Unix sockets. AC-M8-01…12, 14.

Every case counts provider calls in the fake provider's journal (conformance matrix: «ноль обращений к провайдеру»)
and reads the database as the owner role. Executors are real `Executor` objects, each on its own connection of
the `gxg_executor` role; races use threads against one PostgreSQL (EX-10).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from effects_stack import (
    ACTOR_A,
    ACTOR_B,
    API_ID,
    CONNECTOR,
    DEMO,
    EXECUTOR_ID,
    KID,
    MCP_ID,
    DemoClient,
    DemoConfig,
    Keys,
    Servers,
    auto_policy,
    bundle,
    demo_process,
    jwks,
    pub,
    seed,
    serve_demo,
    write_bundle,
)
from fastmcp import Client
from prometheus_client import REGISTRY
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from demo_connector.effects import args_hash
from gxg_approval import keys as keymgmt
from gxg_approval.api import Deps, create_app
from gxg_approval.crypto import Decryptor, Encryptor
from gxg_approval.executor import Executor, Reserved, SigningKeys
from gxg_approval.mcp import create_server
from gxg_connector_kit import _jwt, _schema, canonical, exec_token, svc_jwt
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.identity import StubVerifier

TEXT, RECIPIENT = "hello partner", "partner_1"
PAST = "2020-01-01T00:00:00Z"


@dataclass
class Stack:
    pg: Any
    owner: psycopg.Connection[Any]
    keys: Keys
    dir: Path
    api_pool: ConnectionPool[Any]
    published: dict[str, Any] = field(default_factory=dict)  # the JWKS file of `api` (AS-09)
    built_at: float = field(default_factory=lambda: time.time() - 120)
    executors: list[Executor] = field(default_factory=list)
    signing: list[dict[str, str]] = field(default_factory=list)  # GXG_EXEC_SIGNING_KEYS_FILE content
    clock: list[float] = field(default_factory=lambda: [0.0])  # offset of the connector's JWKS clock

    def __post_init__(self) -> None:
        self.api_sock = str(self.dir / "api.sock")
        data = bundle(self.keys, built_at=self.built_at)
        write_bundle(self.keys, self.dir / "bundle", data)
        (self.dir / "demo-svc.seed").write_text(seed(self.keys.services[CONNECTOR]))
        self.store = BundleStore({KID: self.keys.ci.public_key()}, state_file=self.dir / "api-state.json")
        self.store.load(data, (self.dir / "bundle" / "grants-bundle.json.sig").read_bytes())
        self.published = jwks(self.keys.executor)
        self.signing = [{"seed": seed(self.keys.executor), "not_before": PAST}]
        encryptor = Encryptor({t: {p: pk for p, (_, pk) in v.items()} for t, v in self.keys.payload.items()})
        self.deps = Deps(API_ID, self.api_pool, self.store, encryptor, lambda: self.published)
        self.api = Servers([(create_app(self.deps), self.api_sock)])
        self.cfg = DemoConfig(str(self.dir), f"{KID}={pub(self.keys.ci)}", pub_issuer(self.keys), self.api_sock)
        self.demo = DemoClient(self.cfg, self.keys)
        self.demo_servers, self.connector = serve_demo(self.cfg, lambda: time.time() + self.clock[0])

    # --- bundle ---------------------------------------------------------------------------------
    def reload(self, **kw: Any) -> None:
        """A newer bundle on both sides (api: loaded; demo: files, picked up on the next call, GR-12)."""
        self.built_at += 1
        data = bundle(self.keys, built_at=self.built_at, **kw)
        write_bundle(self.keys, self.dir / "bundle", data)
        self.store.load(data, (self.dir / "bundle" / "grants-bundle.json.sig").read_bytes())

    # --- executors --------------------------------------------------------------------------------
    def executor(self, name: str = "ex-1", keys_now: Callable[[], float] = time.time) -> Executor:
        """keys_now: the clock of the key choice only (AS-10 `not_before`); tokens carry the real time."""
        client = self.demo.internal()
        ex = Executor(
            self.pg.connect("gxg_executor"),
            instance=name,
            service_id=EXECUTOR_ID,
            store=self.store,
            decryptor=Decryptor("args", {t: v["args"][0] for t, v in self.keys.payload.items()}),
            keys=SigningKeys(lambda: self.signing, keys_now),
            svc_key=self.keys.services[EXECUTOR_ID],
            http=lambda service: client if service.id == CONNECTOR else httpx.Client(),
        )
        self.executors.append(ex)
        return ex

    def drain(self, ex: Executor, until: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while not until():
            assert time.time() < deadline, "condition not reached"
            if not ex.run_once():
                time.sleep(0.05)

    # --- reading the database as the owner --------------------------------------------------------
    def intent(self, intent_id: str) -> dict[str, Any]:
        row = (
            self.owner.cursor(row_factory=dict_row)
            .execute("SELECT * FROM intents WHERE intent_id = %s", (intent_id,))
            .fetchone()
        )
        assert row is not None
        return row

    def status(self, intent_id: str) -> str:
        return str(self.intent(intent_id)["status"])

    def executions(self, intent_id: str) -> list[tuple[Any, ...]]:
        return self.owner.execute(
            "SELECT attempt, state, error_code FROM executions WHERE intent_id = %s ORDER BY attempt", (intent_id,)
        ).fetchall()

    def dispatches(self, intent_id: str) -> int:
        row = self.owner.execute(
            "SELECT count(*) FROM audit_events WHERE intent_id = %s AND action = 'dispatch'", (intent_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    # --- helpers for direct `execute` calls (as an attacker or a broken executor would) ---------------
    def propose(self, principal: str = ACTOR_A, board: str = "a-public", **args: Any) -> dict[str, Any]:
        out = self.demo.propose(principal, {"board_id": board, "recipient": RECIPIENT, "text": TEXT, **args})
        assert out.get("isError") is not True, out
        doc: dict[str, Any] = out["structuredContent"]
        return doc

    def claims(self, r: Reserved, **over: Any) -> dict[str, Any]:
        i = self.intent(str(r.intent_id))
        base = {
            "aud": CONNECTOR,
            "sub": str(r.intent_id),
            "jti": str(r.execution_id),
            "ten": i["tenant_id"],
            "req": i["requester_principal"],
            "tool": f"{i['tool_key']}@{i['tool_version']}",
            "con": i["connection_id"],
            "res": i["resource_id"],
            "ahs": i["canonical_args_hash"],
            "idk": i["idempotency_key"],
        }
        return {**base, **over}

    def execute(self, r: Reserved, token: str, args: dict[str, Any] | None = None) -> httpx.Response:
        auth = svc_jwt.sign(self.keys.services[EXECUTOR_ID], iss=EXECUTOR_ID, aud=CONNECTOR)
        body = {
            "intent_id": str(r.intent_id),
            "execution_id": str(r.execution_id),
            "execution_token": token,
            "args": {"recipient": RECIPIENT, "text": TEXT} if args is None else args,
        }
        with self.demo.internal() as c:
            resp: httpx.Response = c.post(
                "/internal/v1/execute/demo.message.execute", json=body, headers={"Authorization": f"Bearer {auth}"}
            )
        return resp

    def close(self) -> None:
        self.demo_servers.stop()
        self.api.stop()
        for ex in self.executors:
            ex.conn.close()


def pub_issuer(keys: Keys) -> str:
    return "ed25519:" + _jwt.b64url_encode(_jwt.raw_public_key(keys.issuer.public_key))


def jws(signer: Ed25519PrivateKey | None, header: dict[str, Any], claims: dict[str, Any]) -> str:
    h = _jwt.b64url_encode(json.dumps(header).encode())
    p = _jwt.b64url_encode(json.dumps(claims).encode())
    return f"{h}.{p}." + (_jwt.b64url_encode(signer.sign(f"{h}.{p}".encode())) if signer else "")


@pytest.fixture
def stack(pg: Any, owner: psycopg.Connection[Any], api_pool: ConnectionPool[Any]) -> Iterator[Stack]:
    os.environ["GXG_PROFILE"] = "test"  # the test identity issuer of `demo` (demo-connector-v1 «Identity в тестах»)
    directory = Path(tempfile.mkdtemp(prefix="m8", dir="/tmp"))  # short: a Unix socket path has ~100 bytes
    s = Stack(pg, owner, Keys(), directory, api_pool)
    try:
        yield s
    finally:
        s.close()
        shutil.rmtree(directory, ignore_errors=True)


# --- AC-M8-01 -------------------------------------------------------------------------------------


def test_ac01_auto_approved_propose_succeeds_and_effects_status_says_so(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    """propose → auto-approved → executor → demo → provider: `effects.status` = succeeded within 10 s."""
    caplog.set_level("INFO")
    before = REGISTRY.get_sample_value(
        "gxg_executions_total", {"tenant": "t-alpha", "tool": "demo.message.propose", "outcome": "succeeded"}
    )
    started = time.time()
    created = stack.propose(idempotency_key="cand-1:v1")
    assert created["status"] == "approved"  # EFF-22
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "succeeded")
    issuer = stack.keys.issuer
    api = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=stack.api_sock), base_url="http://api")
    mcp = create_server(
        api,
        lambda: svc_jwt.sign(stack.keys.services[MCP_ID], iss=MCP_ID, aud=API_ID),
        StubVerifier(MCP_ID, issuer.public_key),
        lambda: issuer.headers(tenant_id="t-alpha", principal=ACTOR_A, audience=MCP_ID),
    )

    async def status() -> Any:
        async with Client(mcp) as c:
            return await c.call_tool("effects.status", {"intent_id": created["intent_id"]}, raise_on_error=False)

    res = asyncio.run(status())
    assert not res.is_error and res.structured_content["status"] == "succeeded"
    assert time.time() - started <= 10
    calls = stack.demo.calls()
    assert len(calls) == 1
    assert calls[0]["provider_key"] == created["intent_id"] and calls[0]["outcome"] == "ok"  # EFF-18 p.5
    assert calls[0]["text_hash"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert stack.intent(created["intent_id"])["attempts"] == 1
    assert stack.executions(created["intent_id"]) == [(1, "succeeded", None)]
    after = REGISTRY.get_sample_value(
        "gxg_executions_total", {"tenant": "t-alpha", "tool": "demo.message.propose", "outcome": "succeeded"}
    )
    assert (after or 0) - (before or 0) == 1
    (event,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "gxg_approval.executor"]
    execution = str(stack.owner.execute("SELECT execution_id FROM executions").fetchone()[0])  # type: ignore[index]
    assert event == {
        "event": "executed",
        "intent_id": created["intent_id"],
        "execution_id": execution,
        "tenant": "t-alpha",
        "tool": "demo.message.propose",
        "outcome": "succeeded",
        "error": None,
    }
    receipt = stack.owner.execute("SELECT provider_operation_id, provider_receipt_ref FROM executions").fetchone()
    assert receipt is not None and receipt[0] == receipt[1] and str(receipt[0]).startswith("demo-op-")


def test_propose_is_listed_and_execute_is_not(stack: Stack) -> None:
    """EX-01 at the connector: `execute`/`reconcile` are internal HTTP only, never MCP tools (EFF-02, MCC-07)."""
    assert stack.demo.tools() == ["demo.boards.list", "demo.message.propose", "demo.note.get", "demo.notes.search"]


def test_human_approval_path(stack: Stack, decide_db: Any) -> None:
    """Without an auto rule: pending_approval → a human approves (T2) → executed."""
    # M9: T1 freezes the tenant's approvers from the bundle and gxg_decide checks them, so the human is one
    human = "oidc:https://idp.example.invalid:9"
    grants = yaml.safe_load((DEMO / "fixtures/grants/t-alpha.yaml").read_text("utf-8"))
    grants["principals"]["humans"] = [{"id": human, "roles": ["approver"]}]
    stack.reload(
        grants={"t-alpha": grants}, policies={"t-alpha": {"tenant": "t-alpha", "schema_version": 1, "rules": []}}
    )
    created = stack.propose()
    assert created["status"] == "pending_approval"
    ex = stack.executor()
    assert ex.run_once() is False  # nothing to execute (the outbox row is a `notify`)
    i = stack.intent(created["intent_id"])
    decide_db(created["intent_id"], human, "approve", i["version"], i["canonical_args_hash"])
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "succeeded")
    assert len(stack.demo.calls()) == 1


# --- AC-M8-02 -------------------------------------------------------------------------------------


def test_ac02_50_intents_10_executors_no_dedupe_one_call_each(stack: Stack) -> None:
    """EX-10: real threads, one PostgreSQL; the provider does not dedupe and answers in 200 ms."""
    ids = [stack.propose(idempotency_key=f"race-{n:04d}")["intent_id"] for n in range(50)]
    stack.demo.mode("ok,no_dedupe,slow:200")
    stop = threading.Event()
    executors = [stack.executor(f"ex-{n}") for n in range(10)]
    threads = [threading.Thread(target=ex.run, args=(stop,), kwargs={"poll_s": 0.05}) for ex in executors]
    for t in threads:
        t.start()
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            row = stack.owner.execute(
                "SELECT count(*) FROM intents WHERE status = 'succeeded' AND intent_id = ANY(%s)",
                ([uuid.UUID(i) for i in ids],),
            ).fetchone()
            if row and row[0] == 50:
                break
            time.sleep(0.1)
    finally:
        stop.set()
        for t in threads:
            t.join()
    calls = stack.demo.calls()
    assert len(calls) == 50
    assert sorted(c["provider_key"] for c in calls) == sorted(ids)  # one per intent
    assert {c["outcome"] for c in calls} == {"ok"}
    reserved_by = stack.owner.execute("SELECT count(DISTINCT reserved_by) FROM executions").fetchone()
    assert reserved_by is not None and reserved_by[0] > 1  # really concurrent
    assert stack.owner.execute("SELECT count(*) FROM executions").fetchone() == (50,)


# --- AC-M8-03, 04, 05 -------------------------------------------------------------------------------


def reserve_one(stack: Stack, **propose: Any) -> tuple[Reserved, Executor]:
    created = stack.propose(**propose)
    ex = stack.executor()
    taken = ex.take()
    assert isinstance(taken, Reserved) and str(taken.intent_id) == created["intent_id"]
    return taken, ex


def test_ac03_invalid_execution_tokens_are_401_without_dispatch(stack: Stack) -> None:
    """The 9 cases of AC-M1-03 on `execute`: 401, provider untouched, `dispatch` never called (EX-03)."""
    r, _ = reserve_one(stack)
    key = stack.keys.executor
    kid = _jwt.thumbprint(key.public_key())
    other = Ed25519PrivateKey.generate()
    now = int(time.time())
    header = {"alg": "EdDSA", "typ": "gxg-exec+jwt", "kid": kid}

    def full(**over: Any) -> dict[str, Any]:
        doc = {**stack.claims(r), "iss": "gxg-executor", "iat": now, "exp": now + 60, **over}
        return {k: v for k, v in doc.items() if v is not None}

    replayed = exec_token.sign(key, stack.claims(r))
    first = stack.execute(r, replayed, {"recipient": RECIPIENT, "text": TEXT + "!"})  # spends the jti (422)
    assert first.status_code == 422
    cases = {
        "foreign signature": jws(other, header, full()),
        "unknown kid": jws(other, {**header, "kid": _jwt.thumbprint(other.public_key())}, full()),
        "iss": jws(key, header, full(iss="someone-else")),
        "aud": jws(key, header, full(aud="rogue")),
        "exp in the past > 5 s": jws(key, header, full(iat=now - 66, exp=now - 6)),
        "iat in the future > 5 s": jws(key, header, full(iat=now + 10, exp=now + 60)),
        "missing claim": jws(key, header, full(idk=None)),
        "alg none": jws(None, {**header, "alg": "none"}, full()),
        "replayed jti": replayed,
    }
    for why, token in cases.items():
        resp = stack.execute(r, token)
        assert resp.status_code == 401, (why, resp.text)
        assert resp.json() == {"code": "unauthenticated", "message": "token rejected", "retryable": False}
    no_auth = stack.demo.internal().post("/internal/v1/execute/demo.message.execute", json={})
    assert no_auth.status_code == 401  # no service JWT of an executor (AS-06)
    assert stack.demo.calls() == []
    assert stack.dispatches(str(r.intent_id)) == 0
    assert stack.executions(str(r.intent_id)) == [(1, "reserved", None)]


def test_ac04_replay_after_success_is_409_and_the_provider_is_called_once(stack: Stack) -> None:
    r, ex = reserve_one(stack)
    token = exec_token.sign(stack.keys.executor, stack.claims(r))
    ok = stack.execute(r, token)
    assert ok.status_code == 200 and ok.json()["status"] == "succeeded"
    again = stack.execute(r, token)
    assert again.status_code == 409 and again.json()["code"] == "conflict"
    # EX-04: the same token straight at `dispatch` again (as a second connector instance would): refused
    resp = httpx.Client(transport=httpx.HTTPTransport(uds=stack.api_sock), base_url="http://api").post(
        f"/v1/executions/{r.execution_id}/dispatch",
        json={"execution_token": token},
        headers={"Authorization": "Bearer " + svc_jwt.sign(stack.keys.services[CONNECTOR], iss=CONNECTOR, aud=API_ID)},
    )
    assert resp.status_code in (401, 409)  # api's jti cache, or the CAS: never a second dispatch
    assert len(stack.demo.calls()) == 1
    assert stack.dispatches(str(r.intent_id)) == 1
    assert ex.run_once() is False  # and the executor has nothing left to do


@pytest.mark.parametrize(
    "args",
    [{"recipient": RECIPIENT, "text": "hello partneR"}, {"recipient": "partner_2", "text": TEXT}],
    ids=["text", "recipient"],
)
def test_ac05_changed_args_are_422(stack: Stack, args: dict[str, Any]) -> None:
    r, _ = reserve_one(stack)
    resp = stack.execute(r, exec_token.sign(stack.keys.executor, stack.claims(r)), args)
    assert resp.status_code == 422 and resp.json()["code"] == "invalid_input"
    assert stack.demo.calls() == [] and stack.dispatches(str(r.intent_id)) == 0


@pytest.mark.parametrize(
    "over",
    [{"res": "demo:demo-main/board/b-public", "ten": "t-beta"}, {"res": "demo:demo-main/board/nope"}],
    ids=["other-tenant", "no-such-board"],
)
def test_token_for_another_target_is_refused(stack: Stack, over: dict[str, str]) -> None:
    """EFF-18 п.3: a token (validly signed, hash over its own claims) for a target this connector does not serve."""
    r, _ = reserve_one(stack)
    claims = stack.claims(r, **over)
    target = (claims["tool"].split("@")[0], "1.0.0", claims["ten"], claims["con"], claims["res"])
    claims["ahs"] = args_hash(*target, {"recipient": RECIPIENT, "text": TEXT})
    resp = stack.execute(r, exec_token.sign(stack.keys.executor, claims))
    assert resp.status_code == 403, resp.text
    assert stack.demo.calls() == [] and stack.dispatches(str(r.intent_id)) == 0


# --- AC-M8-06 -------------------------------------------------------------------------------------


def test_ac06_commit_then_drop_is_unknown_then_reconciled_succeeded(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    """EX-11: the provider executed and the answer was lost: outcome_unknown, reconcile → reconciled_succeeded."""
    stack.demo.mode("commit_then_drop", count=1)
    created = stack.propose()
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "reconciled_succeeded")
    assert len(stack.demo.calls()) == 1
    assert stack.executions(created["intent_id"]) == [(1, "reconciled_executed", None)]
    actions = stack.owner.execute(
        "SELECT action, to_status FROM audit_events WHERE intent_id = %s ORDER BY id", (created["intent_id"],)
    ).fetchall()
    assert ("complete_outcome_unknown", "outcome_unknown") in actions
    assert actions[-1] == ("reconcile_executed", "reconciled_succeeded")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]  # no 24 h alert for a resolved intent


# --- AC-M8-07 -------------------------------------------------------------------------------------


def test_ac07_connector_killed_after_dispatch(stack: Stack) -> None:
    """Kill the connector process after `dispatch`, before its answer: outcome_unknown well within
    dispatch_timeout_s + 30 s, no new provider call; reconcile once the connector is back (T13, T15)."""
    stack.demo_servers.stop()
    with demo_process(stack.cfg) as proc:
        stack.demo.mode("ok,slow:30000")  # the provider got it, the answer would take 30 s
        created = stack.propose()
        ex = stack.executor()
        taken = ex.take()
        assert isinstance(taken, Reserved)
        worker = threading.Thread(target=ex.execute, args=(taken,))
        worker.start()
        deadline = time.time() + 20
        while stack.executions(created["intent_id"])[0][1] != "dispatching":
            assert time.time() < deadline
            time.sleep(0.05)
        while not stack.demo.calls():
            assert time.time() < deadline
            time.sleep(0.05)
        killed_at = time.time()
        proc.kill()
        worker.join(timeout=30)
        assert not worker.is_alive()
    assert stack.status(created["intent_id"]) == "outcome_unknown"
    assert time.time() - killed_at < 60 + 30  # dispatch_timeout_s of the tool (bundle) + 30 s
    provider_calls = 1
    # the connector is down: reconcile finds nothing out and tries again later (T17); dispatched 2 h ago → 1 h
    stack.owner.execute("UPDATE executions SET dispatched_at = now() - interval '2 hours'")
    version = stack.intent(created["intent_id"])["version"]
    stack.drain(ex, lambda: stack.intent(created["intent_id"])["version"] > version)
    assert stack.status(created["intent_id"]) == "outcome_unknown"
    pending = stack.owner.execute(
        "SELECT round(extract(epoch FROM available_at - created_at)) FROM outbox WHERE intent_id = %s "
        "AND topic = 'reconcile' AND delivered_at IS NULL",
        (created["intent_id"],),
    ).fetchone()
    assert pending == (3600,)
    stack.owner.execute("UPDATE outbox SET available_at = now() WHERE intent_id = %s", (created["intent_id"],))
    stack.demo_servers, stack.connector = serve_demo(stack.cfg)  # back, same provider journal
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "reconciled_succeeded")
    assert len(stack.demo.calls()) == provider_calls


def test_reconcile_gives_up_after_24_hours(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    """T17: after 24 h of `unknown` no reconcile is queued any more; a human decides (T18), with an alert."""
    stack.demo.mode("commit_then_drop", count=1)
    created = stack.propose()
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "outcome_unknown")
    stack.demo_servers.stop()  # the connector cannot tell
    stack.owner.execute("UPDATE executions SET dispatched_at = now() - interval '25 hours'")
    version = stack.intent(created["intent_id"])["version"]
    stack.drain(ex, lambda: stack.intent(created["intent_id"])["version"] > version)
    assert ex.run_once() is False
    assert stack.status(created["intent_id"]) == "outcome_unknown"
    left = stack.owner.execute(
        "SELECT count(*) FROM outbox WHERE intent_id = %s AND topic = 'reconcile' AND delivered_at IS NULL",
        (created["intent_id"],),
    ).fetchone()
    assert left == (0,)
    alert = f"ALERT executor: intent {created['intent_id']} outcome_unknown for 24 h: manual review (T17, T18)"
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [alert]
    stack.demo_servers, stack.connector = serve_demo(stack.cfg)  # for the fixture's teardown


def test_ac07_t14_when_the_executor_dies_too(stack: Stack, pg: Any) -> None:
    """Nobody completes the dispatched execution: the sweeper's T14 makes it outcome_unknown after the lease."""
    r, _ = reserve_one(stack)
    stack.demo.mode("ok,slow:30000")

    def call() -> None:
        try:
            stack.execute(r, exec_token.sign(stack.keys.executor, stack.claims(r)))
        except httpx.HTTPError:
            pass

    threading.Thread(target=call, daemon=True).start()
    deadline = time.time() + 20
    while stack.executions(str(r.intent_id))[0][1] != "dispatching":
        assert time.time() < deadline
        time.sleep(0.05)
    lease = stack.owner.execute(
        "SELECT round(extract(epoch FROM dispatch_lease_until - dispatched_at)) FROM executions"
    ).fetchone()
    assert lease == (90,)  # dispatch_timeout_s 60 (manifest via bundle) + 30
    stack.owner.execute("UPDATE executions SET dispatch_lease_until = now() - interval '1 second'")
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_sweep(100)")
    assert stack.status(str(r.intent_id)) == "outcome_unknown"
    assert len(stack.demo.calls()) == 1


# --- AC-M8-08 -------------------------------------------------------------------------------------


def test_ac08_timeout_before_start_twice_then_ok(stack: Stack) -> None:
    """EX-12, EX-23: failed_before_effect is retried with a new execution and the same provider key (intent_id)."""
    stack.demo.mode("timeout_before_start", count=2)
    created = stack.propose(idempotency_key="retry-key-1")
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) in ("succeeded", "failed"))
    assert stack.status(created["intent_id"]) == "succeeded"
    assert stack.intent(created["intent_id"])["attempts"] == 3
    calls = stack.demo.calls(created["intent_id"])  # one provider key for every attempt
    assert [c["outcome"] for c in calls] == ["timeout_before_start", "timeout_before_start", "ok"]
    assert len({c["execution_id"] for c in calls}) == 3  # a new execution_id per attempt
    assert [e[1] for e in stack.executions(created["intent_id"])] == [
        "failed_before_effect",
        "failed_before_effect",
        "succeeded",
    ]


def test_ac08_max_attempts_2_ends_failed(stack: Stack) -> None:
    stack.demo.mode("timeout_before_start", count=2)
    created = stack.propose(idempotency_key="retry-key-2")
    stack.owner.execute("UPDATE intents SET max_attempts = 2 WHERE intent_id = %s", (created["intent_id"],))
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) in ("succeeded", "failed"))
    assert stack.status(created["intent_id"]) == "failed"
    assert stack.intent(created["intent_id"])["attempts"] == 2
    assert len(stack.demo.calls(created["intent_id"])) == 2


def test_rejecting_provider_fails_before_effect(stack: Stack) -> None:
    stack.demo.mode("reject")
    created = stack.propose()
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "failed")
    assert [c["outcome"] for c in stack.demo.calls()] == ["reject"] * 3


# --- AC-M8-09 -------------------------------------------------------------------------------------


def test_ac09_executor_dies_between_reserve_and_dispatch(stack: Stack, pg: Any) -> None:
    """T10: after the 60 s lease the execution is released, the intent approved again and then executed;
    `attempts` counts only the dispatched attempt."""
    created = stack.propose()
    dead = stack.executor("dead")
    assert isinstance(dead.take(), Reserved)  # reserved, then the process «dies»: no execute
    lease = stack.owner.execute(
        "SELECT round(extract(epoch FROM reserve_lease_until - reserved_at)) FROM executions"
    ).fetchone()
    assert lease == (60,)
    stack.owner.execute("UPDATE executions SET reserve_lease_until = now() - interval '1 second'")
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_sweep(100)")
    assert stack.status(created["intent_id"]) == "approved"
    stack.drain(stack.executor("alive"), lambda: stack.status(created["intent_id"]) == "succeeded")
    assert stack.executions(created["intent_id"]) == [
        (1, "released", "reserve_lease_expired"),
        (2, "succeeded", None),
    ]  # T10 reason (M10)
    assert stack.intent(created["intent_id"])["attempts"] == 1
    assert len(stack.demo.calls()) == 1


# --- AC-M8-10 -------------------------------------------------------------------------------------


def test_ac10_kill_switch_after_reserve_before_dispatch(stack: Stack, pg: Any) -> None:
    """EX-13: `dispatch` → 423, the provider is not called; the other tenant's intents still run."""
    alpha = stack.propose()
    beta = stack.propose(ACTOR_B, board="b-public")
    ex = stack.executor()
    r = ex.take()
    assert isinstance(r, Reserved) and str(r.intent_id) == alpha["intent_id"]
    with pg.connect("gxg_admin") as admin:
        admin.execute("INSERT INTO switches VALUES ('tenant:t-alpha', true, 'incident', 'oidc:op', now())")
    resp = stack.execute(r, exec_token.sign(stack.keys.executor, stack.claims(r)))
    assert resp.status_code == 423 and resp.json()["code"] == "effect_disabled"
    assert stack.demo.calls() == []
    ex.execute(r)  # the executor's own call of the same reservation: refused at dispatch as well (T12a)
    assert stack.executions(alpha["intent_id"])[0][1] == "failed_before_effect"
    stack.drain(ex, lambda: stack.status(beta["intent_id"]) == "succeeded")
    assert ex.run_once() is True  # the re-queued row: T7 refuses (423), the row stays with a backoff
    assert ex.run_once() is False  # it waits ≥ 5 s: nothing else is due
    assert stack.status(alpha["intent_id"]) == "approved"  # waits: T7 refuses while the switch is on
    assert [c["tenant"] for c in stack.demo.calls()] == ["t-beta"]
    deferred = stack.owner.execute(
        "SELECT attempts FROM outbox WHERE intent_id = %s AND topic = 'execute' AND delivered_at IS NULL",
        (alpha["intent_id"],),
    ).fetchone()
    assert deferred == (1,)  # kept, one refusal, then the backoff
    with pg.connect("gxg_admin") as admin:
        admin.execute("UPDATE switches SET blocked = false WHERE scope = 'tenant:t-alpha'")
    stack.owner.execute("UPDATE outbox SET available_at = now() - interval '1 hour'")
    stack.drain(ex, lambda: stack.status(alpha["intent_id"]) == "succeeded")


def test_same_key_of_two_requesters_is_two_provider_effects(stack: Stack) -> None:
    """EFF-08 (per requester) + EFF-18 p.5: two agents of t-alpha use the same idempotency key with different
    messages → two intents, two provider effects keyed by intent_id, two operation ids, both succeeded."""
    other = "agent:t-alpha:actor2"
    grants = yaml.safe_load((DEMO / "fixtures" / "grants" / "t-alpha.yaml").read_text())
    grants["principals"]["agents"].append({"id": other})
    for g in grants["grants"]:
        if g["id"] == "alpha-actor-public":
            g["subjects"].append(other)
    policy = auto_policy("t-alpha", ACTOR_A)
    policy["rules"][0]["match"]["requesters"].append(other)
    stack.reload(grants={"t-alpha": grants}, policies={"t-alpha": policy, "t-beta": auto_policy("t-beta", ACTOR_B)})
    first = stack.propose(idempotency_key="shared-key-1", text="first message")
    second = stack.propose(other, idempotency_key="shared-key-1", text="second message", recipient="partner_2")
    assert first["intent_id"] != second["intent_id"]
    ex = stack.executor()
    ids = (first["intent_id"], second["intent_id"])
    stack.drain(ex, lambda: all(stack.status(i) == "succeeded" for i in ids))
    calls = stack.demo.calls()
    assert sorted(c["provider_key"] for c in calls) == sorted(ids)
    assert {c["outcome"] for c in calls} == {"ok"}
    assert {c["text_hash"] for c in calls} == {
        hashlib.sha256(t).hexdigest() for t in (b"first message", b"second message")
    }
    ops = stack.owner.execute(
        "SELECT intent_id::text, provider_operation_id FROM executions WHERE state = 'succeeded'"
    ).fetchall()
    assert sorted(o[0] for o in ops) == sorted(ids)
    assert len({o[1] for o in ops}) == 2 and all(o[1] for o in ops)  # two provider receipts


# --- AC-M8-11 -------------------------------------------------------------------------------------


def test_ac11_propose_grant_revoked_between_approve_and_execute(stack: Stack) -> None:
    """EX-14: a new bundle without the requester's `propose` grant → the connector refuses, no provider call."""
    created = stack.propose()
    grants = yaml.safe_load((DEMO / "fixtures" / "grants" / "t-alpha.yaml").read_text())
    for g in grants["grants"]:
        if g["id"] == "alpha-actor-public":
            g["actions"] = ["read"]
    stack.reload(grants={"t-alpha": grants})
    ex = stack.executor()
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.executions(created["intent_id"])[0][1:] == ("failed_before_effect", "http_403")
    r2 = ex.take()  # T12a sent it back to approved: the next attempt is refused the same way
    assert isinstance(r2, Reserved)
    resp = stack.execute(r2, exec_token.sign(stack.keys.executor, stack.claims(r2)))
    assert resp.status_code == 403 and resp.json()["code"] == "permission_denied"
    assert stack.demo.calls() == [] and stack.dispatches(created["intent_id"]) == 0


# --- AC-M8-12 -------------------------------------------------------------------------------------


def test_ac12_approval_service_unavailable(stack: Stack) -> None:
    """EX-16: propose → temporarily_unavailable and no intent; execute without a reachable dispatch → no call."""
    warm = stack.propose()
    stack.drain(stack.executor("warm"), lambda: stack.status(warm["intent_id"]) == "succeeded")  # JWKS cached
    r, _ = reserve_one(stack)
    token = exec_token.sign(stack.keys.executor, stack.claims(r))
    stack.api.stop()
    out = stack.demo.propose(ACTOR_A, {"board_id": "a-public", "recipient": RECIPIENT, "text": TEXT})
    assert out["isError"] is True and out["structuredContent"]["code"] == "temporarily_unavailable"
    assert out["structuredContent"]["retryable"] is True
    assert stack.owner.execute("SELECT count(*) FROM intents").fetchone() == (2,)  # no new intent
    resp = stack.execute(r, token)
    assert resp.status_code == 503 and resp.json()["code"] == "temporarily_unavailable"
    assert len(stack.demo.calls()) == 1  # only the warm-up
    assert stack.executions(str(r.intent_id)) == [(1, "reserved", None)]


# --- AC-M8-14 -------------------------------------------------------------------------------------


def test_ac14_rotation_without_downtime_and_removal_of_a_compromised_kid(stack: Stack) -> None:
    """EX-24: a new `kid` is accepted ≤ 10 min after publication; a removed one is refused ≤ 10 min (AS-09, AS-10)."""
    t0 = time.time()
    clock = {"executor": 0.0}
    old_kid = _jwt.thumbprint(stack.keys.executor.public_key())
    first = stack.propose()
    ex = stack.executor(keys_now=lambda: time.time() + clock["executor"])
    stack.drain(ex, lambda: stack.status(first["intent_id"]) == "succeeded")  # the connector cached JWKS {old}
    keys_file, jwks_file = stack.dir / "exec-keys.json", stack.dir / "exec-jwks.json"
    keys_file.write_text(json.dumps(stack.signing))
    jwks_file.write_text(json.dumps(stack.published))
    new_kid = keymgmt.rotate(keys_file, jwks_file, now=t0)
    stack.signing, stack.published = json.loads(keys_file.read_text()), json.loads(jwks_file.read_text())
    assert {k["kid"] for k in stack.published["keys"]} == {old_kid, new_kid}
    assert oct(keys_file.stat().st_mode & 0o777) == "0o600"
    # before not_before (publication + 15 min) the executor keeps signing with the old key
    assert _jwt.thumbprint(ex.keys.current().public_key()) == old_kid
    clock["executor"] = 15 * 60
    assert _jwt.thumbprint(ex.keys.current().public_key()) == new_kid
    # the connector's JWKS cache (10 min) has not seen the new kid yet: refused before dispatch, no call
    second = stack.propose()
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.executions(second["intent_id"])[0][1:] == ("failed_before_effect", "http_401")
    stack.clock[0] = 10 * 60  # 10 minutes later the connector refetches: the new kid is accepted
    stack.drain(ex, lambda: stack.status(second["intent_id"]) == "succeeded")
    assert len(stack.demo.calls()) == 2
    # compromise of the new key: removed from both files; api refuses it at once, every reservation released
    third = stack.propose()
    r3 = ex.take()
    assert isinstance(r3, Reserved)
    keymgmt.remove(keys_file, jwks_file, new_kid)
    stack.signing, stack.published = json.loads(keys_file.read_text()), json.loads(jwks_file.read_text())
    assert ex.release_reserved() == 1  # AS-10
    assert stack.executions(third["intent_id"])[0][1] == "released"
    assert stack.status(third["intent_id"]) == "approved"
    # the old key is still published and active: the executor falls back to it, the next attempt runs
    stack.drain(ex, lambda: stack.status(third["intent_id"]) == "succeeded")
    assert len(stack.demo.calls()) == 3


def test_ac14_removed_kid_is_refused_at_dispatch_at_once_and_by_the_connector_within_10_min(stack: Stack) -> None:
    warm = stack.propose()
    stack.drain(stack.executor("warm"), lambda: stack.status(warm["intent_id"]) == "succeeded")  # JWKS cached
    r, _ = reserve_one(stack)
    token = exec_token.sign(stack.keys.executor, stack.claims(r))
    stack.published = {"keys": []}  # compromised kid removed from the JWKS file
    resp = stack.execute(r, token)  # the connector's cache still trusts it; `api` (current file) does not
    assert resp.status_code == 409 and len(stack.demo.calls()) == 1
    r2, _ = reserve_one(stack)
    stack.clock[0] = 10 * 60  # after the connector's cache age the token is refused at the token check
    resp = stack.execute(r2, exec_token.sign(stack.keys.executor, stack.claims(r2)))
    assert resp.status_code == 401 and stack.dispatches(str(r2.intent_id)) == 0
    assert len(stack.demo.calls()) == 1


# --- executor details -----------------------------------------------------------------------------


def test_connector_not_registered_for_the_tenant_gets_no_token(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    """AS-25: the connector lost t-alpha in the bundle: the attempt is refused without any HTTP call."""
    caplog.set_level("INFO")
    r, ex = reserve_one(stack)
    stack.store.load(*_bundle_without_tenant(stack))
    ex.execute(r)
    assert stack.executions(str(r.intent_id))[0][1:] == ("failed_before_effect", "not_registered")
    (event,) = [json.loads(x.getMessage()) for x in caplog.records if x.name == "gxg_approval.executor"]
    assert (event["outcome"], event["error"]) == ("refused_before_dispatch", "not_registered")
    assert stack.demo.calls() == []


def _bundle_without_tenant(stack: Stack) -> tuple[bytes, bytes]:
    data = json.loads(bundle(stack.keys, built_at=stack.built_at + 5))
    for s in data["platform"]["services"]:
        if s["id"] == CONNECTOR:
            s["tenants"] = ["t-beta"]
    raw = canonical.dumps(data)
    sig = json.dumps({"alg": "EdDSA", "kid": KID, "sig": _jwt.b64url_encode(stack.keys.ci.sign(raw))}).encode()
    return raw, sig


def test_logs_carry_no_text_and_no_token(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    """SEC-18, EFF-21: the audit lines of `execute` hold ids and outcomes, never the text or the token."""
    caplog.set_level("INFO")
    created = stack.propose(text="very secret words")
    stack.drain(stack.executor(), lambda: stack.status(created["intent_id"]) == "succeeded")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert created["intent_id"] in logs
    assert "very secret words" not in logs and "eyJ" not in logs and "demo-secret-test" not in logs


def test_policy_used_by_the_stack_is_valid() -> None:
    assert _schema.validator("approval-policy").is_valid(auto_policy("t-alpha", ACTOR_A))


# --- executor branches (DoD-4) --------------------------------------------------------------------


def mock_executor(stack: Stack, handler: Callable[[httpx.Request], httpx.Response]) -> Executor:
    """An executor whose connector is a mock (answers the real connector never gives)."""
    ex = stack.executor("mock")
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://demo.internal:8081")
    ex.http = lambda _service: client
    return ex


def test_stale_outbox_rows_are_delivered_without_work(stack: Stack, pg: Any) -> None:
    cancelled = stack.propose()
    i = stack.intent(cancelled["intent_id"])
    with pg.connect("gxg_api") as api:
        api.execute("SELECT gxg_cancel(%s, 't-alpha', %s, %s)", (cancelled["intent_id"], ACTOR_A, i["version"]))
    exhausted = stack.propose()
    stack.owner.execute("UPDATE intents SET max_attempts = 0 WHERE intent_id = %s", (exhausted["intent_id"],))
    ex = stack.executor()
    assert [ex.run_once() for _ in range(3)] == [True, True, False]
    approved = stack.propose()  # its execute row delivered; a reconcile row for it must not reserve it
    stack.owner.execute("UPDATE outbox SET delivered_at = now() WHERE intent_id = %s", (approved["intent_id"],))
    stack.owner.execute(
        "INSERT INTO outbox (topic, intent_id, payload, created_at, available_at) "
        "VALUES ('reconcile', %s, '{}', now(), now())",
        (approved["intent_id"],),
    )
    assert [ex.run_once() for _ in range(2)] == [True, False]
    assert stack.status(approved["intent_id"]) == "approved"
    pending = stack.owner.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL "
                                  "AND topic IN ('execute', 'reconcile')").fetchone()  # fmt: skip
    assert pending == (0,)
    assert stack.owner.execute("SELECT count(*) FROM executions").fetchone() == (0,)
    assert stack.demo.calls() == []


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(200, text="<html>"), "http_200"),
        (httpx.Response(200, json={"status": "succeeded"}), "http_200"),  # not an effect-result
        (httpx.Response(500, json={"code": "internal_error"}), "http_500"),
        (
            httpx.Response(500, json={"operation_id": "x", "status": "succeeded", "reconciliation_required": False}),
            "http_500",
        ),  # a valid effect-result, but not a 200
    ],
)
def test_unusable_answers_before_dispatch_are_refused_attempts(
    stack: Stack, answer: httpx.Response, error: str
) -> None:
    created = stack.propose()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    ex = mock_executor(stack, handler)
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.executions(created["intent_id"]) == [(1, "failed_before_effect", error)]
    assert stack.intent(created["intent_id"])["attempts"] == 1  # T12a counts
    (req,) = seen
    assert req.url.path == "/internal/v1/execute/demo.message.execute"
    assert req.extensions["timeout"]["read"] == 60  # AS-13: dispatch_timeout_s of the tool from the bundle
    claims = _jwt_payload(req.headers["authorization"].split()[1])
    assert (claims["iss"], claims["aud"]) == (EXECUTOR_ID, CONNECTOR)


def _jwt_payload(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    doc: dict[str, Any] = json.loads(_jwt.b64url_decode(payload))
    return doc


def test_a_released_execution_is_not_completed(stack: Stack, pg: Any, caplog: pytest.LogCaptureFixture) -> None:
    """The sweeper released the reservation (T10) while the executor was away: its late failure changes nothing."""
    caplog.set_level("INFO")
    created = stack.propose()
    ex = mock_executor(stack, lambda _r: httpx.Response(503))
    r = ex.take()
    assert isinstance(r, Reserved)
    stack.owner.execute("UPDATE executions SET reserve_lease_until = now() - interval '1 second'")
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_sweep(100)")
    ex.execute(r)
    assert stack.executions(created["intent_id"]) == [(1, "released", "reserve_lease_expired")]  # T10 reason (M10)
    events = [json.loads(x.getMessage()) for x in caplog.records if x.name == "gxg_approval.executor"]
    assert events == [{"event": "ignored", "intent_id": created["intent_id"], "execution_id": str(r.execution_id),
                       "tenant": "t-alpha", "tool": "demo.message.propose", "outcome": "released",
                       "error": "http_503"}]  # fmt: skip
    audit = stack.owner.execute("SELECT details FROM audit_events WHERE action = 'complete_ignored'").fetchall()
    assert audit == [({"execution_id": str(r.execution_id), "outcome": "released", "error": "http_503"},)]


@pytest.mark.parametrize(
    "answer",
    [
        lambda body: httpx.Response(
            200, json={**body, "execution_id": "00000000-0000-4000-8000-000000000009", "finding": "executed"}
        ),
        lambda body: httpx.Response(200, json={"finding": "executed"}),
        lambda body: httpx.Response(404, json={**body, "finding": "executed"}),
        lambda body: httpx.Response(200, text="x"),
    ],
    ids=["other-execution", "invalid", "not-200", "not-json"],
)
def test_unusable_reconcile_answers_are_unknown(stack: Stack, answer: Callable[[dict[str, Any]], Any]) -> None:
    stack.demo.mode("commit_then_drop", count=1)
    created = stack.propose()
    stack.drain(stack.executor(), lambda: stack.status(created["intent_id"]) == "outcome_unknown")
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return cast(httpx.Response, answer({k: body[k] for k in ("intent_id", "execution_id")}))

    version = stack.intent(created["intent_id"])["version"]
    ex = mock_executor(stack, handler)
    assert ex.run_once() is True
    assert stack.status(created["intent_id"]) == "outcome_unknown"
    assert stack.intent(created["intent_id"])["version"] == version + 1  # T17
    execution = stack.owner.execute("SELECT execution_id FROM executions").fetchone()
    assert execution is not None
    assert bodies == [{"intent_id": created["intent_id"], "execution_id": str(execution[0]),
                       "idempotency_key": stack.intent(created["intent_id"])["idempotency_key"]}]  # fmt: skip


def test_connector_dropped_from_the_bundle_is_never_called(stack: Stack) -> None:
    """AS-25, AS-26: the connector entry, or its tool, is gone: no token, no call; reconcile stays unknown."""
    created = stack.propose()
    data = json.loads(bundle(stack.keys, built_at=stack.built_at + 5))
    for svc in data["platform"]["services"]:
        if svc["id"] == CONNECTOR:
            svc["tools"] = [t for t in svc["tools"] if t["name"] != "demo.message.propose"]
    raw = canonical.dumps(data)
    stack.store.load(
        raw, json.dumps({"alg": "EdDSA", "kid": KID, "sig": _jwt.b64url_encode(stack.keys.ci.sign(raw))}).encode()
    )
    ex = stack.executor()
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.executions(created["intent_id"]) == [(1, "failed_before_effect", "not_registered")]
    assert stack.demo.calls() == []


def test_as13_the_executor_waits_only_dispatch_timeout_s(stack: Stack) -> None:
    """AS-13: the HTTP timeout of `execute` is the tool's dispatch_timeout_s (1 s here); then T13."""
    stack.reload(timeout=1)
    stack.demo.mode("ok,slow:4000")
    created = stack.propose()
    ex = stack.executor()
    r = ex.take()
    assert isinstance(r, Reserved)
    started = time.time()
    ex.execute(r)
    assert time.time() - started < 3.5
    assert stack.status(created["intent_id"]) == "outcome_unknown"
    assert stack.executions(created["intent_id"]) == [(1, "outcome_unknown", "no_response")]


def test_run_survives_an_error_and_stops(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    ex = stack.executor()
    stop = threading.Event()
    calls = []

    def flaky() -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("one bad row")
        stop.set()
        return True

    ex.run_once = flaky  # type: ignore[method-assign]
    started = time.time()
    worker = threading.Thread(target=ex.run, args=(stop,), kwargs={"poll_s": 0.2}, daemon=True)
    worker.start()
    worker.join(timeout=5)
    stop.set()  # a broken loop must not outlive the test
    assert not worker.is_alive() and len(calls) == 2
    assert 0.15 < time.time() - started < 5  # waited one poll after the error
    assert [r.getMessage() for r in caplog.records if r.name == "gxg_approval.executor"] == ["executor: error"]


def test_late_reconcile_that_finds_the_effect_raises_no_alert(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    stack.demo.mode("commit_then_drop", count=1)
    created = stack.propose()
    ex = stack.executor()
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "outcome_unknown")
    stack.owner.execute("UPDATE executions SET dispatched_at = now() - interval '25 hours'")
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "reconciled_succeeded")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_run_takes_the_next_row_at_once_while_there_is_work(stack: Stack) -> None:
    """The poll interval is only for an empty queue: three queued intents take far less than one poll."""
    ids = [stack.propose()["intent_id"] for _ in range(3)]
    ex = stack.executor()
    stop = threading.Event()
    worker = threading.Thread(target=ex.run, args=(stop,), kwargs={"poll_s": 30})
    started = time.time()
    worker.start()
    try:
        while not all(stack.status(i) == "succeeded" for i in ids):
            assert time.time() - started < 10
            time.sleep(0.05)
    finally:
        stop.set()
        worker.join()


# --- cycle 2 (critic cycle 1: findings 1, 2, 3, 6) ---------------------------------------------------------


def test_repeat_propose_after_success_returns_the_intent_as_it_is_now(stack: Stack) -> None:
    """EFF-08 through the real MCP client: the repeat returns 200 with the same intent, now `succeeded`."""
    first = stack.propose(idempotency_key="repeat-key-1")
    stack.drain(stack.executor(), lambda: stack.status(first["intent_id"]) == "succeeded")
    again = stack.propose(idempotency_key="repeat-key-1")
    assert again == {"intent_id": first["intent_id"], "status": "succeeded"}
    assert len(stack.demo.calls()) == 1


def test_without_an_active_signing_key_nothing_is_reserved_and_no_attempt_is_spent(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    """AS-10: after removing the only key (or before a new key's not_before) intents wait, they do not fail."""
    later = {"seed": stack.signing[0]["seed"], "not_before": "2999-01-01T00:00:00Z"}
    stack.signing = [later]
    created = stack.propose()
    ex = stack.executor()
    assert ex.run_once() is True  # the row is deferred with the backoff
    (warning,) = [r for r in caplog.records if r.name == "gxg_approval.executor"]
    assert warning.levelname == "WARNING"
    assert warning.getMessage() == (
        f"executor: no active signing key, intent {created['intent_id']} waits (AS-10: keys rotate)"
    )
    assert ex.run_once() is False
    assert stack.status(created["intent_id"]) == "approved"
    assert stack.intent(created["intent_id"])["attempts"] == 0 and stack.executions(created["intent_id"]) == []
    deferred = stack.owner.execute(
        "SELECT attempts FROM outbox WHERE intent_id = %s AND delivered_at IS NULL AND topic = 'execute'",
        (created["intent_id"],),
    ).fetchone()
    assert deferred == (1,)
    stack.signing = [{**later, "not_before": PAST}]  # a key becomes active
    stack.owner.execute("UPDATE outbox SET available_at = now() - interval '1 hour'")
    stack.drain(ex, lambda: stack.status(created["intent_id"]) == "succeeded")
    assert stack.intent(created["intent_id"])["attempts"] == 1


def test_keys_cli_rotate_remove_and_release(stack: Stack, pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented commands: rotate/remove over the two files, release as the executor role (AS-10)."""
    from gxg_approval import cli

    keys_file, jwks_file = stack.dir / "exec-keys" / "signing-keys.json", stack.dir / "config" / "exec-jwks.json"
    keys_file.parent.mkdir()
    jwks_file.parent.mkdir()
    monkeypatch.setenv("GXG_EXEC_SIGNING_KEYS_FILE", str(keys_file))
    monkeypatch.setenv("GXG_EXEC_JWKS_FILE", str(jwks_file))
    cli.main(["keys", "rotate"])
    (kid,) = [k["kid"] for k in json.loads(jwks_file.read_text())["keys"]]
    assert oct(keys_file.stat().st_mode & 0o777) == "0o600"
    created = stack.propose()
    assert isinstance(stack.executor().take(), Reserved)
    cli.main(["keys", "remove", "--kid", kid])
    assert json.loads(jwks_file.read_text()) == {"keys": []} and json.loads(keys_file.read_text()) == []
    (stack.dir / "dsn").write_text(pg.dsn("gxg_executor"))
    monkeypatch.setenv("GXG_DB_DSN_FILE", str(stack.dir / "dsn"))
    cli.main(["keys", "release"])
    assert stack.executions(created["intent_id"])[0][1] == "released"
    assert stack.status(created["intent_id"]) == "approved"
    actor = stack.owner.execute(
        "SELECT actor FROM audit_events WHERE intent_id = %s ORDER BY id DESC LIMIT 1", (created["intent_id"],)
    ).fetchone()
    assert actor == ("keys release",)
    with pytest.raises(SystemExit, match="rotate"):
        cli.main(["keys", "remove"])


# Lock waits of this test's database only: pg_locks is cluster-wide, and mutmut runs mutants in parallel.
WAITING_HERE = (
    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid)"
    " WHERE NOT l.granted AND a.datname = current_database()"
)


def test_release_skips_an_execution_dispatched_while_it_waited_for_the_lock(stack: Stack, pg: Any) -> None:
    """gxg_release_reserved: the loop saw the execution `reserved`; by the time it holds the intent lock it is
    `dispatching` — it is skipped (CONTINUE WHEN), the call does not abort, the other reservation is released."""
    first, second = stack.propose(), stack.propose()
    ex = stack.executor()
    assert isinstance(ex.take(), Reserved) and isinstance(ex.take(), Reserved)
    blocker = pg.connect("gxg_owner")
    blocker.autocommit = False
    result: list[Any] = []
    releaser = threading.Thread(target=lambda: result.append(_release_or_error(pg)), daemon=True)
    try:  # a failed wait must not leave the lock held: the releaser would block the session forever
        blocker.execute("SELECT 1 FROM intents WHERE intent_id = %s FOR UPDATE", (first["intent_id"],))
        releaser.start()
        deadline = time.time() + 10
        while stack.owner.execute(WAITING_HERE).fetchone() != (1,):
            assert time.time() < deadline, "the release did not wait for the intent lock"
            time.sleep(0.05)
        blocker.execute("UPDATE executions SET state = 'dispatching', dispatched_at = now(), "
                        "dispatch_lease_until = now() + interval '90 seconds' WHERE intent_id = %s",
                        (first["intent_id"],))  # fmt: skip
        blocker.commit()
    finally:
        blocker.close()
    releaser.join(timeout=10)
    assert result == [1]
    assert stack.executions(first["intent_id"])[0][1] == "dispatching"
    assert stack.executions(second["intent_id"])[0][1] == "released"


def _release_or_error(pg: Any) -> Any:
    from gxg_approval.executor import release_reserved

    try:
        with pg.connect("gxg_executor") as conn:
            return release_reserved(conn, "test")
    except Exception as exc:
        return exc


# --- EX-25 (AS-12a, T12b): 429 rate_limited before dispatch defers without spending an attempt -----------------


def _due_now(stack: Stack, intent_id: str) -> None:
    stack.owner.execute(
        "UPDATE outbox SET available_at = now() WHERE intent_id = %s AND topic = 'execute' AND delivered_at IS NULL",
        (intent_id,),
    )


def _task_delay(stack: Stack, intent_id: str) -> float:
    row = stack.owner.execute(
        "SELECT extract(epoch FROM max(available_at) - now()) FROM outbox WHERE intent_id = %s AND topic = 'execute' "
        "AND delivered_at IS NULL",
        (intent_id,),
    ).fetchone()
    assert row is not None and row[0] is not None
    return float(row[0])


def limited(retry: Any = 30, code: str = "rate_limited") -> Callable[[httpx.Request], httpx.Response]:
    body = {"code": code, "message": "send quota", "retryable": True} | (
        {} if retry is None else {"retry_after_s": retry}
    )
    return lambda _r: httpx.Response(429, json=body)


def test_ex25_quota_429_then_ok_spends_no_attempt(stack: Stack) -> None:
    """The demo connector's own quota (`execute_quota`) answers 429 before dispatch: exec released with
    error_code rate_limited, attempts unchanged, the next reserve not before clamp(retry_after_s), then succeeded."""
    first = stack.propose(idempotency_key="quota-key-1")
    ex = stack.executor()
    stack.demo.mode("ok,execute_quota:1/60")
    stack.drain(ex, lambda: stack.status(first["intent_id"]) == "succeeded")
    second = stack.propose(idempotency_key="quota-key-2")
    i = second["intent_id"]
    stack.drain(ex, lambda: stack.executions(i) != [] and stack.status(i) == "approved")
    assert stack.executions(i) == [(1, "released", "rate_limited")] and stack.intent(i)["attempts"] == 0
    delay = _task_delay(stack, i)
    assert 55 * 1.0 - 2 <= delay <= 60 * 1.2 + 1  # clamp(retry_after_s ≈ 60, 5, 3600) plus jitter ≤ 20 %
    assert ex.run_once() is False  # T7 only from a due task: nothing to reserve before it
    stack.demo.mode("ok")
    _due_now(stack, i)
    stack.drain(ex, lambda: stack.status(i) == "succeeded")
    assert stack.intent(i)["attempts"] == 1
    assert [e[1:] for e in stack.executions(i)] == [("released", "rate_limited"), ("succeeded", None)]
    assert len(stack.demo.calls(i)) == 1
    with stack.pg.connect("gxg_api") as api:
        m = api.execute("SELECT labels, value FROM gxg_metrics() WHERE name = 'gxg_execute_deferred_total'").fetchall()
        done = api.execute("SELECT labels->>'outcome', value FROM gxg_metrics() WHERE name = 'gxg_executions_total'")
        outcomes = done.fetchall()
    assert m == [({"tenant": "t-alpha", "tool": "demo.message.propose"}, 1.0)]
    assert outcomes == [("succeeded", 2.0)]  # a deferral is not a finished attempt


def test_ex25_always_429_expires_at_the_deadline_with_no_provider_call(stack: Stack, pg: Any) -> None:
    created = stack.propose(idempotency_key="quota-always")
    i = created["intent_id"]
    ex = mock_executor(stack, limited(retry=1))
    for n in range(1, 6):  # retry_after_s 1 is clamped to 5 s; the test moves the task forward instead of waiting
        assert stack.status(i) == "approved"
        _due_now(stack, i)
        stack.drain(ex, functools.partial(lambda k: stack.status(i) == "approved" and len(stack.executions(i)) == k, n))
    assert stack.intent(i)["attempts"] == 0 and {e[1:] for e in stack.executions(i)} == {("released", "rate_limited")}
    assert len(stack.executions(i)) == 5
    delay = _task_delay(stack, i)
    assert 5 - 1 <= delay <= 6 + 1  # min 5 s, jitter ≤ 20 %
    stack.owner.execute("UPDATE intents SET execution_deadline = now() WHERE intent_id = %s", (i,))
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_sweep(100)")
    assert stack.status(i) == "expired" and stack.demo.calls(i) == []
    with pg.connect("gxg_api") as api:
        m = api.execute("SELECT value FROM gxg_metrics() WHERE name = 'gxg_intents_expired_after_rate_limit'")
        assert m.fetchall() == [(1.0,)]


def test_ex25_no_task_past_the_deadline(stack: Stack) -> None:
    """T12b: if now + delay ≥ execution_deadline no task is queued; T8 closes the intent."""
    created = stack.propose(idempotency_key="quota-late")
    i = created["intent_id"]
    stack.owner.execute(
        "UPDATE intents SET execution_deadline = now() + interval '3 seconds' WHERE intent_id = %s", (i,)
    )
    ex = mock_executor(stack, limited(retry=30))
    stack.drain(ex, lambda: len(stack.executions(i)) > 0 and stack.status(i) == "approved")
    pending = stack.owner.execute(
        "SELECT count(*) FROM outbox WHERE intent_id = %s AND topic = 'execute' AND delivered_at IS NULL", (i,)
    ).fetchone()
    assert pending == (0,)


@pytest.mark.parametrize(
    ("retry", "code"), [(None, "rate_limited"), (0, "rate_limited"), ("5", "rate_limited"), (30, "conflict")]
)
def test_ex25_malformed_429_is_t12a(stack: Stack, retry: Any, code: str) -> None:
    """AS-12a: any other 429 before dispatch (no retry_after_s, another code) is a refusal: the attempt counts."""
    created = stack.propose(idempotency_key=f"quota-bad-{retry}-{code}")
    i = created["intent_id"]
    ex = mock_executor(stack, limited(retry=retry, code=code))
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.intent(i)["attempts"] == 1 and stack.executions(i) == [(1, "failed_before_effect", "http_429")]


def test_ex25_429_after_dispatch_is_outcome_unknown(stack: Stack, pg: Any) -> None:
    """A 429 after a successful dispatch is no deferral (AS-12: any HTTP error after dispatch)."""
    created = stack.propose(idempotency_key="quota-after")
    i = created["intent_id"]

    def dispatched_then_429(_r: httpx.Request) -> httpx.Response:
        with pg.connect("gxg_api") as api:
            e = stack.owner.execute("SELECT execution_id FROM executions WHERE intent_id = %s", (i,)).fetchone()
            assert e is not None
            api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (e[0], i))
        return limited(retry=30)(_r)

    ex = mock_executor(stack, dispatched_then_429)
    r = ex.take()
    assert isinstance(r, Reserved)
    ex.execute(r)
    assert stack.status(i) == "outcome_unknown" and stack.intent(i)["attempts"] == 1


def test_ex25_late_429_after_t10_is_ignored(stack: Stack, pg: Any) -> None:
    """The sweeper released the reservation (T10) first: the late 429 is ignored and audited."""
    created = stack.propose(idempotency_key="quota-t10")
    i = created["intent_id"]
    ex = mock_executor(stack, limited(retry=30))
    r = ex.take()
    assert isinstance(r, Reserved)
    stack.owner.execute("UPDATE executions SET reserve_lease_until = now() - interval '1 second'")
    with pg.connect("gxg_sweeper") as sweeper:
        sweeper.execute("SELECT gxg_sweep(100)")
    ex.execute(r)
    assert stack.executions(i) == [(1, "released", "reserve_lease_expired")] and stack.intent(i)["attempts"] == 0
    audit = stack.owner.execute("SELECT details->>'outcome' FROM audit_events WHERE action = 'complete_ignored'")
    assert audit.fetchall() == [("released",)]


def test_ex25_defer_cas_race_is_ignored(stack: Stack, pg: Any) -> None:
    """T12b CAS: the execution leaves `reserved` between the executor's read and gxg_defer → ignored, audited."""
    created = stack.propose(idempotency_key="quota-race")
    i = created["intent_id"]
    ex = mock_executor(stack, limited(retry=30))
    r = ex.take()
    assert isinstance(r, Reserved)
    real = ex.conn.execute

    def race(sql: Any, *a: Any, **kw: Any) -> Any:
        if str(sql).startswith("SELECT gxg_defer"):
            stack.owner.execute("UPDATE executions SET reserve_lease_until = now() - interval '1 second'")
            with pg.connect("gxg_sweeper") as sweeper:
                sweeper.execute("SELECT gxg_sweep(100)")
        return real(sql, *a, **kw)

    setattr(ex.conn, "execute", race)  # noqa: B010 (mypy: a bound method of psycopg)
    ex.execute(r)
    assert stack.executions(i) == [(1, "released", "reserve_lease_expired")]
    audit = stack.owner.execute("SELECT details->>'outcome' FROM audit_events WHERE action = 'complete_ignored'")
    assert audit.fetchall() == [("rate_limited",)]


def test_ex25_reserve_needs_a_due_task(stack: Stack, pg: Any) -> None:
    """T7 only from an outbox task with available_at ≤ now."""
    created = stack.propose(idempotency_key="quota-task")
    i = created["intent_id"]
    stack.owner.execute("UPDATE outbox SET available_at = now() + interval '1 minute' WHERE intent_id = %s", (i,))
    with pg.connect("gxg_executor") as ex, pytest.raises(psycopg.Error) as err:
        ex.execute("SELECT gxg_reserve(%s, 'x')", (i,))
    assert err.value.sqlstate == "GX409"
