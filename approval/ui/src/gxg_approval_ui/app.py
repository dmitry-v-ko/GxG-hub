"""Approval Web UI (approval-service-v1 «Web UI», EFF-15, SEC-06, SEC-07, SEC-12, AS-15…AS-17).

Server-rendered, no inline script. A human is `oidc:<iss>:<sub>` from a verified ID token (SEC-01); roles come
from the current grants bundle. Every write goes through a SECURITY DEFINER function of the `gxg_ui` role, which
re-checks the intent state, version, args hash, deadline, requester ≠ approver and the approver set frozen at T1.
Decisions and passkey enrollment are relayed to the decider (AS-30, M10): the ui cannot record a decision itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from gxg_connector_kit import canonical
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import BundleStore

from . import render, stepup, tgadmin

SESSION_COOKIE = "__Host-gxg"  # AS-15: __Host- prefix, Secure, HttpOnly, SameSite=Strict, Path=/
SESSION_MAX_S = 8 * 3600  # «Вход»: session 8 h
SESSION_IDLE_S = 30 * 60  # idle timeout 30 min
CSP = "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
TERMINAL = ("succeeded", "failed", "reconciled_succeeded", "reconciled_failed", "rejected", "expired", "cancelled")
LIST_LIMIT = 200
COMMENT_MAX = 500  # approval-decision.schema.json
LINK_CODE_TRIES = 5

# Refusals (status, text for people). Module constants: mutation testing leaves them alone; tests check both.
UNAVAILABLE = (503, "Сервис временно недоступен")
SIGN_IN = (403, "Войдите заново")
STALE_FORM = (403, "Форма устарела: обновите страницу")
NOT_FOUND = (404, "Заявка не найдена")
LOGIN_FAILED = (403, "Вход не удался")
NOT_APPROVER = (403, "Нет роли approver в этом tenant'е")
NO_VERSION = (400, "Нет версии заявки")
HASH_MISMATCH = (409, "Параметры не совпадают с хешем: заявку нужно отклонить (EFF-33)")
STEP_UP = (403, "Нужно подтверждение решения passkey (AS-30)")
BAD_DECISION = (400, "Решение: approve или reject")
NOT_RESOLVER = (403, "Нужна роль approver или tenant_admin")
TELEGRAM_APPROVERS = (403, "Привязка Telegram — только для approver'ов")
NO_CODE = (503, "Не удалось выдать код, попробуйте ещё раз")
LONG_COMMENT = (400, f"Комментарий длиннее {COMMENT_MAX} символов")
PREVIEW_UNAVAILABLE = "превью недоступно"
NOT_FOUND_PAGE = (404, "Страница не найдена")  # the operator wizard for everybody else
TG_NOT_CONFIGURED = (503, "Подключение Telegram не настроено (GXG_TELEGRAM_ADMIN_URL)")
TG_SCOPE_MAX = 3  # chats the pilot may read
BAD_PHONE = "Номер: международный формат, например +79001234567"
BAD_CODE = "Код: только цифры, как в сообщении Telegram"
NEED_2FA = "Введите пароль двухфакторной защиты"
TOO_MANY_CHATS = f"Можно выбрать не больше {TG_SCOPE_MAX} чатов"
UNKNOWN_CHAT = "Этого чата нет в списке аккаунта: обновите страницу"
TG_DOWN = "Telegram-сервис не отвечает: проверьте, что он запущен, и повторите"
PHONE = re.compile(r"^\+?\d{7,15}$")
CODE = re.compile(r"^\d{3,8}$")
PASSWORD_MAX = 256
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000",  # the ui is served over HTTPS only (AS-29)
}
NO_DOC_PAGES = {"docs_url": None, "redoc_url": None, "openapi_url": None}
SESSION_COOKIE_FLAGS = {"session_cookie": SESSION_COOKIE, "max_age": SESSION_MAX_S, "same_site": "strict",
                        "https_only": True, "path": "/"}  # fmt: skip
OIDC_CLIENT = {"scope": "openid email", "code_challenge_method": "S256"}  # code flow with PKCE; authlib adds the nonce
OIDC_DISCOVERY = "/.well-known/openid-configuration"
for _name in ("httpx", "httpx2", "httpcore"):  # SEC-17: no request URLs at INFO (authlib's IdP calls)
    logging.getLogger(_name).setLevel(logging.WARNING)
TEMPLATES = Jinja2Templates(
    env=Environment(loader=PackageLoader("gxg_approval_ui"), autoescape=True)  # AS-15: every value escaped
)

_COLUMNS = (
    "intent_id, tenant_id, requester_principal, business_task_id, tool_key, tool_version, connection_id, "
    "resource_id, effect_class, canonical_args_hash, preview_ciphertext, policy_version, rule_ids, approval_mode, "
    "required_approvals, step_up_required, status, version, created_at, expires_at, approved_at, terminal_at"
)
_SQL_INTENT = f"SELECT {_COLUMNS} FROM intents WHERE intent_id = %s"  # noqa: S608 (constant column list)
_SQL_LIST = {
    "pending": f"SELECT {_COLUMNS} FROM intents WHERE status = 'pending_approval' AND tenant_id = ANY(%s) "  # noqa: S608
    f"ORDER BY expires_at, intent_id LIMIT {LIST_LIMIT}",
    "unknown": f"SELECT {_COLUMNS} FROM intents WHERE status = 'outcome_unknown' AND tenant_id = ANY(%s) "  # noqa: S608
    f"ORDER BY created_at, intent_id LIMIT {LIST_LIMIT}",
    "history": f"SELECT {_COLUMNS} FROM intents WHERE status = ANY(%s) AND tenant_id = ANY(%s) "  # noqa: S608
    f"ORDER BY terminal_at DESC, intent_id LIMIT {LIST_LIMIT}",
}
_SQL_DECISIONS = (
    "SELECT decided_by, decision, step_up, comment, decided_at FROM decisions WHERE intent_id = %s ORDER BY decided_at"
)
_SQL_RECEIVED = (
    "SELECT count(*) FROM decisions WHERE intent_id = %s AND decision = 'approve' AND decided_by NOT LIKE 'policy:%%'"
)
_SQL_RESOLVE = "SELECT gxg_resolve_manual(%s, %s, %s, %s, %s)"
_SQL_LINK_CODE = "SELECT gxg_link_code(%s, %s, %s)"
_SQL_LINKS = "SELECT tenant_id, linked_at FROM approver_links WHERE human_principal = %s ORDER BY tenant_id"
_HTTP = {"GX400": 400, "GX403": 403, "GX404": 404, "GX409": 409, "GX423": 423}  # _gxg_fail SQLSTATEs


@dataclass
class Deps:
    pool: ConnectionPool[Any]
    bundles: BundleStore
    preview_keys: Mapping[str, SealedBox[PrivateKey]]  # tenant → preview private key (AS-28: only the ui)
    session_key: str
    public_url: str  # https://approve.example.com (the OIDC redirect and the WebAuthn origin)
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    decider: stepup.Decider  # AS-30: decisions and passkeys
    operators: frozenset[str] = (
        frozenset()
    )  # GXG_UI_OPERATORS: lower-case emails; the session must carry a confirmed one
    telegram_admin: tgadmin.TelegramAdmin | None = None  # the operator wizard (M13); None = not configured
    clock: Callable[[], float] = time.time
    oidc_client_kwargs: dict[str, Any] = field(default_factory=dict)


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message


def _version(form: Mapping[str, Any]) -> int:
    """The intent version of the screen: a PostgreSQL integer (the functions take `integer`), else 400."""
    try:
        version = int(_field(form, "version"))
    except ValueError:
        raise Refused(*NO_VERSION) from None
    if not 0 < version < 2**31:
        raise Refused(*NO_VERSION)
    return version


def _field(form: Mapping[str, Any], name: str) -> str:
    """A form value as text; a missing or file field is empty."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


def create_app(deps: Deps) -> FastAPI:  # noqa: PLR0915 (one closure per screen)
    app = FastAPI(**NO_DOC_PAGES)  # type: ignore[arg-type]
    templates = TEMPLATES
    oauth = OAuth()
    oauth.register(
        "oidc",
        client_id=deps.oidc_client_id,
        client_secret=deps.oidc_client_secret,
        server_metadata_url=deps.oidc_issuer.removesuffix("/") + OIDC_DISCOVERY,
        client_kwargs=OIDC_CLIENT | deps.oidc_client_kwargs,
    )

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    # shortcut: signed cookie, no server-side revocation (logout clears this browser only; a copied cookie lives until
    # the idle/absolute limit); add a session table when «log out everywhere» is needed. Rotating the key ends all.
    app.add_middleware(SessionMiddleware, secret_key=deps.session_key, **SESSION_COOKIE_FLAGS)  # type: ignore[arg-type]
    app.mount("/static", StaticFiles(packages=[("gxg_approval_ui", "static")]))

    @app.exception_handler(Refused)
    def refused(request: Request, exc: Refused) -> Response:
        return page(request, "error.html", {"message": exc.message}, status=exc.status)

    @app.exception_handler(stepup.Unavailable)
    def no_decider(request: Request, exc: stepup.Unavailable) -> Response:
        return page(request, "error.html", {"message": UNAVAILABLE[1]}, status=UNAVAILABLE[0])

    @app.exception_handler(GxgError)
    def unavailable(request: Request, exc: GxgError) -> Response:  # bundle not ready, no payload key
        return page(request, "error.html", {"message": UNAVAILABLE[1]}, status=UNAVAILABLE[0])

    def page(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> Response:
        session = request.session
        base = {"human": session.get("sub"), "csrf": session.get("csrf"), "operator": is_operator(request)}
        return templates.TemplateResponse(request, name, base | ctx, status_code=status)

    def human(request: Request) -> str:
        """The signed-in human, or 403 (machine credentials and no session alike: SEC-06)."""
        session, now = request.session, deps.clock()
        try:
            sub, iat, seen = session["sub"], session["iat"], session["seen"]
        except KeyError:
            raise Refused(*SIGN_IN) from None
        if now - iat > SESSION_MAX_S or now - seen > SESSION_IDLE_S:
            session.clear()
            raise Refused(*SIGN_IN)
        session["seen"] = now
        return str(sub)

    def is_operator(request: Request) -> bool:
        """A confirmed email from GXG_UI_OPERATORS (kept in the session only when the IdP said `email_verified`)."""
        email = request.session.get("email")
        return bool(request.session.get("sub")) and isinstance(email, str) and email.lower() in deps.operators

    def operator(request: Request) -> str:
        """The signed-in operator, or 403 without a session; 404 for everybody else (the section does not exist)."""
        who = human(request)
        if not is_operator(request):
            raise Refused(*NOT_FOUND_PAGE)
        return who

    def check_csrf(request: Request, token: str | None) -> None:
        expected = request.session.get("csrf")
        if not expected or not token or not hmac.compare_digest(token, expected):
            raise Refused(*STALE_FORM)

    def roles(who: str) -> dict[str, frozenset[str]]:
        return {t: g.roles[who] for t, g in deps.bundles.current.tenants.items() if who in g.roles}

    def tenants_with(who: str, *wanted: str) -> list[str]:
        return sorted(t for t, r in roles(who).items() if r & set(wanted))

    def load(conn: psycopg.Connection[Any], intent_id: str) -> dict[str, Any]:
        try:
            key = uuid.UUID(intent_id)
        except ValueError:
            raise Refused(*NOT_FOUND) from None
        row = conn.cursor(row_factory=dict_row).execute(_SQL_INTENT, (key,)).fetchone()
        if row is None:
            raise Refused(*NOT_FOUND)
        return row

    def envelope(row: Mapping[str, Any]) -> tuple[dict[str, Any], Any, bool] | None:
        """(preview, canonical args, args match the stored hash) or None after purge (RET-01)."""
        if row["preview_ciphertext"] is None:
            return None
        box = deps.preview_keys.get(row["tenant_id"])
        if box is None:
            raise Refused(*UNAVAILABLE)  # no preview key for this tenant
        try:
            doc = json.loads(box.decrypt(bytes(row["preview_ciphertext"])))
        except CryptoError:  # a key that does not open it (rotated or wrong file): unavailable, not a crash
            raise Refused(*UNAVAILABLE) from None
        args = doc["canonical_args"]
        recomputed = canonical.hash(
            {
                "tool_key": row["tool_key"],
                "tool_version": row["tool_version"],
                "tenant_id": row["tenant_id"],
                "connection_id": row["connection_id"],
                "resource_id": row["resource_id"],
                "args": args,
            }
        )
        return doc["preview"], args, recomputed == row["canonical_args_hash"]

    def summary(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            opened = envelope(row)
            title, label = ("—", "—") if opened is None else (opened[0]["title"], opened[0]["target"]["label"])
        except Refused:  # one unreadable preview must not hide the rest of the list (EFF-14: the list is the source)
            opened, title, label = None, PREVIEW_UNAVAILABLE, "—"
        group = hashlib.sha256(canonical.dumps([row["requester_principal"], opened and opened[0]])).hexdigest()
        return {
            "id": str(row["intent_id"]),
            "tenant": row["tenant_id"],
            "status": row["status"],
            "requester": render.visible(row["requester_principal"])[0],
            "title": render.visible(title)[0],
            "label": render.visible(label)[0],
            "expires_at": row["expires_at"],
            "group": group,
        }

    def groups(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """AP-09: same requester and same preview are shown together, in the order of the earliest deadline."""
        out: dict[str, list[dict[str, Any]]] = {}
        for item in (summary(r) for r in rows):
            out.setdefault(item["group"], []).append(item)
        return list(out.values())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        try:
            with deps.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            db = True
        except Exception:  # any failure means not ready
            db = False
        state = deps.bundles.readiness()
        ok = db and state.ready
        return JSONResponse({"ready": ok, "db": db, "reasons": list(state.reasons)}, status_code=200 if ok else 503)

    @app.get("/login")
    async def login(request: Request) -> Response:
        request.session.clear()  # no session fixation: a new session after the IdP
        return await oauth.oidc.authorize_redirect(request, deps.public_url.rstrip("/") + "/auth/callback")  # type: ignore[no-any-return]

    @app.get("/auth/callback", response_class=HTMLResponse)
    def callback(request: Request) -> Response:
        # A cross-site navigation from the IdP carries no SameSite=Strict cookie; a same-site hop does.
        return page(request, "redirect.html", {"target": "/auth/complete?" + request.url.query})

    @app.get("/auth/complete")
    async def complete(request: Request) -> Response:
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except Exception:  # state, PKCE, token endpoint, ID token signature or claims (authlib/joserfc errors)
            request.session.clear()
            raise Refused(*LOGIN_FAILED) from None
        claims = token.get("userinfo") or {}
        if not claims.get("iss") or not claims.get("sub"):
            raise Refused(*LOGIN_FAILED)
        now = deps.clock()
        request.session.clear()
        email = claims.get("email")
        session = {"sub": f"oidc:{claims['iss']}:{claims['sub']}", "iat": now, "seen": now,
                   "csrf": secrets.token_urlsafe(32)}  # fmt: skip
        if isinstance(email, str) and claims.get("email_verified") is True:  # an unconfirmed email proves nothing
            session["email"] = email.lower()
        request.session.update(session)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = await request.form()
        check_csrf(request, _field(form, "csrf"))
        request.session.clear()  # shortcut: this browser only (see SessionMiddleware)
        return RedirectResponse("/signed-out", status_code=303)

    @app.get("/signed-out")
    def signed_out(request: Request) -> Response:
        return page(request, "signed_out.html", {})

    @app.get("/")
    def pending(request: Request) -> Response:
        if not request.session.get("sub"):
            return page(request, "signed_out.html", {})
        who = human(request)
        with deps.pool.connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(_SQL_LIST["pending"], (tenants_with(who, "approver"),))
                .fetchall()
            )
            return page(request, "list.html", {"screen": "pending", "groups": groups(rows)})  # operator: links if empty

    @app.get("/history")
    def history(request: Request) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(_SQL_LIST["history"], (list(TERMINAL), tenants_with(who, "approver", "viewer")))
                .fetchall()
            )
            return page(request, "list.html", {"screen": "history", "groups": [[summary(r)] for r in rows]})

    @app.get("/unknown")
    def unknown(request: Request) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            rows = (
                conn.cursor(row_factory=dict_row)
                .execute(_SQL_LIST["unknown"], (tenants_with(who, "approver", "tenant_admin"),))
                .fetchall()
            )
            return page(request, "list.html", {"screen": "unknown", "groups": [[summary(r)] for r in rows]})

    @app.get("/intents/{intent_id}")
    def show(request: Request, intent_id: str) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            row = load(conn, intent_id)
            mine = roles(who).get(row["tenant_id"], frozenset())
            if not mine & {"approver", "viewer", "tenant_admin"}:
                raise Refused(*NOT_FOUND)  # another tenant's intent looks like a missing one
            opened = envelope(row)
            preview, args_json, hidden = render.card(*opened[:2]) if opened else (None, None, False)
            decisions = conn.execute(_SQL_DECISIONS, (row["intent_id"],)).fetchall()
            received = (conn.execute(_SQL_RECEIVED, (row["intent_id"],)).fetchone() or (0,))[0]
            ctx = {
                "intent": row,
                "requester": render.visible(row["requester_principal"])[0],
                "task": render.visible(row["business_task_id"] or "")[0],
                "preview": preview,
                "args_json": args_json,
                "hidden": hidden,
                "args_match": opened is None or opened[2],
                "decisions": [(render.visible(d[0])[0], *d[1:]) for d in decisions],
                "received": received,
                "can_decide": "approver" in mine and row["status"] == "pending_approval" and opened is not None,
                "can_resolve": bool(mine & {"approver", "tenant_admin"}) and row["status"] == "outcome_unknown",
                "has_passkey": stepup.has_passkey(conn, who),
            }
            return page(request, "card.html", ctx)

    @app.post("/intents/{intent_id}/challenge")
    async def challenge(request: Request, intent_id: str) -> Response:
        """WebAuthn options for one decision on the screen's version and args hash, issued by the decider."""
        who = human(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        form = await request.form()
        decision, version = _field(form, "decision"), _version(form)
        if decision not in ("approve", "reject"):
            raise Refused(*BAD_DECISION)

        def work() -> Response:
            with deps.pool.connection() as conn:
                row = load(conn, intent_id)
            if "approver" not in roles(who).get(row["tenant_id"], frozenset()):
                raise Refused(*NOT_APPROVER)
            if decision == "approve":  # EFF-33: the signed challenge decides what is recorded, so check it here
                opened = envelope(row)
                if opened is None or not opened[2]:
                    raise Refused(*HASH_MISMATCH)
            body = {"human": who, "intent_id": str(row["intent_id"]), "decision": decision, "version": version,
                    "args_hash": _field(form, "args_hash")}  # fmt: skip
            status, text = stepup.relay(deps.decider, "/v1/decisions/options", body)
            if status != 200:
                raise Refused(status, stepup.message(text))
            return Response(text, media_type="application/json")

        return await run_in_threadpool(work)

    @app.post("/intents/{intent_id}/decision")
    async def decide(request: Request, intent_id: str) -> Response:
        form = await request.form()
        who = human(request)
        check_csrf(request, _field(form, "csrf"))
        return await run_in_threadpool(lambda: _decide(request, who, intent_id, form))

    def _decide(request: Request, who: str, intent_id: str, form: Mapping[str, Any]) -> Response:
        """The decision, its version and args hash are inside the signed challenge (AS-30); the decider checks it, and
        an approve challenge is issued only for args that match the hash (EFF-33, `challenge` above). Here: the
        current bundle still makes the human an approver (T2), the comment, and an assertion at all."""
        comment, assertion = _field(form, "comment"), _field(form, "assertion")
        if len(comment) > COMMENT_MAX:
            raise Refused(*LONG_COMMENT)
        with deps.pool.connection() as conn:
            row = load(conn, intent_id)  # EFF-15: re-read before the decision
        if "approver" not in roles(who).get(row["tenant_id"], frozenset()):
            raise Refused(*NOT_APPROVER)  # current bundle (T2)
        if not assertion:
            raise Refused(*STEP_UP)
        body = {"human": who, "intent_id": str(row["intent_id"]), "assertion": assertion, "comment": comment or None}
        status, text = stepup.relay(deps.decider, "/v1/decisions", body)
        if status != 200:
            raise Refused(status, stepup.message(text))
        return RedirectResponse(f"/intents/{row['intent_id']}", status_code=303)

    @app.post("/intents/{intent_id}/resolve")
    async def resolve(request: Request, intent_id: str) -> Response:
        form = await request.form()
        who = human(request)
        check_csrf(request, _field(form, "csrf"))

        def work() -> Response:
            version = _version(form)
            with deps.pool.connection() as conn:
                row = load(conn, intent_id)
                if not roles(who).get(row["tenant_id"], frozenset()) & {"approver", "tenant_admin"}:
                    raise Refused(*NOT_RESOLVER)
                call(
                    conn,
                    _SQL_RESOLVE,
                    (row["intent_id"], who, _field(form, "outcome"), version, _field(form, "reason")),
                )
            return RedirectResponse(f"/intents/{row['intent_id']}", status_code=303)

        return await run_in_threadpool(work)

    @app.get("/passkeys")
    def passkeys(request: Request) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            return page(request, "passkeys.html", {"has_passkey": stepup.has_passkey(conn, who)})

    def passkey_body(request: Request, raw: bytes) -> dict[str, Any]:
        who = human(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        try:
            doc = json.loads(raw)
            body = {"human": who, "invite": str(doc["invite"])}
            if "credential" in doc:
                body["credential"] = json.dumps(doc["credential"])
        except (ValueError, KeyError, TypeError):
            raise Refused(400, "invite и credential") from None
        return body

    @app.post("/passkeys/options")
    async def passkey_options(request: Request) -> Response:
        body = passkey_body(request, await request.body())
        status, text = await run_in_threadpool(stepup.relay, deps.decider, "/v1/passkeys/options", body)
        if status != 200:
            return JSONResponse({"message": stepup.message(text)}, status_code=status)
        return Response(text, media_type="application/json")

    @app.post("/passkeys")
    async def add_passkey(request: Request) -> Response:
        body = passkey_body(request, await request.body())
        status, _ = await run_in_threadpool(stepup.relay, deps.decider, "/v1/passkeys", body)
        return JSONResponse({"registered": status == 200}, status_code=200 if status == 200 else 400)

    @app.get("/telegram")
    def telegram(request: Request) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            links = conn.execute(_SQL_LINKS, (who,)).fetchall()
        return page(request, "telegram.html", {"links": links, "code": None})

    @app.post("/telegram/code")
    async def telegram_code(request: Request) -> Response:
        form = await request.form()
        who = human(request)
        check_csrf(request, _field(form, "csrf"))
        tenants = tenants_with(who, "approver")
        if not tenants:
            raise Refused(*TELEGRAM_APPROVERS)
        with deps.pool.connection() as conn:
            for _ in range(LINK_CODE_TRIES):
                code = f"{secrets.randbelow(10**6):06d}"
                if call(conn, _SQL_LINK_CODE, (who, tenants, code)):
                    links = conn.execute(_SQL_LINKS, (who,)).fetchall()
                    return page(request, "telegram.html", {"links": links, "code": code})
        raise Refused(*NO_CODE)

    # ---- M13: «Подключение Telegram» (the typed secrets only pass through)
    def admin() -> tgadmin.TelegramAdmin:
        if deps.telegram_admin is None:
            raise Refused(*TG_NOT_CONFIGURED)
        return deps.telegram_admin

    def api_error(exc: Exception, *hidden: str) -> tuple[int, str]:
        """(HTTP status, text for the person). The text never contains what the operator just typed."""
        if isinstance(exc, tgadmin.ApiError):
            text = exc.message
            for value in hidden:
                if value:
                    text = text.replace(value, "…")
            return (502 if exc.status >= 500 else 400), text
        return 502, TG_DOWN

    def wizard_view(who: str, error: str | None = None, status: int = 200) -> tuple[dict[str, Any], int]:
        """What the page shows now: the service's login state, and — once authorized — the chats and the saved scope."""
        ctx: dict[str, Any] = {"error": error, "state": None, "chats": [], "scope": [], "sync": None}
        try:
            api = admin()
            state = api.call("GET", "status", who)
            ctx["sync"] = state.get("sync")
            if state.get("authorized"):
                ctx["state"] = "authorized"
                scope = api.call("GET", "scope", who)
                chosen = scope.get("chat_refs", []) if isinstance(scope, dict) else scope
                ctx["scope"] = [str(c) for c in chosen]
                ctx["chats"] = [
                    {"ref": str(c["chat_ref"]), "title": render.visible(str(c.get("title", "")))[0],
                     "kind": str(c.get("kind", "")), "chosen": str(c["chat_ref"]) in ctx["scope"]}
                    for c in api.call("GET", "chats", who)
                ]  # fmt: skip
            else:
                ctx["state"] = (
                    state.get("login") if state.get("login") in ("code_sent", "password_required") else "idle"
                )
        except Refused:
            raise
        except (tgadmin.ApiError, tgadmin.Unavailable) as exc:
            code, text = api_error(exc)
            ctx["error"], ctx["state"] = error or text, ctx["state"] or "down"
            status = max(status, code)
        return ctx, status

    def wizard_step(api: tgadmin.TelegramAdmin, step: str, form: Any, who: str) -> None:
        """One POST of the wizard → the API; the typed values never leave this function."""
        if step == "phone":
            phone = re.sub(r"[ \-()]", "", _field(form, "phone"))
            if not PHONE.match(phone):
                raise Refused(400, BAD_PHONE)
            api.call("POST", "login/start", who, {"phone": phone})
        elif step == "code":
            code = _field(form, "code").strip()
            if not CODE.match(code):
                raise Refused(400, BAD_CODE)
            api.call("POST", "login/code", who, {"code": code})
        elif step == "password":
            password = _field(form, "password")
            if not password or len(password) > PASSWORD_MAX:
                raise Refused(400, NEED_2FA)
            api.call("POST", "login/password", who, {"password": password})
        elif step == "cancel":
            api.call("POST", "login/cancel", who)
        elif step == "scope":
            chosen = {c for c in form.getlist("chat") if isinstance(c, str)}
            if len(chosen) > TG_SCOPE_MAX:
                raise Refused(400, TOO_MANY_CHATS)
            if not chosen <= {str(c["chat_ref"]) for c in api.call("GET", "chats", who)}:
                raise Refused(400, UNKNOWN_CHAT)
            api.call("PUT", "scope", who, {"chat_refs": sorted(chosen)})
        else:
            raise Refused(*NOT_FOUND_PAGE)

    @app.get("/operator/telegram")
    def telegram_wizard(request: Request) -> Response:
        who = operator(request)
        ctx, status = wizard_view(who)
        return page(request, "operator_telegram.html", ctx, status=status)

    @app.post("/operator/telegram/{step}")
    async def telegram_step(request: Request, step: str) -> Response:
        form = await request.form()
        who = operator(request)
        check_csrf(request, _field(form, "csrf"))

        def work() -> Response:
            try:
                wizard_step(admin(), step, form, who)
            except Refused as exc:
                if exc.status in (NOT_FOUND_PAGE[0], TG_NOT_CONFIGURED[0]):
                    raise
                ctx, status = wizard_view(who, exc.message, exc.status)
                return page(request, "operator_telegram.html", ctx, status=status)
            except (tgadmin.ApiError, tgadmin.Unavailable) as exc:
                code, text = api_error(exc, *(_field(form, n) for n in ("phone", "code", "password")))
                ctx, status = wizard_view(who, text, code)
                return page(request, "operator_telegram.html", ctx, status=status)
            return RedirectResponse("/operator/telegram", status_code=303)  # PRG: the secrets are never re-rendered

        return await run_in_threadpool(work)

    def call(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
        """A transition function; its refusal (SQLSTATE GXnnn) becomes the HTTP status, nothing is written."""
        try:
            with conn.transaction():
                return (conn.execute(sql, params).fetchone() or (None,))[0]
        except psycopg.Error as exc:
            status = _HTTP.get(str(exc.sqlstate))
            if status is None:
                raise
            raise Refused(status, str(exc.diag.message_primary)) from None  # our own _gxg_fail texts

    return app
