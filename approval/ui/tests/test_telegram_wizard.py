"""The operator wizard «Подключение Telegram» (M13) against a fake Telegram admin API, no database.

The fake API verifies the service JWT like the real one (iss `approval-ui`, aud `telegram`, key from the registry) and
keeps the login state machine of the contract; the session cookie is minted with the session key (the OIDC flow
itself is covered by the E2E)."""

from __future__ import annotations

import base64
import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from gxg_approval_ui import app as ui_app
from gxg_approval_ui import config, tgadmin
from gxg_connector_kit import _jwt
from gxg_connector_kit.svc_jwt import Service, SvcJwtVerifier

NOW = 100.0
OPERATOR = "oidc:https://idp.example.test:owner-1"
OPERATOR_EMAIL = "owner@example.test"
PHONE, CODE, PASSWORD = "+79001234567", "48151", "hunter2-2fa-pass"
CHATS = [
    {"chat_ref": "c:alpha", "title": "Команда", "kind": "group"},
    {"chat_ref": "c:beta", "title": "Семья", "kind": "group"},
    {"chat_ref": "c:gamma", "title": "Новости", "kind": "channel"},
    {"chat_ref": "c:delta", "title": "Друг", "kind": "private"},
]


class FakeTelegram:
    """The contract's endpoints; `fail` makes the next call to a path answer with (status, body)."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        pub = key.public_key()
        registry = {"approval-ui": Service("approval-ui", "ui", (pub,), frozenset({"*"}))}
        self.verifier = SvcJwtVerifier("telegram", lambda: registry, now=lambda: NOW)
        self.state: dict[str, Any] = {"authorized": False, "login": "idle", "sync": "idle", "two_fa": True}
        self.scope: list[str] = []
        self.calls: list[tuple[str, str, str, Any]] = []  # method, path, operator, body
        self.fail: dict[str, tuple[int, Any]] = {}
        self.chats: list[dict[str, Any]] = CHATS
        self.raw_scope: Any = None  # the answer of GET scope as it is, when set (a bare list is allowed too)
        self.verbs: list[tuple[str, str]] = []  # the method exactly as the ui spelled it, path
        self.down = False
        app = FastAPI()
        app.add_api_route("/internal/v1/admin/tg-pilot/{rest:path}", self.handle, methods=["GET", "POST", "PUT"])
        self.app = app

    async def handle(self, request: Request, rest: str) -> JSONResponse:  # noqa: PLR0911 (one branch per endpoint)
        self.verifier.verify(request.headers["authorization"].removeprefix("Bearer "), allowed_issuers={"approval-ui"})
        operator = request.headers["x-gxg-operator"]
        raw = await request.body()
        body: Any = json.loads(raw) if raw else None
        self.calls.append((request.method, rest, operator, body))
        if rest in self.fail:
            status, doc = self.fail.pop(rest)
            return JSONResponse(doc, status_code=status)
        st = self.state
        if rest == "status":
            return JSONResponse({k: st[k] for k in ("authorized", "login", "sync")})
        if rest == "login/start":
            st["login"] = "code_sent"
            return JSONResponse({"login": "code_sent"})
        if rest == "login/code":
            if body["code"] != CODE:
                return JSONResponse({"message": f"код {body['code']} неверный"}, status_code=400)
            if st["two_fa"]:
                st["login"] = "password_required"
                return JSONResponse({"login": "password_required"})
            st.update(authorized=True, login="idle")
            return JSONResponse({"authorized": True})
        if rest == "login/password":
            st.update(authorized=True, login="idle")
            return JSONResponse({"authorized": True})
        if rest == "login/cancel":
            st["login"] = "idle"
            return JSONResponse({})
        if rest == "chats":
            return JSONResponse(self.chats)
        if rest == "scope" and request.method == "PUT":
            self.scope = list(body["chat_refs"])
            return JSONResponse({"chat_refs": self.scope})
        if rest == "scope":
            return JSONResponse({"chat_refs": self.scope} if self.raw_scope is None else self.raw_scope)
        return JSONResponse({"message": "нет такого"}, status_code=404)


class Pool:
    """No database: the pending list of the home page is empty."""

    @contextmanager
    def connection(self, **_: Any) -> Iterator[Any]:
        cursor: Any = SimpleNamespace(fetchall=lambda: [])
        cursor.execute = lambda *a, **k: cursor
        yield SimpleNamespace(cursor=lambda **k: cursor)


class Transport:
    """tgadmin.Client over the fake API's TestClient; `down` simulates a refused connection."""

    def __init__(self, fake: FakeTelegram, client: TestClient) -> None:
        self.fake, self.client = fake, client

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any, timeout: float) -> Any:
        if self.fake.down:
            raise ConnectionError("refused")
        self.fake.verbs.append((method, url.removeprefix("/internal/v1/admin/tg-pilot/")))
        return self.client.request(method, url, headers=headers, json=json)


class Ui:
    def __init__(self, client: TestClient, session_key: str, fake: FakeTelegram) -> None:
        self.client, self.key, self.fake = client, session_key, fake
        self.csrf = ""

    def login(self, sub: str = OPERATOR, email: str | None = OPERATOR_EMAIL, **extra: Any) -> Ui:
        self.csrf = secrets.token_urlsafe(32)
        session: dict[str, Any] = {"sub": sub, "iat": 1.0, "seen": 1.0, "csrf": self.csrf} | extra
        if email is not None:
            session["email"] = email
        data = base64.b64encode(json.dumps(session).encode())
        self.client.cookies.clear()
        self.client.cookies.set(
            ui_app.SESSION_COOKIE, TimestampSigner(self.key).sign(data).decode(), domain="testserver.local"
        )
        return self

    def post(self, step: str, *, csrf: str | None = None, **form: Any) -> Any:
        body: dict[str, Any] = {"csrf": self.csrf if csrf is None else csrf} | form
        return self.client.post(f"/operator/telegram/{step}", data=body, follow_redirects=False)

    def step(self, step: str, **form: Any) -> str:
        """A successful POST (303 to the wizard) and the page after it."""
        r = self.post(step, **form)
        assert r.status_code == 303 and r.headers["location"] == "/operator/telegram", r.text
        page = self.client.get("/operator/telegram")
        assert page.status_code == 200
        return str(page.text)


@pytest.fixture
def svc_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def fake(svc_key: Ed25519PrivateKey) -> FakeTelegram:
    return FakeTelegram(svc_key)  # verifies with the ui's public key


@pytest.fixture
def ui(fake: FakeTelegram, svc_key: Ed25519PrivateKey) -> Iterator[Ui]:
    now = NOW  # one clock: the session and the JWTs
    api_client = TestClient(fake.app, base_url="http://telegram.internal")
    admin = tgadmin.TelegramAdmin(Transport(fake, api_client), svc_key, clock=lambda: now)
    session_key = secrets.token_urlsafe(32)
    deps = ui_app.Deps(
        pool=cast(Any, Pool()),
        bundles=cast(Any, SimpleNamespace(current=SimpleNamespace(tenants={}))),
        preview_keys={},
        session_key=session_key,
        public_url="https://ui.example.test",
        oidc_issuer="https://idp.example.invalid",
        oidc_client_id="ui",
        oidc_client_secret="test-only",  # noqa: S106
        decider=cast(Any, None),
        clock=lambda: now,
        operators=config.operators(f" {OPERATOR_EMAIL.upper()} , other@example.test,"),
        telegram_admin=admin,
    )
    with TestClient(ui_app.create_app(deps), base_url="https://testserver", raise_server_exceptions=False) as client:
        yield Ui(client, session_key, fake).login()


def test_the_whole_wizard(ui: Ui, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    typed = (PHONE, CODE, PASSWORD)
    pages = [ui.client.get("/operator/telegram").text]
    assert 'name="phone"' in pages[0] and "Аккаунт не подключён" in pages[0]
    pages.append(ui.step("phone", phone="+7 (900) 123-45-67"))  # formatting is stripped
    assert 'name="code"' in pages[1] and 'name="phone"' not in pages[1]
    pages.append(ui.step("code", code=CODE))
    assert '<input name="password" type="password" autocomplete="off" required>' in pages[2]
    pages.append(ui.step("password", password=PASSWORD))
    assert "Готово: аккаунт подключён" in pages[3] and 'name="chat"' in pages[3]
    assert pages[3].count('type="checkbox"') == len(CHATS) and "checked" not in pages[3].replace("checkbox", "")
    pages.append(ui.step("scope", chat=["c:beta", "c:delta"]))
    assert 'value="c:beta" checked' in pages[4] and 'value="c:alpha" checked' not in pages[4]
    assert '<span id="chosen"><code>c:beta</code>, <code>c:delta</code></span>' in pages[4]
    assert ui.fake.scope == ["c:beta", "c:delta"]
    # the typed secrets: only ever in the one request to the API; not in any page, any log record
    for secret in typed:
        assert [i for i, p in enumerate(pages) if secret in p] == [] and secret not in caplog.text
    sent = [(c[1], c[3]) for c in ui.fake.calls if c[0] != "GET"]
    assert sent == [
        ("login/start", {"phone": PHONE}), ("login/code", {"code": CODE}),
        ("login/password", {"password": PASSWORD}), ("scope", {"chat_refs": ["c:beta", "c:delta"]}),
    ]  # fmt: skip
    assert {c[2] for c in ui.fake.calls} == {
        OPERATOR
    }  # X-GxG-Operator on every call (the JWT was verified by the fake)


def test_cancel_starts_over_and_without_2fa_the_code_finishes(ui: Ui) -> None:
    ui.step("phone", phone=PHONE)
    assert 'name="phone"' in ui.step("cancel")
    ui.fake.state["two_fa"] = False
    ui.step("phone", phone=PHONE)
    assert "Готово: аккаунт подключён" in ui.step("code", code=CODE)


@pytest.mark.parametrize("email", [None, "intruder@example.test"])
def test_everyone_else_gets_404(ui: Ui, email: str | None) -> None:
    ui.login(email=email)
    assert ui.client.get("/operator/telegram").status_code == 404
    for step in ("phone", "code", "password", "cancel", "scope"):
        assert ui.post(step, phone=PHONE, code=CODE, password=PASSWORD).status_code == 404
    assert ui.fake.calls == []
    assert "Подключение Telegram" not in ui.client.get("/").text  # no link either


def test_an_email_the_idp_did_not_confirm_is_nobody(ui: Ui) -> None:
    """`app.complete` stores the email only for `email_verified: true`; a session without it is not an operator."""
    ui.login(email=None)
    assert ui.client.get("/operator/telegram").status_code == 404


def test_the_site_cannot_be_reached_without_a_session(ui: Ui) -> None:
    ui.client.cookies.clear()
    assert ui.client.get("/operator/telegram").status_code == 403
    assert ui.client.post("/operator/telegram/phone", data={"phone": PHONE}).status_code == 403
    assert ui.fake.calls == []


@pytest.mark.parametrize("csrf", ["", "wrong"])
def test_forms_need_the_csrf_token(ui: Ui, csrf: str) -> None:
    for step in ("phone", "code", "password", "cancel", "scope"):
        r = ui.post(step, csrf=csrf, phone=PHONE, code=CODE, password=PASSWORD, chat=["c:alpha"])
        assert r.status_code == 403 and ui_app.STALE_FORM[1] in r.text
    assert ui.fake.calls == []


def test_the_token_is_in_every_form_and_the_password_field_is_closed(ui: Ui) -> None:
    ui.step("phone", phone=PHONE)
    ui.step("code", code=CODE)
    page = ui.client.get("/operator/telegram").text
    assert f'name="csrf" value="{ui.csrf}"' in page and page.count('name="csrf"') == 3  # logout, password form, cancel


def test_an_api_refusal_is_explained_and_the_typed_code_is_not_echoed(ui: Ui, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    ui.step("phone", phone=PHONE)
    r = ui.post("code", code="99999")  # the fake says "код 99999 неверный"
    assert r.status_code == 400 and 'id="error"' in r.text and "неверный" in r.text
    assert "99999" not in r.text and "99999" not in caplog.text
    assert "код … неверный" in r.text  # the typed value is replaced, the rest of the text stays
    assert 'name="code"' in r.text  # the person stays on the step and can retry


def test_the_api_down_is_a_plain_message(ui: Ui) -> None:
    ui.fake.down = True
    r = ui.client.get("/operator/telegram")
    assert r.status_code == 502 and tgadmin_down(r.text) and "Повторить" in r.text
    r = ui.post("phone", phone=PHONE)
    assert r.status_code == 502 and tgadmin_down(r.text)


def tgadmin_down(text: str) -> bool:
    return ui_app.TG_DOWN in text


def test_a_server_error_text_and_a_textless_error(ui: Ui) -> None:
    ui.fake.fail["status"] = (500, {"message": "telethon: FLOOD_WAIT 30"})
    assert "FLOOD_WAIT 30" in ui.client.get("/operator/telegram").text
    ui.fake.fail["status"] = (401, "nope")
    r = ui.client.get("/operator/telegram")
    assert r.status_code == 400
    assert "не принял подпись" in r.text


def test_invalid_input_is_refused_before_the_api(ui: Ui) -> None:
    for r, text in (
        (ui.post("phone", phone="hello"), ui_app.BAD_PHONE),
        (ui.post("code", code="12ab"), ui_app.BAD_CODE),
        (ui.post("password", password=""), ui_app.NEED_2FA),
        (ui.post("password", password="x" * 257), ui_app.NEED_2FA),
        (ui.post("password", password="x" * 300), ui_app.NEED_2FA),
    ):
        assert r.status_code == 400 and text in r.text
    assert [c for c in ui.fake.calls if c[0] != "GET"] == []


def test_scope_is_at_most_three_known_chats(ui: Ui) -> None:
    ui.fake.state.update(authorized=True)
    r = ui.post("scope", chat=["c:alpha", "c:beta", "c:gamma", "c:delta"])
    assert r.status_code == 400 and ui_app.TOO_MANY_CHATS in r.text
    r = ui.post("scope", chat=["c:alpha", "c:forged"])
    assert r.status_code == 400 and ui_app.UNKNOWN_CHAT in r.text
    assert ui.fake.scope == [] and not [c for c in ui.fake.calls if c[0] == "PUT"]
    ui.step("scope", chat=["c:alpha", "c:alpha", "c:beta"])  # a repeat is one chat
    assert ui.fake.scope == ["c:alpha", "c:beta"]
    ui.step("scope")  # nothing ticked: the pilot reads nothing
    assert ui.fake.scope == []


def test_an_unknown_step_is_404(ui: Ui) -> None:
    assert ui.post("format-disk").status_code == 404


def test_without_the_api_url_the_page_says_so(ui: Ui) -> None:
    app = cast(Any, ui.client.app)
    del app  # the closure holds deps; build another app without the admin client
    deps = ui_app.Deps(
        pool=cast(Any, Pool()), bundles=cast(Any, None), preview_keys={}, session_key=ui.key,
        public_url="https://ui.example.test", oidc_issuer="https://i", oidc_client_id="ui",
        oidc_client_secret="x",  # noqa: S106
        decider=cast(Any, None), clock=lambda: 100.0,
        operators=frozenset({OPERATOR_EMAIL}),
    )  # fmt: skip
    with TestClient(ui_app.create_app(deps), base_url="https://testserver", raise_server_exceptions=False) as c:
        other = Ui(c, ui.key, ui.fake).login()
        r = c.get("/operator/telegram")
        assert r.status_code == 503 and "GXG_TELEGRAM_ADMIN_URL" in r.text
        assert other.post("phone", phone=PHONE).status_code == 503


def test_home_of_an_operator_without_intents_has_the_links(ui: Ui) -> None:
    page = ui.client.get("/").text
    assert 'id="empty"' in page
    assert 'id="link-telegram" href="/operator/telegram"' in page and 'id="link-passkeys" href="/passkeys"' in page
    ui.login(email="intruder@example.test")
    assert "operator-links" not in ui.client.get("/").text


def test_operators_variable() -> None:
    assert config.operators("") == frozenset()
    assert config.operators(" A@x.test, ,b@X.test ") == frozenset({"a@x.test", "b@x.test"})


def test_the_service_jwt_is_short_and_addressed_to_telegram() -> None:
    key = Ed25519PrivateKey.generate()
    seen: dict[str, Any] = {}

    class Spy:
        def request(self, method: str, url: str, *, headers: dict[str, str], json: Any, timeout: float) -> Any:
            seen.update(headers=headers, url=url)
            return SimpleNamespace(status_code=200, json=lambda: {"authorized": False})

    tgadmin.TelegramAdmin(Spy(), key, clock=lambda: 1000.0).call("GET", "status", OPERATOR)
    token = seen["headers"]["Authorization"].removeprefix("Bearer ")
    claims = json.loads(_jwt.b64url_decode(token.split(".")[1]))
    assert (claims["iss"], claims["aud"], claims["exp"] - claims["iat"]) == ("approval-ui", "telegram", 60)
    assert seen["headers"]["X-GxG-Operator"] == OPERATOR
    assert seen["url"] == "/internal/v1/admin/tg-pilot/status"


def test_session_lives_thirty_days_without_an_idle_timeout(ui: Ui) -> None:
    """Owner decision 2026-11-24 (approval-service «Вход»): 30 days from sign-in, no idle limit, persistent cookie."""
    day = 24 * 3600
    assert ui_app.SESSION_MAX_S == 30 * day and not hasattr(ui_app, "SESSION_IDLE_S")
    assert ui_app.SESSION_COOKIE_FLAGS["max_age"] == 30 * day  # Max-Age: the cookie survives the browser
    assert (
        ui.login(iat=NOW - 29 * day, seen=1.0).client.get("/operator/telegram").status_code == 200
    )  # `seen` is long gone
    assert ui.login(iat=NOW - 31 * day).client.get("/operator/telegram").status_code == 403


def test_the_api_is_called_with_exact_methods(ui: Ui) -> None:
    """The service reads the method as it is sent (`POST`, not `post`): every call of the wizard, spelled out."""
    ui.step("phone", phone=PHONE)
    ui.step("cancel")
    ui.step("phone", phone=PHONE)
    ui.step("code", code=CODE)
    ui.step("password", password=PASSWORD)
    ui.step("scope", chat=["c:beta"])
    assert set(ui.fake.verbs) == {
        ("POST", "login/start"), ("POST", "login/code"), ("POST", "login/password"), ("POST", "login/cancel"),
        ("PUT", "scope"), ("GET", "status"), ("GET", "scope"), ("GET", "chats"),
    }  # fmt: skip


@pytest.mark.parametrize(("api", "shown"), [(400, 400), (499, 400), (500, 502), (501, 502), (503, 502)])
def test_an_api_error_is_a_client_error_unless_it_is_the_services_fault(ui: Ui, api: int, shown: int) -> None:
    ui.fake.fail["status"] = (api, {"message": "text of the service"})
    r = ui.client.get("/operator/telegram")
    assert r.status_code == shown and "text of the service" in r.text


def test_the_authorized_page_shows_sync_titles_and_kinds(ui: Ui) -> None:
    ui.fake.state.update(authorized=True, sync="syncing")
    page = ui.client.get("/operator/telegram").text
    assert "Синхронизация: syncing." in page
    assert "Команда <small>(group)</small>" in page and "Новости <small>(channel)</small>" in page
    ui.fake.chats = [{"chat_ref": "c:bare"}]  # no title, no kind: empty, not «None»
    assert 'value="c:bare">  <small>()</small>' in ui.client.get("/operator/telegram").text


@pytest.mark.parametrize(("raw", "chosen"), [(["c:beta"], "<code>c:beta</code>"), ({}, "ничего")])
def test_the_saved_scope_may_be_a_bare_list_or_empty(ui: Ui, raw: Any, chosen: str) -> None:
    ui.fake.state.update(authorized=True)
    ui.fake.raw_scope = raw
    r = ui.client.get("/operator/telegram")
    assert r.status_code == 200 and f'<span id="chosen">{chosen}</span>' in r.text


def test_an_unknown_login_state_is_the_first_step(ui: Ui) -> None:
    ui.fake.state["login"] = "something-new"
    assert 'name="phone"' in ui.client.get("/operator/telegram").text


def test_the_limits_are_inclusive(ui: Ui) -> None:
    ui.step("password", password="x" * 256)  # PASSWORD_MAX characters are fine
    assert ("POST", "login/password") in ui.fake.verbs
    ui.fake.state.update(authorized=True)
    ui.fake.chats = CHATS[:3]
    ui.step("scope", chat=["c:alpha", "c:beta", "c:gamma"])  # TG_SCOPE_MAX chats, all the account has
    assert ui.fake.scope == ["c:alpha", "c:beta", "c:gamma"]
