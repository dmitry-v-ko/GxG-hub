# ruff: noqa: S105, S106 (fake secrets of a test IdP)
"""The operator's sign-in and invite page of `admin` (admin_web): no database, a fake IdP and a fake invite store."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from gxg_approval import admin, admin_web

ISSUER, CLIENT = "https://idp.example.invalid", "gxg-admin-client"
PUBLIC = "https://admin.example.ts.net"
OPERATOR = f"oidc:{ISSUER}:op-1"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
SESSION_KEY = b"k" * 32
CODE = "invite-code-MARKER-0123456789"


class Clock:
    now = time.time()

    def __call__(self) -> float:
        return self.now


class Fixture:
    def __init__(self) -> None:
        self.clock = Clock()
        self.issued: list[tuple[str, str]] = []
        self.switched: list[tuple[str, str, bool, str]] = []
        self.switch_refusal = ""
        self.claims: dict[str, Any] = {}
        self.sign_with: Any = KEY
        self.token_status = 200
        self.exchanges: list[dict[str, str]] = []
        self.nonce = ""
        self.omit_auth_time = False

        def idp(request: httpx.Request) -> httpx.Response:
            self.exchanges.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            now = int(self.clock())
            doc = {
                "iss": ISSUER, "aud": CLIENT, "sub": "op-1", "iat": now, "exp": now + 300, "auth_time": now,
                "email_verified": True, "nonce": self.nonce,
            } | self.claims  # fmt: skip
            if self.omit_auth_time:
                doc.pop("auth_time")
            return httpx.Response(200, json={"id_token": jwt.encode(doc, self.sign_with, algorithm="ES256")})

        web = admin_web.Web(
            public_url=PUBLIC,
            client_id=CLIENT,
            client_secret="client-secret-MARKER",
            session_key=SESSION_KEY,
            endpoints=lambda: {"authorization_endpoint": f"{ISSUER}/auth", "token_endpoint": f"{ISSUER}/token"},
            http=httpx.Client(transport=httpx.MockTransport(idp)),
            ui_url="https://ui.example.ts.net",
        )
        deps = admin.Deps(
            pool=None,  # type: ignore[arg-type]
            issuer=ISSUER, audience=CLIENT, operators=frozenset({OPERATOR}),
            signing_key=lambda _t: KEY.public_key(), clock=self.clock, web=web,
        )  # fmt: skip
        app = admin.create_app(deps)
        self.client = TestClient(app, base_url=PUBLIC, follow_redirects=False)

    def sign_in(self, nxt: str = "") -> TestClient:
        login = self.client.get("/login" + (f"?next={nxt}" if nxt else ""))
        assert login.status_code == 303
        query = parse_qs(urlsplit(login.headers["location"]).query)
        self.nonce, state = query["nonce"][0], query["state"][0]
        return self.finish(state)

    def finish(self, state: str, code: str = "idp-code") -> TestClient:
        self.landing = self.client.get(f"/auth/callback?code={code}&state={state}")
        return self.client


def build(monkeypatch: pytest.MonkeyPatch) -> Fixture:
    holder: dict[str, Fixture] = {}
    original = admin_web.register

    def register(app: Any, *, web: Any, deps: Any, issue_invite: Callable[[str, str], str], **_: Any) -> None:
        def issue(who: str, human: str) -> str:
            holder["f"].issued.append((who, human))
            return CODE

        def listed() -> list[tuple[str, bool, str, str, str]]:
            return [(sc, b, r, who, "2026-11-26T00:00:00+00:00") for who, sc, b, r in holder["f"].switched[-1:]]

        def switch(who: str, scope: str, blocked: bool, reason: str) -> str:
            if holder["f"].switch_refusal:
                return holder["f"].switch_refusal
            holder["f"].switched.append((who, scope, blocked, reason))
            return ""

        original(app, web=web, deps=deps, issue_invite=issue, list_switches=listed, set_switch=switch)

    monkeypatch.setattr(admin_web, "register", register)
    holder["f"] = f = Fixture()
    return f


def csrf_of(html_text: str) -> str:
    found = re.search(r'name=csrf value="([^"]+)"', html_text)
    assert found
    return found.group(1)


def test_sign_in_uses_pkce_prompt_login_and_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    login = f.client.get("/login")
    url = urlsplit(login.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.geturl().startswith(f"{ISSUER}/auth?")
    assert q["prompt"] == "login" and q["max_age"] == "300" and q["code_challenge_method"] == "S256"
    assert q["client_id"] == CLIENT and q["redirect_uri"] == PUBLIC + "/auth/callback" and q["response_type"] == "code"
    assert "__Host-gxg_admin_flow" in login.headers["set-cookie"] and "SameSite=lax" in login.headers["set-cookie"]


def test_operator_signs_in_sees_the_form_and_gets_the_code_once(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in()
    page = client.get("/invites")
    assert page.status_code == 200 and OPERATOR in page.text  # the default principal is the operator's own
    cookie = f.client.cookies.get("__Host-gxg_admin_session")
    assert cookie and "client-secret-MARKER" not in page.text
    done = client.post("/invites", content=f"csrf={csrf_of(page.text)}&human={OPERATOR}",
                       headers={"content-type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert done.status_code == 200 and CODE in done.text and "/passkeys" in done.text
    assert f.issued == [(OPERATOR, OPERATOR)]
    assert done.headers["cache-control"] == "no-store"
    assert f.exchanges[0]["code_verifier"] and f.exchanges[0]["client_secret"] == "client-secret-MARKER"
    assert CODE not in client.get("/invites").text  # shown once, not on the form


def test_the_code_is_not_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    f = build(monkeypatch)
    client = f.sign_in()
    page = client.get("/invites")
    client.post("/invites", content=f"csrf={csrf_of(page.text)}&human={OPERATOR}",
                headers={"content-type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert CODE not in caplog.text and "client-secret-MARKER" not in caplog.text


@pytest.mark.parametrize(
    ("claims", "status"),
    [
        ({"iss": "https://evil.example.invalid"}, 401),
        ({"aud": "someone-else"}, 401),
        ({"exp": 1}, 401),
        ({"nonce": "replayed"}, 401),
        ({"auth_time": int(time.time()) - 1000}, 403),  # signed in 16 minutes ago: not a fresh login
        ({"email_verified": False}, 403),
        ({"sub": "not-an-operator"}, 403),
    ],
)
def test_token_checks(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any], status: int) -> None:
    f = build(monkeypatch)
    f.claims = claims
    login = f.client.get("/login")
    q = parse_qs(urlsplit(login.headers["location"]).query)
    if "nonce" not in claims:
        f.nonce = q["nonce"][0]
    result = f.client.get(f"/auth/callback?code=x&state={q['state'][0]}")
    assert result.status_code == status and "__Host-gxg_admin_session" not in result.headers.get("set-cookie", "")
    assert f.client.get("/invites").status_code == 303  # no session was created


def test_a_forged_id_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    f.sign_with = OTHER_KEY  # signed by a key the IdP does not publish
    login = f.client.get("/login")
    q = parse_qs(urlsplit(login.headers["location"]).query)
    f.nonce = q["nonce"][0]
    result = f.client.get(f"/auth/callback?code=x&state={q['state'][0]}")
    assert result.status_code == 401 and f.client.get("/invites").status_code == 303


def test_state_mismatch_and_token_endpoint_error(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    f.client.get("/login")
    assert f.client.get("/auth/callback?code=x&state=forged").status_code == 400
    f.token_status = 400
    login = f.client.get("/login")
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    assert f.client.get(f"/auth/callback?code=x&state={state}").status_code == 401


def test_no_session_no_form_and_no_post(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    assert f.client.get("/invites").headers["location"] == "/login"
    posted = f.client.post("/invites", content="csrf=x&human=oidc:a:b",
                           headers={"content-type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert posted.status_code == 303 and f.issued == []


def test_csrf_is_required_on_every_form(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in()
    headers = {"content-type": "application/x-www-form-urlencoded"}
    assert client.post("/invites", content=f"csrf=wrong&human={OPERATOR}", headers=headers).status_code == 403
    assert client.post("/invites", content=f"human={OPERATOR}", headers=headers).status_code == 403
    assert client.post("/invites/logout", content="csrf=wrong", headers=headers).status_code == 403
    assert f.issued == []


def test_principal_must_look_like_a_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in()
    token = csrf_of(client.get("/invites").text)
    headers = {"content-type": "application/x-www-form-urlencoded"}
    bad = client.post("/invites", content=f"csrf={token}&human=someone%40example.com", headers=headers)
    assert bad.status_code == 200 and "oidc:" in bad.text and f.issued == []


def test_session_lasts_fifteen_minutes_and_a_tampered_cookie_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in()
    assert client.get("/invites").status_code == 200
    cookie = client.cookies.get("__Host-gxg_admin_session") or ""
    client.cookies.set(
        "__Host-gxg_admin_session", cookie[:-2] + ("AA" if cookie[-2:] != "AA" else "BB"), domain="admin.example.ts.net"
    )
    assert client.get("/invites").status_code == 303  # signature broken
    client.cookies.set("__Host-gxg_admin_session", cookie, domain="admin.example.ts.net")
    assert client.get("/invites").status_code == 200
    f.clock.now += 15 * 60 + 1
    assert client.get("/invites").status_code == 303  # expired


def test_seal_roundtrip_and_expiry() -> None:
    now = time.time()
    value = admin_web.seal(SESSION_KEY, {"a": 1, "exp": now + 10})
    assert admin_web.unseal(SESSION_KEY, value, now) == {"a": 1, "exp": now + 10}
    assert admin_web.unseal(SESSION_KEY, value, now + 11) is None
    assert admin_web.unseal(b"x" * 32, value, now) is None
    assert admin_web.unseal(SESSION_KEY, "garbage", now) is None


def test_without_auth_time_a_fresh_iat_signs_in_and_an_old_iat_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google gives no `auth_time`: the freshness comes from `iat` of a token bound to this prompt=login request."""
    f = build(monkeypatch)
    f.omit_auth_time = True
    client = f.sign_in()
    assert client.get("/invites").status_code == 200
    g = build(monkeypatch)
    g.omit_auth_time = True
    g.claims = {"iat": int(time.time()) - 1000}
    login = g.client.get("/login")
    q = parse_qs(urlsplit(login.headers["location"]).query)
    g.nonce = q["nonce"][0]
    assert g.client.get(f"/auth/callback?code=x&state={q['state'][0]}").status_code == 403
    assert g.client.get("/invites").status_code == 303


def test_callback_is_a_page_not_a_redirect_so_the_strict_cookie_is_sent_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 303 would stay in the cross-site chain from the IdP and the browser would drop the Strict cookie (a loop)."""
    f = build(monkeypatch)
    login = f.client.get("/login")
    q = parse_qs(urlsplit(login.headers["location"]).query)
    f.nonce = q["nonce"][0]
    done = f.client.get(f"/auth/callback?code=x&state={q['state'][0]}")
    assert done.status_code == 200 and "location" not in done.headers
    assert 'http-equiv="refresh" content="0;url=/invites"' in done.text and 'href="/invites"' in done.text
    cookie = done.headers["set-cookie"]
    assert "__Host-gxg_admin_session" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert f.client.get("/invites").status_code == 200  # the next, same-site request carries the cookie


FORGED = {  # AC-M14-09 (OPS-07, SEC-25): what `tailscale serve` adds is not an operator
    "Tailscale-User-Login": "operator@example.com",
    "Tailscale-User-Name": "Operator",
    "X-Forwarded-For": "100.64.0.7",
}


def test_tailscale_headers_are_not_an_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    calls = (
        ("GET", "/v1/switches", None),
        ("POST", "/v1/switches", {"scope": "global", "blocked": True, "reason": "forged header"}),
        ("POST", "/v1/switches", {"scope": "global", "blocked": False, "reason": "forged header"}),
        ("POST", "/v1/passkey-invites", {"human": "oidc:a:b"}),
    )
    for method, path, body in calls:
        r = f.client.request(method, path, headers=FORGED, json=body)
        assert r.status_code in (401, 403), (path, r.status_code)
    assert f.client.get("/invites", headers=FORGED).headers["location"] == "/login"
    posted = f.client.post("/invites", content="csrf=x&human=oidc:a:b",
                           headers={**FORGED, "content-type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert posted.status_code == 303 and f.issued == []


FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_switch_page_needs_a_session_and_returns_to_it_after_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    assert f.client.get("/switches").headers["location"] == "/login?next=/switches"
    assert (
        f.client.post("/switches", content="csrf=x&scope=global&blocked=1&reason=r", headers=FORM).headers["location"]
        == "/login?next=/switches"
    )
    assert f.switched == []
    client = f.sign_in("/switches")
    assert "url=/switches" in f.landing.text
    page = client.get("/switches")
    assert page.status_code == 200 and "все эффекты открыты" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_login_next_is_not_an_open_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    f.sign_in("https://evil.example/x")
    assert "url=/invites" in f.landing.text and "evil" not in f.landing.text


def test_operator_blocks_all_effects_from_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in("/switches")
    csrf = csrf_of(client.get("/switches").text)
    done = client.post("/switches", content=f"csrf={csrf}&scope=global&blocked=1&reason=DRILL", headers=FORM)
    assert done.status_code == 200 and "global: заблокировано" in done.text
    assert f.switched == [(OPERATOR, "global", True, "DRILL")]
    assert "<b>заблокировано</b>" in client.get("/switches").text


def test_a_foreign_form_or_empty_fields_change_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in("/switches")
    csrf = csrf_of(client.get("/switches").text)
    bad = client.post("/switches", content="csrf=forged&scope=global&blocked=1&reason=x", headers=FORM)
    assert bad.status_code == 403
    empty = client.post("/switches", content=f"csrf={csrf}&scope=global&blocked=1&reason=", headers=FORM)
    assert "Нужны scope и причина" in empty.text
    assert f.switched == []


def test_clearing_needs_a_sign_in_not_older_than_5_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in("/switches")
    csrf = csrf_of(client.get("/switches").text)
    clear = f"csrf={csrf}&scope=global&blocked=0&reason=over"
    f.clock.now += 301  # the session (15 min) is alive, the sign-in is not fresh any more
    stale = client.post("/switches", content=clear, headers=FORM)
    assert "свежего входа" in stale.text and f.switched == []
    block = client.post("/switches", content=f"csrf={csrf}&scope=global&blocked=1&reason=x", headers=FORM)
    assert "заблокировано" in block.text  # blocking needs no fresh sign-in, as in the API
    f.clock.now -= 2
    fresh = client.post("/switches", content=clear, headers=FORM)
    assert "global: блокировка снята" in fresh.text
    assert f.switched[-1] == (OPERATOR, "global", False, "over")


def test_a_refusal_of_the_database_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    f = build(monkeypatch)
    client = f.sign_in("/switches")
    csrf = csrf_of(client.get("/switches").text)
    f.switch_refusal = "unknown scope"
    page = client.post("/switches", content=f"csrf={csrf}&scope=bogus&blocked=1&reason=x", headers=FORM)
    assert page.status_code == 200 and "unknown scope" in page.text
