"""Approval Service tests on a real PostgreSQL 16 in Docker, reached over a Unix socket.

The repository forbids TCP in tests (AC-M0-01), so the container's socket directory is bind-mounted.
`GXG_TEST_PG_SOCKET_DIR` points at an already running server instead (scripts/ci/mutmut.sh: one server
for all mutants). Every session gets a fresh database migrated by the owner role; every test starts
from empty tables. Keys are generated at test time.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from gxg_approval import crypto, db, transitions
from gxg_approval.api import Deps, create_app
from gxg_connector_kit import _jwt, canonical, svc_jwt
from gxg_connector_kit.grants import BundleStore

sys.path.insert(0, str(Path(__file__).parent))  # effects_stack (M8), also from mutmut's copy; importlib mode
IMAGE = "postgres:16.6-alpine"
BUNDLE_KID = "ci-test"
ADMIN = "test"  # superuser of the test container; trust auth on the Unix socket
REPO = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())
ROLES = ("gxg_api", "gxg_mcp", "gxg_ui", "gxg_executor", "gxg_sweeper", "gxg_notifier", "gxg_admin")
TABLES = "intents, decisions, executions, outbox, switches, approver_links, audit_events"

API_ID, MCP_ID, EXECUTOR_ID = "approval-api", "approval-mcp", "executor"
ALPHA, BETA = "t-alpha", "t-beta"
BOT, OTHER, SIGNALS = "agent:t-alpha:bot", "agent:t-alpha:other", "agent:t-alpha:signals"
BETA_BOT = "agent:t-beta:bot"
SHARED = "agent:shared"  # the same principal ID registered in both tenants (the schema allows it)
HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
CHAT = "demo:demo-main/board/b1"
TOOLS = [
    {
        "name": "demo.message.propose",
        "version": "1.0.0",
        "effect_class": "external_effect",
        "action": "propose",
        "executes_via": "demo.message.execute",
        "dispatch_timeout_s": 30,
    },
    {
        "name": "demo.note.propose",
        "version": "1.0.0",
        "effect_class": "write_reversible",
        "action": "propose",
        "executes_via": "demo.note.execute",
    },
    {
        "name": "demo.wire.propose",
        "version": "1.0.0",
        "effect_class": "high_risk",
        "action": "propose",
        "executes_via": "demo.wire.execute",
    },
    {"name": "demo.board.search", "version": "1.0.0", "effect_class": "read", "action": "read"},
    {
        "name": "demo.memo.write",
        "version": "1.0.0",
        "effect_class": "write_reversible",
        "action": "write",
        "executes_via": "demo.memo.execute",
    },
    {"name": "demo.orphan.propose", "version": "1.0.0", "effect_class": "external_effect", "action": "propose"},
]


def format_public_key(key: Ed25519PublicKey) -> str:
    return "ed25519:" + _jwt.b64url_encode(_jwt.raw_public_key(key))


def sign_bundle(key: Ed25519PrivateKey, data: bytes) -> bytes:
    """`grants-bundle.json.sig` (GR-11a)."""
    return json.dumps({"alg": "EdDSA", "kid": BUNDLE_KID, "sig": _jwt.b64url_encode(key.sign(data))}).encode()


def _wait_socket(directory: str) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with psycopg.connect(f"host={directory} dbname=postgres user={ADMIN}", connect_timeout=2):
                return
        except psycopg.OperationalError:
            time.sleep(0.3)
    raise RuntimeError("PostgreSQL did not come up")


@pytest.fixture(scope="session")
def pg_socket(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if external := os.environ.get("GXG_TEST_PG_SOCKET_DIR"):
        _wait_socket(external)
        yield external
        return
    import docker  # type: ignore[import-untyped]  # Docker SDK: talks to the daemon over its Unix socket

    directory = tmp_path_factory.mktemp("pgsock")
    directory.chmod(0o777)
    container = docker.from_env().containers.run(
        IMAGE,
        detach=True,
        remove=True,
        environment={"POSTGRES_USER": ADMIN, "POSTGRES_HOST_AUTH_METHOD": "trust"},
        volumes={str(directory): {"bind": "/var/run/postgresql", "mode": "rw"}},
        network_mode="none",  # no TCP at all: the socket is the only way in
    )
    try:
        deadline = time.time() + 120
        while b"PostgreSQL init process complete" not in container.logs() and time.time() < deadline:
            time.sleep(0.2)  # the image's init runs a temporary server first, then restarts
        _wait_socket(str(directory))
        yield str(directory)
    finally:
        container.stop(timeout=5)


@dataclass(frozen=True)
class Pg:
    socket: str
    dbname: str

    def dsn(self, role: str) -> str:
        return f"host={self.socket} dbname={self.dbname} user={role}"

    def connect(self, role: str) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn(role), autocommit=True)


def _admin(socket: str, sql: str) -> None:
    for attempt in range(20):  # roles are cluster-wide: parallel sessions (mutmut) may collide
        try:
            with psycopg.connect(f"host={socket} dbname=postgres user={ADMIN}", autocommit=True) as conn:
                conn.execute(sql)  # static test SQL
            return
        except psycopg.Error:  # e.g. "tuple concurrently updated", template1 in use
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError(f"admin SQL failed: {sql[:60]}")


@pytest.fixture(scope="session")
def pg(pg_socket: str) -> Iterator[Pg]:
    _admin(pg_socket, (REPO / "approval/deploy/roles.sql").read_text())
    for role in ("gxg_owner", *ROLES):
        _admin(pg_socket, f"ALTER ROLE {role} LOGIN")
    name = f"gxg_{uuid.uuid4().hex[:12]}"
    _admin(pg_socket, f"CREATE DATABASE {name} OWNER gxg_owner")
    target = Pg(pg_socket, name)
    db.migrate(target.dsn("gxg_owner"))
    yield target
    _admin(pg_socket, f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture
def owner(pg: Pg) -> Iterator[psycopg.Connection[Any]]:
    """The schema owner (not a process role): test setup and inspection only."""
    with pg.connect("gxg_owner") as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        # The migration installed the same table; re-installing it per test lets mutmut's mutants of
        # gxg_approval.transitions reach the database (it attributes session-fixture calls to one test).
        conn.execute(transitions.install_sql())
        yield conn


def tenant_file(tenant: str, agents: list[str], connection: str) -> dict[str, Any]:
    humans = [{"id": HUMAN1, "roles": ["approver"]}, {"id": HUMAN2, "roles": ["approver"]}] if tenant == ALPHA else []
    return {
        "tenant": tenant,
        "schema_version": 1,
        "connections": [{"id": connection, "domain": "demo", "credential_ref": f"secret://{tenant}/demo"}],
        "principals": {"agents": [{"id": a} for a in agents], "humans": humans},
        "groups": {"bots": [BOT, OTHER]} if tenant == ALPHA else {},
        "grants": [
            {"id": "g1", "subjects": agents, "actions": ["propose"], "resources": [f"demo:{connection}/board/*"]}
        ],
    }


@dataclass
class World:
    """Signed bundle, service keys and payload keys of one test."""

    state_dir: Path
    ci_signer: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    keys: dict[str, Ed25519PrivateKey] = field(default_factory=dict)
    payload_private: dict[str, dict[str, str]] = field(default_factory=dict)
    policies: dict[str, Any] = field(default_factory=dict)
    demo_tenants: list[str] = field(default_factory=lambda: [ALPHA, BETA])

    def __post_init__(self) -> None:
        self.store = BundleStore(
            {BUNDLE_KID: self.ci_signer.public_key()}, state_file=self.state_dir / "bundle-state.json"
        )
        public: dict[str, dict[str, str]] = {}
        for tenant in (ALPHA, BETA):
            public[tenant], self.payload_private[tenant] = {}, {}
            for purpose in ("args", "preview"):
                sk, pk = crypto.generate()
                public[tenant][purpose], self.payload_private[tenant][purpose] = pk, sk
        self.encryptor = crypto.Encryptor(public)
        self.reload()

    def services(self) -> list[dict[str, Any]]:
        spec = [
            (API_ID, "approval_api", ["*"], None),
            (MCP_ID, "approval_mcp", ["*"], None),
            (EXECUTOR_ID, "executor", ["*"], None),
            ("demo", "connector", self.demo_tenants, TOOLS),
            ("approval-mcp-beta", "approval_mcp", [BETA], None),
            ("rogue", "connector", [BETA], TOOLS),
        ]
        out = []
        for sid, kind, tenants, tools in spec:
            key = self.keys.setdefault(sid, Ed25519PrivateKey.generate())
            entry: dict[str, Any] = {
                "id": sid,
                "kind": kind,
                "public_keys": [format_public_key(key.public_key())],
                "tenants": tenants,
            }
            if kind == "connector":
                entry["internal_url"] = f"https://{sid}.internal:8081"
                entry["tools"] = tools
            out.append(entry)
        return out

    def reload(self, version: str = "b" * 40) -> None:
        doc = {
            "schema_version": 1,
            "bundle_version": version,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tenants": {
                ALPHA: {
                    "grants": tenant_file(ALPHA, [BOT, OTHER, SIGNALS, SHARED], "demo-main"),
                    "approval_policy": self.policies.get(ALPHA),
                },
                BETA: {
                    "grants": tenant_file(BETA, [BETA_BOT, SHARED], "demo-b"),
                    "approval_policy": self.policies.get(BETA),
                },
            },
            "platform": {"schema_version": 1, "services": self.services()},
        }
        data = canonical.dumps(doc)
        self.signed = data, sign_bundle(self.ci_signer, data)
        self.store.load(*self.signed)

    def bundle_keys(self, signer: Ed25519PrivateKey | None = None) -> str:
        """`GXG_BUNDLE_KEYS` for this bundle (or for another key)."""
        return json.dumps({BUNDLE_KID: format_public_key((signer or self.ci_signer).public_key())})

    def set_policy(self, tenant: str, rules: list[dict[str, Any]], **extra: Any) -> None:
        self.policies[tenant] = {"tenant": tenant, "schema_version": 1, "rules": rules, **extra}
        self.reload()

    def token(self, service: str, aud: str = API_ID, signer: Ed25519PrivateKey | None = None) -> str:
        return svc_jwt.sign(signer or self.keys[service], iss=service, aud=aud)

    def auth(self, service: str = "demo", **kw: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(service, **kw)}"}

    def body(
        self,
        *,
        tool: str = "demo.message.propose",
        tenant: str = ALPHA,
        requester: str = BOT,
        args: dict[str, Any] | None = None,
        key: str | None = None,
        resource: str = CHAT,
        connection: str = "demo-main",
        **extra: Any,
    ) -> dict[str, Any]:
        args = {"text": "hello partner", "n": 1} if args is None else args
        h = canonical.hash(
            {
                "tool_key": tool,
                "tool_version": "1.0.0",
                "tenant_id": tenant,
                "connection_id": connection,
                "resource_id": resource,
                "args": args,
            }
        )
        doc = {
            "tool_key": tool,
            "tool_version": "1.0.0",
            "connection_id": connection,
            "resource_id": resource,
            "canonical_args": args,
            "canonical_args_hash": h,
            "idempotency_key": key or f"key-{uuid.uuid4()}",
            "preview": {
                "schema_version": 1,
                "title": "Send a message",
                "target": {"label": "Board b1", "rid": resource},
                "fields": [{"label": "Text", "value": str(args.get("text", "")), "kind": "multiline"}],
            },
            "requester": {"principal": requester, "tenant_id": tenant},
        }
        doc.update(extra)
        return doc

    def deps(self, pool: ConnectionPool[Any]) -> Deps:
        return Deps(API_ID, pool, self.store, self.encryptor, lambda: {"keys": []})


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def api_pool(pg: Pg) -> Iterator[ConnectionPool[Any]]:
    with ConnectionPool(pg.dsn("gxg_api"), min_size=1, max_size=20, kwargs={"autocommit": True}) as pool:
        yield pool


@pytest.fixture
def client(world: World, api_pool: ConnectionPool[Any], owner: psycopg.Connection[Any]) -> Iterator[TestClient]:
    with TestClient(create_app(world.deps(api_pool)), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def propose(client: TestClient, world: World) -> Callable[..., Any]:
    def call(**kw: Any) -> Any:
        return client.post("/v1/intents", json=world.body(**kw), headers=world.auth())

    return call


def _jwks(*keys: Ed25519PrivateKey) -> dict[str, Any]:
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _jwt.b64url_encode(_jwt.raw_public_key(k.public_key())),
                "kid": _jwt.thumbprint(k.public_key()),
                "use": "sig",
                "alg": "EdDSA",
            }
            for k in keys
        ]
    }


@pytest.fixture
def make_jwks() -> Callable[..., dict[str, Any]]:
    """JWKS document (AS-09) of the given executor keys."""
    return _jwks
