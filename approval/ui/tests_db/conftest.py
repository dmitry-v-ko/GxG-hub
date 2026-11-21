"""ui tests on a real PostgreSQL: the Approval Service fixtures (approval/tests_db/conftest.py) plus a signed-in
browser for the ui app. The session cookie is minted with the session key (the OIDC flow itself is covered by the
Playwright E2E); everything after it is the production code path."""

from __future__ import annotations

import base64
import importlib.util
import json
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from nacl.public import PrivateKey, SealedBox
from psycopg_pool import ConnectionPool

ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())
if "approval_db_fixtures" not in sys.modules:  # one copy per session (the notifier tests load it too)
    _spec = importlib.util.spec_from_file_location("approval_db_fixtures", ROOT / "approval/tests_db/conftest.py")
    assert _spec and _spec.loader
    sys.modules["approval_db_fixtures"] = _base = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_base)
from approval_db_fixtures import *  # type: ignore[import-not-found]  # noqa: E402, F403  (pg, owner, world, propose, …)
from approval_db_fixtures import Pg, World  # noqa: E402

from gxg_approval_ui.app import SESSION_COOKIE, Deps, create_app  # noqa: E402 (after the *: it has another Deps)

PUBLIC_URL = "https://ui.example.test"


@dataclass
class Clock:
    now: float

    def __call__(self) -> float:
        return self.now


@dataclass
class Browser:
    """A signed-in human: TestClient with the `__Host-` session cookie (HTTPS base URL, Secure cookies)."""

    client: TestClient
    key: str
    clock: Clock
    session: dict[str, Any] = field(default_factory=dict)

    def login(self, human: str) -> Browser:
        now = self.clock()
        self.session = {"sub": human, "iat": now, "seen": now, "csrf": secrets.token_urlsafe(32)}
        data = base64.b64encode(json.dumps(self.session).encode())
        self.client.cookies.clear()  # the server's own Set-Cookie would shadow the new session
        self.client.cookies.set(
            SESSION_COOKIE, TimestampSigner(self.key).sign(data).decode(), domain="testserver.local"
        )
        return self

    @property
    def csrf(self) -> str:
        return str(self.session["csrf"])

    def decide(self, intent: dict[str, Any], decision: str = "approve", **form: Any) -> Any:
        body = {"csrf": self.csrf, "version": intent["version"], "args_hash": intent["canonical_args_hash"],
                "decision": decision} | form  # fmt: skip
        return self.client.post(f"/intents/{intent['intent_id']}/decision", data=body, follow_redirects=False)


@pytest.fixture
def clock() -> Clock:
    import time

    return Clock(time.time())


@pytest.fixture
def ui_pool(pg: Pg) -> Iterator[ConnectionPool[Any]]:
    with ConnectionPool(pg.dsn("gxg_ui"), min_size=1, max_size=5, kwargs={"autocommit": True}) as pool:
        yield pool


@pytest.fixture
def ui_deps(world: World, ui_pool: ConnectionPool[Any], clock: Clock) -> Deps:
    keys = {t: SealedBox(PrivateKey(base64.b64decode(k["preview"]))) for t, k in world.payload_private.items()}
    return Deps(
        pool=ui_pool, bundles=world.store, preview_keys=keys, session_key=secrets.token_urlsafe(32),
        public_url=PUBLIC_URL, oidc_issuer="https://idp.example.invalid", oidc_client_id="ui",
        oidc_client_secret="test-only", clock=clock,
    )  # fmt: skip


@pytest.fixture
def browser(ui_deps: Deps, owner: psycopg.Connection[Any], clock: Clock) -> Iterator[Browser]:
    with TestClient(create_app(ui_deps), base_url="https://testserver", raise_server_exceptions=False) as client:
        yield Browser(client, ui_deps.session_key, clock)


@pytest.fixture
def make_browser(ui_deps: Deps, clock: Clock) -> Iterator[Any]:
    """Another browser (another session) on the same app."""
    app = create_app(ui_deps)
    opened: list[TestClient] = []

    def make() -> Browser:
        c = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        opened.append(c.__enter__())
        return Browser(c, ui_deps.session_key, clock)

    yield make
    for c in opened:
        c.__exit__(None, None, None)


@pytest.fixture
def row_of(owner: psycopg.Connection[Any]) -> Any:
    def get(intent_id: str) -> dict[str, Any]:
        cur = owner.cursor(row_factory=psycopg.rows.dict_row)
        row = cur.execute("SELECT * FROM intents WHERE intent_id = %s", (intent_id,)).fetchone()
        assert row is not None
        return dict(row)

    return get


@pytest.fixture
def execute(pg: Pg) -> Any:
    return lambda intent_id, outcome="succeeded": run_execution(pg, intent_id, outcome)


def run_execution(pg: Pg, intent_id: str, outcome: str = "succeeded") -> None:
    """Executor transitions T7, T9, T11/T13 through the real functions of their roles (M8's executor is not
    on this branch): reserve as gxg_executor, dispatch as gxg_api for the connector, complete as gxg_executor."""
    with pg.connect("gxg_executor") as ex, pg.connect("gxg_api") as api:
        execution = ex.execute("SELECT gxg_reserve(%s, 'test-executor')", (intent_id,)).fetchone()
        assert execution is not None
        api.execute("SELECT gxg_dispatch(%s, %s, 'demo', 30)", (execution[0], intent_id))
        ex.execute("SELECT gxg_complete(%s, 'test-executor', %s, 'op-1', 'receipt-1', null)", (execution[0], outcome))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class SoftAuthenticator:
    """A software passkey (ES256, attestation `none`) producing the JSON the browser's `toJSON()` sends."""

    def __init__(self, rp_id: str = "ui.example.test", origin: str = PUBLIC_URL) -> None:
        import os

        from cryptography.hazmat.primitives.asymmetric import ec

        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.count = 0

    def _client_data(self, kind: str, options_json: str, origin: str | None) -> bytes:
        challenge = json.loads(options_json)["challenge"]
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin}).encode()

    def register(self, options_json: str) -> str:
        import hashlib

        import cbor2

        numbers = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        auth = (
            hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + self.count.to_bytes(4, "big")
            + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cbor2.dumps(cose)
        )  # fmt: skip
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        client = self._client_data("webauthn.create", options_json, None)
        response = {"clientDataJSON": _b64(client), "attestationObject": _b64(attestation)}
        cid = _b64(self.credential_id)
        return json.dumps({"id": cid, "rawId": cid, "type": "public-key", "response": response})

    def assertion(self, options_json: str, origin: str | None = None) -> str:
        import hashlib

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.count += 1
        auth = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + self.count.to_bytes(4, "big")
        client = self._client_data("webauthn.get", options_json, origin)
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return json.dumps({"id": _b64(self.credential_id), "rawId": _b64(self.credential_id), "type": "public-key",
                           "response": {"clientDataJSON": _b64(client), "authenticatorData": _b64(auth),
                                        "signature": _b64(signature)}})  # fmt: skip


@pytest.fixture
def soft_authenticator() -> type[SoftAuthenticator]:
    return SoftAuthenticator


@pytest.fixture
def passkey() -> Any:
    """`passkey(browser)`: register a soft passkey for the signed-in human; returns the authenticator."""

    def make(b: Browser) -> SoftAuthenticator:
        authenticator = SoftAuthenticator()
        options = b.client.get("/passkeys/options").text
        r = b.client.post("/passkeys", content=authenticator.register(options), headers={"X-CSRF-Token": b.csrf})
        assert r.json() == {"registered": True}, r.text
        return authenticator

    return make
