"""OIDC sign-in in process (SEC-06, SEC-01): discovery, authorization redirect with PKCE and nonce, code exchange,
ID token checks. The IdP is an httpx MockTransport that signs ID tokens (joserfc, authlib's JOSE library); the
browser path with a real issuer is the Playwright E2E."""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx  # authlib 1.9's HTTP client
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

ISSUER = "https://idp.example.invalid"
CLIENT = "ui"


class FakeIdp:
    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
        self.codes: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}
        self.token_requests: list[dict[str, list[str]]] = []

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "sub": "1", "aud": CLIENT, "iat": now, "exp": now + 300, "nonce": nonce}
        return jwt.encode({"alg": "RS256", "kid": "k1"}, claims | self.claims, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks", "response_types_supported": ["code"],
                "id_token_signing_alg_values_supported": ["RS256"], "subject_types_supported": ["public"],
            })  # fmt: skip
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.token_requests.append(form | {"authorization": [request.headers.get("authorization", "")]})
            grant = self.codes.get(form["code"][0])
            verifier = form.get("code_verifier", [""])[0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if grant is None or challenge != grant["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "expires_in": 60,
                                             "id_token": self.id_token(grant["nonce"])})  # fmt: skip
        return httpx.Response(404)


@pytest.fixture
def idp(ui_deps: Any) -> FakeIdp:
    fake = FakeIdp()
    ui_deps.oidc_issuer = ISSUER
    ui_deps.oidc_client_id = CLIENT
    ui_deps.oidc_client_kwargs = {"transport": httpx.MockTransport(fake.handler)}
    return fake


@pytest.fixture
def client(idp: FakeIdp, ui_deps: Any, owner: Any) -> Any:
    from gxg_approval_ui.app import create_app

    with TestClient(create_app(ui_deps), base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def start(client: Any, idp: FakeIdp) -> dict[str, str]:
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    idp.codes["code-1"] = q
    return q


def test_authorization_request(client: Any, idp: FakeIdp) -> None:
    q = start(client, idp)
    assert (
        q["response_type"] == "code" and q["client_id"] == CLIENT and q["scope"] == "openid email"
    )  # `email` feeds GXG_UI_OPERATORS (M13)
    assert q["redirect_uri"] == "https://ui.example.test/auth/callback"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert len(q["state"]) >= 20 and len(q["nonce"]) >= 20


def test_sign_in(client: Any, idp: FakeIdp) -> None:
    """The human is oidc:<iss>:<sub> of the verified ID token; a new session with a CSRF token."""
    q = start(client, idp)
    hop = client.get(f"/auth/callback?code=code-1&state={q['state']}")
    assert 'content="0;url=/auth/complete?code=code-1&amp;state=' in hop.text  # same-site hop (SameSite=Strict)
    r = client.get(f"/auth/complete?code=code-1&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = client.get("/history").text
    assert f"oidc:{ISSUER}:1" in page and 'name="csrf" value="' in page
    basic = base64.b64encode(f"{CLIENT}:test-only".encode()).decode()
    assert idp.token_requests[0]["authorization"] == [f"Basic {basic}"]  # client_secret_basic
    assert idp.token_requests[0]["redirect_uri"] == ["https://ui.example.test/auth/callback"]


def test_state_is_checked(client: Any, idp: FakeIdp) -> None:
    start(client, idp)
    r = client.get("/auth/complete?code=code-1&state=forged", follow_redirects=False)
    assert r.status_code == 403 and "Вход не удался" in r.text
    assert client.get("/history").status_code == 403


def test_pkce_is_checked(client: Any, idp: FakeIdp) -> None:
    q = start(client, idp)
    idp.codes["code-1"] = q | {"code_challenge": "x" * 43}
    assert client.get(f"/auth/complete?code=code-1&state={q['state']}").status_code == 403


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"nonce": "replayed"},
        {"exp": 1},
        {"sub": ""},
    ],
)
def test_id_token_is_checked(client: Any, idp: FakeIdp, claims: dict[str, Any]) -> None:
    q = start(client, idp)
    idp.claims = claims
    r = client.get(f"/auth/complete?code=code-1&state={q['state']}", follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/history").status_code == 403


def test_id_token_signed_by_another_key(client: Any, idp: FakeIdp) -> None:
    q = start(client, idp)
    other = RSAKey.generate_key(2048, parameters={"kid": "k1"})
    idp.id_token = lambda nonce: jwt.encode(  # type: ignore[method-assign]
        {"alg": "RS256", "kid": "k1"},
        {
            "iss": ISSUER,
            "sub": "1",
            "aud": CLIENT,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "nonce": nonce,
        },
        other,
    )
    assert client.get(f"/auth/complete?code=code-1&state={q['state']}").status_code == 403


def test_login_starts_a_new_session(client: Any, idp: FakeIdp, browser: Any) -> None:
    """No session fixation: /login drops whatever session came with the request."""
    q = start(client, idp)
    client.get(f"/auth/complete?code=code-1&state={q['state']}")
    assert client.get("/history").status_code == 200
    client.get("/login", follow_redirects=False)
    assert client.get("/history").status_code == 403


def test_callback_escapes_the_query(client: Any) -> None:
    page = client.get('/auth/callback?code="><script>x</script>').text
    assert "<script>x" not in page and 'code="' not in page and "code=%22%3E%3Cscript%3Ex" in page


def test_no_claims(client: Any, idp: FakeIdp) -> None:
    q = start(client, idp)
    idp.claims = {"sub": None}
    assert client.get(f"/auth/complete?code=code-1&state={q['state']}").status_code == 403


def test_issuer_with_a_trailing_slash(idp: FakeIdp, ui_deps: Any, owner: Any) -> None:
    from gxg_approval_ui.app import create_app

    ui_deps.oidc_issuer = ISSUER + "/"
    with TestClient(create_app(ui_deps), base_url="https://testserver", raise_server_exceptions=False) as c:
        assert c.get("/login", follow_redirects=False).status_code == 302  # discovery at <issuer>/.well-known/…
