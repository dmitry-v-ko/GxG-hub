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
from webauthn_soft import SoftAuthenticator  # noqa: E402 (approval/tests_db, on sys.path from its conftest)

from gxg_approval_ui.app import SESSION_COOKIE, Deps, create_app  # noqa: E402 (after the *: it has another Deps)

PUBLIC_URL = "https://ui.example.test"


@dataclass
class Clock:
    now: float

    def __call__(self) -> float:
        return self.now


@dataclass
class Browser:
    """A signed-in human: TestClient with the `__Host-` session cookie (HTTPS base URL, Secure cookies).

    `decide` signs every decision (AS-30) with the human's soft passkey, enrolling one through an operator invite
    on first use; `sign=False` sends the form as it is."""

    client: TestClient
    key: str
    clock: Clock
    invite: Any  # human → one-time invite token (gxg_passkey_invite as gxg_admin)
    passkeys: dict[str, Any]  # human → SoftAuthenticator, shared by the browsers of one test
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

    @property
    def human(self) -> str:
        return str(self.session["sub"])

    def enroll(self, authenticator: Any = None) -> Any:
        """Register a soft passkey for the signed-in human through an operator invite (AS-30)."""
        key = authenticator or SoftAuthenticator()
        invite = self.invite(self.human)
        headers = {"X-CSRF-Token": self.csrf}
        options = self.client.post("/passkeys/options", json={"invite": invite}, headers=headers)
        assert options.status_code == 200, options.text
        credential = json.loads(key.register(options.text))
        r = self.client.post("/passkeys", json={"invite": invite, "credential": credential}, headers=headers)
        assert r.json() == {"registered": True}, r.text
        self.passkeys[self.human] = key
        return key

    def challenge(self, intent: dict[str, Any], decision: str = "approve", **form: Any) -> Any:
        body = {"version": intent["version"], "args_hash": intent["canonical_args_hash"], "decision": decision} | form
        return self.client.post(
            f"/intents/{intent['intent_id']}/challenge", data=body, headers={"X-CSRF-Token": self.csrf}
        )

    def decide(self, intent: dict[str, Any], decision: str = "approve", sign: bool = True, **form: Any) -> Any:
        body = {"csrf": self.csrf, "version": intent["version"], "args_hash": intent["canonical_args_hash"],
                "decision": decision} | form  # fmt: skip
        if sign and "assertion" not in form:
            key = self.passkeys.get(self.human) or self.enroll()
            options = self.challenge(intent, decision, version=body["version"], args_hash=body["args_hash"])
            if options.status_code != 200:
                return options  # the refusal of the challenge (stale screen, not an approver, …)
            body["assertion"] = key.assertion(options.text)
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
def decider_client(pg: Pg) -> Iterator[TestClient]:
    """The decider process (AS-30) in-process, with its own role; the ui's `GXG_DECIDER_URL` in tests."""
    from gxg_approval import decider

    with ConnectionPool(pg.dsn("gxg_decider"), min_size=1, max_size=3, kwargs={"autocommit": True}) as pool:
        app = decider.create_app(decider.Deps(pool, rp_id="ui.example.test", origin=PUBLIC_URL))
        with TestClient(app, base_url="http://decider.internal", raise_server_exceptions=False) as client:
            yield client


@pytest.fixture
def ui_deps(world: World, ui_pool: ConnectionPool[Any], clock: Clock, decider_client: TestClient) -> Deps:
    keys = {t: SealedBox(PrivateKey(base64.b64decode(k["preview"]))) for t, k in world.payload_private.items()}
    return Deps(
        pool=ui_pool, bundles=world.store, preview_keys=keys, session_key=secrets.token_urlsafe(32),
        public_url=PUBLIC_URL, oidc_issuer="https://idp.example.invalid", oidc_client_id="ui",
        oidc_client_secret="test-only", decider=decider_client, clock=clock,
    )  # fmt: skip


@pytest.fixture
def invite(pg: Pg) -> Any:
    """`invite(human)`: a one-time passkey invite, as the admin process issues it (AS-30)."""
    from gxg_approval.decider import invite_hash

    def make(human: str) -> str:
        token = secrets.token_urlsafe(24)
        with pg.connect("gxg_admin") as admin:
            admin.execute(
                "SELECT gxg_passkey_invite(%s, %s, 'oidc:https://idp.example.invalid:operator', 600)",
                (human, invite_hash(token)),
            )
        return token

    return make


@pytest.fixture
def passkeys() -> dict[str, Any]:
    return {}


@pytest.fixture
def browser(
    ui_deps: Deps, owner: psycopg.Connection[Any], clock: Clock, invite: Any, passkeys: dict[str, Any]
) -> Iterator[Browser]:
    with TestClient(create_app(ui_deps), base_url="https://testserver", raise_server_exceptions=False) as client:
        yield Browser(client, ui_deps.session_key, clock, invite, passkeys)


@pytest.fixture
def make_browser(ui_deps: Deps, clock: Clock, invite: Any, passkeys: dict[str, Any]) -> Iterator[Any]:
    """Another browser (another session) on the same app."""
    app = create_app(ui_deps)
    opened: list[TestClient] = []

    def make() -> Browser:
        c = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
        opened.append(c.__enter__())
        return Browser(c, ui_deps.session_key, clock, invite, passkeys)

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


@pytest.fixture
def soft_authenticator() -> type[SoftAuthenticator]:
    return SoftAuthenticator


@pytest.fixture
def passkey() -> Any:
    """`passkey(browser)`: register a soft passkey for the signed-in human; returns the authenticator."""

    def make(b: Browser) -> SoftAuthenticator:
        return b.enroll()  # type: ignore[no-any-return]

    return make
