"""Operator sign-in and the passkey-invite page of `admin` (AS-24, AS-30): `GET /invites`.

The invite is issued by `admin` itself, after the operator signs in at the IdP **here** (OIDC code flow with PKCE,
`prompt=login`, `max_age=300`); the Approval UI takes no part, so a compromised UI cannot enrol a passkey. `admin`
verifies the ID token on its own: signature (JWKS), `iss`, `aud` = client id, `nonce`, `exp`, `auth_time` ≤ 5 minutes,
`email_verified`, and the principal `oidc:<iss>:<sub>` must be in `GXG_ADMIN_OPERATORS`. The session is a separate,
sealed cookie of at most 15 minutes; every form carries a CSRF token. The invite code is shown once, in the response
to the form post, and is written nowhere else (not to a log, not to the audit: the audit holds only its hash).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

SESSION_COOKIE = "__Host-gxg_admin_session"
FLOW_COOKIE = "__Host-gxg_admin_flow"
SESSION_MAX_S = 15 * 60
FLOW_MAX_S = 10 * 60
MAX_AGE_S = 300  # a fresh login at the IdP: the same bound as the step-up of the API
PRINCIPAL = re.compile(r"^oidc:\S{1,500}$")
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


@dataclass(frozen=True)
class Web:
    public_url: str  # https://<admin>.<tailnet>.ts.net
    client_id: str
    client_secret: str
    session_key: bytes
    endpoints: Callable[[], dict[str, str]]  # discovery: authorization_endpoint, token_endpoint
    http: httpx.Client
    ui_url: str = ""  # where the approver enters the code (the «Passkey» page of the UI)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(key: bytes, payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def unseal(key: bytes, value: str | None, now: float) -> dict[str, Any] | None:
    if not value or value.count(".") != 1:
        return None
    body, _, mac = value.partition(".")
    if not hmac.compare_digest(_b64(hmac.new(key, body.encode(), hashlib.sha256).digest()), mac):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int | float) or payload["exp"] < now:
        return None
    return payload


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px system-ui;max-width:34rem;margin:2rem auto;padding:0 1rem}"
        "input{font:inherit;width:100%;padding:.4rem;box-sizing:border-box}"
        "button,a.b{font:inherit;padding:.5rem 1rem;border:1px solid;border-radius:.4rem;background:none}"
        "code{font-size:1.2rem;word-break:break-all}</style>"
        f"<h1>{html.escape(title)}</h1>{body}</html>"
    )
    return HTMLResponse(doc, status_code=status, headers=HEADERS)


def register(app: FastAPI, *, web: Web, deps: Any, issue_invite: Callable[[str, str], str]) -> None:  # noqa: PLR0915
    """Add `/login`, `/auth/callback`, `/invites`, `/invites/logout` to the admin app."""
    redirect_uri = web.public_url.rstrip("/") + "/auth/callback"

    def secure_cookie(response: Response, name: str, value: str, max_age: int, same_site: str) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            secure=True,
            httponly=True,
            samesite=same_site,  # type: ignore[arg-type]
        )

    def session(request: Request) -> dict[str, Any] | None:
        s = unseal(web.session_key, request.cookies.get(SESSION_COOKIE), deps.clock())
        if (
            s is None or s.get("who") not in deps.operators
        ):  # operators are checked on every request, not only at sign-in
            return None
        return s

    @app.get("/login")
    def login() -> Response:
        flow = {
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "verifier": secrets.token_urlsafe(48),
            "exp": deps.clock() + FLOW_MAX_S,
        }
        challenge = _b64(hashlib.sha256(flow["verifier"].encode()).digest())
        query = urlencode(
            {
                "response_type": "code", "client_id": web.client_id, "redirect_uri": redirect_uri,
                "scope": "openid email", "state": flow["state"], "nonce": flow["nonce"],
                "code_challenge": challenge, "code_challenge_method": "S256",
                "prompt": "login", "max_age": MAX_AGE_S,
            }
        )  # fmt: skip
        response = RedirectResponse(web.endpoints()["authorization_endpoint"] + "?" + query, status_code=303)
        secure_cookie(response, FLOW_COOKIE, seal(web.session_key, flow), FLOW_MAX_S, "lax")  # the IdP navigates back
        response.headers.update(HEADERS)
        return response

    def refuse(status: int, message: str) -> Response:
        response = page("Вход не выполнен", f"<p>{html.escape(message)}</p>", status)
        response.delete_cookie(FLOW_COOKIE, path="/")
        return response

    @app.get("/auth/callback")
    def callback(request: Request) -> Response:  # noqa: PLR0911
        now = deps.clock()
        flow = unseal(web.session_key, request.cookies.get(FLOW_COOKIE), now)
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        if flow is None or not code or not hmac.compare_digest(str(flow["state"]), state):
            return refuse(400, "Сессия входа истекла или не совпала. Начните заново: /login")
        try:
            tokens = web.http.post(
                web.endpoints()["token_endpoint"],
                data={
                    "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                    "client_id": web.client_id, "client_secret": web.client_secret, "code_verifier": flow["verifier"],
                },
                timeout=15,
            )  # fmt: skip
            id_token = tokens.json()["id_token"]
            key = deps.signing_key(id_token)
            claims = jwt.decode(
                id_token, getattr(key, "key", key), algorithms=["RS256", "ES256", "EdDSA"], audience=deps.audience,
                issuer=deps.issuer, options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]}, leeway=5,
            )  # fmt: skip
        except Exception:  # token endpoint, bad signature, unknown kid, wrong iss/aud, expired, malformed
            return refuse(401, "Токен не принят.")
        if not hmac.compare_digest(str(claims["nonce"]), str(flow["nonce"])):
            return refuse(401, "Токен не принят.")
        # Google puts no `auth_time` in the ID token. The request carried `prompt=login` and a `nonce`, so a token
        # issued now (`iat`) is bound to a password prompt answered for this very request: `iat` stands in for it.
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, int | float):
            auth_time = claims.get("iat")
        if not isinstance(auth_time, int | float) or now - auth_time > MAX_AGE_S + 5:
            return refuse(403, "Нужен свежий вход (не старше 5 минут).")
        if claims.get("email_verified") is not True:
            return refuse(403, "Адрес почты не подтверждён.")
        who = f"oidc:{claims['iss']}:{claims['sub']}"
        if who not in deps.operators:
            return refuse(403, "Нужна роль platform_admin.")
        # Not a 303: a redirect stays in the navigation chain that began at the IdP (cross-site) and the browser does not
        # send the SameSite=Strict session cookie on it, which made a loop /invites -> /login -> /auth/callback.
        # A page that moves on by itself is a new, same-site navigation.
        response = HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/invites">'
            '<title>Вход выполнен</title><p>Вход выполнен. <a href="/invites">Продолжить</a></p></html>',
            headers=HEADERS,
        )
        response.delete_cookie(FLOW_COOKIE, path="/")
        secure_cookie(
            response,
            SESSION_COOKIE,
            seal(web.session_key, {"who": who, "csrf": secrets.token_urlsafe(32), "exp": now + SESSION_MAX_S}),
            SESSION_MAX_S,
            "strict",
        )
        return response

    def form(s: dict[str, Any], error: str = "") -> Response:
        note = f'<p role="alert">{html.escape(error)}</p>' if error else ""
        return page(
            "Приглашение passkey",
            f"{note}<form method=post action=/invites>"
            f'<input type=hidden name=csrf value="{html.escape(s["csrf"])}">'
            "<p><label>Approver (принципал OIDC)<br>"
            f'<input name=human value="{html.escape(s["who"])}" maxlength=512 required></label></p>'
            "<p><button>Выдать приглашение passkey</button></p></form>"
            f"<form method=post action=/invites/logout><input type=hidden name=csrf value=\"{html.escape(s['csrf'])}\">"
            "<button>Выйти</button></form>",
        )

    @app.get("/invites")
    def invites(request: Request) -> Response:
        s = session(request)
        return form(s) if s else RedirectResponse("/login", status_code=303)

    async def read_form(request: Request, s: dict[str, Any]) -> dict[str, str] | None:
        if len(await request.body()) > 4096:
            return None
        values = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
        if not hmac.compare_digest(values.get("csrf", ""), str(s["csrf"])):
            return None
        return values

    @app.post("/invites")
    async def issue(request: Request) -> Response:
        s = session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        values = await read_form(request, s)
        if values is None:
            return page("Запрос отклонён", "<p>Неверная форма (CSRF).</p>", 403)
        human = values.get("human", "").strip()
        if not PRINCIPAL.match(human):
            return form(s, "Принципал должен выглядеть как oidc:<issuer>:<sub>.")
        code = issue_invite(s["who"], human)
        link = (
            f'<p><a class=b href="{html.escape(web.ui_url.rstrip("/") + "/passkeys")}">Страница «Passkey» в UI</a></p>'
            if web.ui_url
            else ""
        )
        return page(
            "Приглашение выдано",
            f"<p>Для <b>{html.escape(human)}</b>. Код показан один раз, действует 24 часа и только один раз:</p>"
            f"<p><code>{html.escape(code)}</code></p>{link}<p>Передайте код человеку отдельным каналом.</p>",
        )

    @app.post("/invites/logout")
    async def logout(request: Request) -> Response:
        s = session(request)
        if s is not None and await read_form(request, s) is None:
            return page("Запрос отклонён", "<p>Неверная форма (CSRF).</p>", 403)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
