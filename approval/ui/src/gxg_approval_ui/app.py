"""Approval Web UI (approval-service-v1 «Web UI», EFF-15, SEC-06, SEC-07, SEC-12, AS-15…AS-17).

Server-rendered, no inline script. A human is `oidc:<iss>:<sub>` from a verified ID token (SEC-01); roles come
from the current grants bundle. Every write goes through a SECURITY DEFINER function of the `gxg_ui` role, which
re-checks the intent state, version, args hash, deadline, requester ≠ approver and the approver set frozen at T1.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

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

from . import render, stepup

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
STEP_UP = (403, "Нужно подтверждение passkey (step-up) для этой заявки")
NOT_RESOLVER = (403, "Нужна роль approver или tenant_admin")
TELEGRAM_APPROVERS = (403, "Привязка Telegram — только для approver'ов")
NO_CODE = (503, "Не удалось выдать код, попробуйте ещё раз")
LONG_COMMENT = (400, f"Комментарий длиннее {COMMENT_MAX} символов")
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
OIDC_CLIENT = {"scope": "openid", "code_challenge_method": "S256"}  # code flow with PKCE; authlib adds the nonce
OIDC_DISCOVERY = "/.well-known/openid-configuration"
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
_SQL_DECIDE = "SELECT gxg_decide(%s, %s, %s, %s, %s, %s, %s)"
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
    clock: Callable[[], float] = time.time
    oidc_client_kwargs: dict[str, Any] = field(default_factory=dict)


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _field(form: Mapping[str, Any], name: str) -> str:
    """A form value as text; a missing or file field is empty."""
    value = form.get(name)
    return value if isinstance(value, str) else ""


def create_app(deps: Deps) -> FastAPI:  # noqa: PLR0915 (one closure per screen)
    app = FastAPI(**NO_DOC_PAGES)  # type: ignore[arg-type]
    templates = TEMPLATES
    split = urlsplit(deps.public_url)
    rp = stepup.RelyingParty(rp_id=str(split.hostname), origin=f"{split.scheme}://{split.netloc}")
    oauth = OAuth()
    oauth.register(
        "oidc",
        client_id=deps.oidc_client_id,
        client_secret=deps.oidc_client_secret,
        server_metadata_url=deps.oidc_issuer.rstrip("/") + OIDC_DISCOVERY,
        client_kwargs=OIDC_CLIENT | deps.oidc_client_kwargs,
    )

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    app.add_middleware(SessionMiddleware, secret_key=deps.session_key, **SESSION_COOKIE_FLAGS)  # type: ignore[arg-type]
    app.mount("/static", StaticFiles(packages=[("gxg_approval_ui", "static")]))

    @app.exception_handler(Refused)
    def refused(request: Request, exc: Refused) -> Response:
        return page(request, "error.html", {"message": exc.message}, status=exc.status)

    @app.exception_handler(GxgError)
    def unavailable(request: Request, exc: GxgError) -> Response:  # bundle not ready, no payload key
        return page(request, "error.html", {"message": UNAVAILABLE[1]}, status=UNAVAILABLE[0])

    def page(request: Request, name: str, ctx: dict[str, Any], status: int = 200) -> Response:
        session = request.session
        base = {"human": session.get("sub"), "csrf": session.get("csrf")}
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
        opened = envelope(row)
        title, label = ("—", "—") if opened is None else (opened[0]["title"], opened[0]["target"]["label"])
        group = hashlib.sha256(
            json.dumps([row["requester_principal"], opened and opened[0]], sort_keys=True).encode()
        ).hexdigest()
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
        request.session.update(
            {"sub": f"oidc:{claims['iss']}:{claims['sub']}", "iat": now, "seen": now, "csrf": secrets.token_urlsafe(32)}
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = await request.form()
        check_csrf(request, _field(form, "csrf"))
        request.session.clear()
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
            return page(request, "list.html", {"screen": "pending", "groups": groups(rows)})

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

    @app.get("/intents/{intent_id}/step-up")
    def step_up_options(request: Request, intent_id: str) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            row = load(conn, intent_id)
            if "approver" not in roles(who).get(row["tenant_id"], frozenset()):
                raise Refused(*NOT_APPROVER)
            options = stepup.authentication_options(
                rp, conn, request.session, human=who, intent_id=str(row["intent_id"]),
                args_hash=row["canonical_args_hash"], now=deps.clock(),
            )  # fmt: skip
            return Response(options, media_type="application/json")

    @app.post("/intents/{intent_id}/decision")
    async def decide(request: Request, intent_id: str) -> Response:
        form = await request.form()
        who = human(request)
        check_csrf(request, _field(form, "csrf"))
        return await run_in_threadpool(lambda: _decide(request, who, intent_id, form))

    def _decide(request: Request, who: str, intent_id: str, form: Mapping[str, Any]) -> Response:
        decision, args_hash, comment = _field(form, "decision"), _field(form, "args_hash"), _field(form, "comment")
        try:
            version = int(_field(form, "version"))
        except ValueError:
            raise Refused(*NO_VERSION) from None
        if len(comment) > COMMENT_MAX:
            raise Refused(*LONG_COMMENT)
        with deps.pool.connection() as conn:
            row = load(conn, intent_id)  # EFF-15: re-read before the decision
            if "approver" not in roles(who).get(row["tenant_id"], frozenset()):
                raise Refused(*NOT_APPROVER)  # current bundle (T2)
            step_up = False
            if decision == "approve":
                opened = envelope(row)
                if opened is None or not opened[2]:
                    raise Refused(*HASH_MISMATCH)
                if row["step_up_required"]:
                    step_up = stepup.verify(
                        rp, conn, request.session, human=who, intent_id=str(row["intent_id"]), args_hash=args_hash,
                        assertion_json=_field(form, "assertion"), now=deps.clock(),
                    )  # fmt: skip
                    if not step_up:
                        raise Refused(*STEP_UP)
            call(conn, _SQL_DECIDE, (row["intent_id"], who, decision, version, args_hash, step_up, comment or None))
        return RedirectResponse(f"/intents/{row['intent_id']}", status_code=303)

    @app.post("/intents/{intent_id}/resolve")
    async def resolve(request: Request, intent_id: str) -> Response:
        form = await request.form()
        who = human(request)
        check_csrf(request, _field(form, "csrf"))

        def work() -> Response:
            try:
                version = int(_field(form, "version"))
            except ValueError:
                raise Refused(*NO_VERSION) from None
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

    @app.get("/passkeys/options")
    def passkey_options(request: Request) -> Response:
        who = human(request)
        with deps.pool.connection() as conn:
            options = stepup.registration_options(rp, conn, request.session, human=who, now=deps.clock())
        return Response(options, media_type="application/json")

    @app.post("/passkeys")
    async def add_passkey(request: Request) -> Response:
        who = human(request)
        check_csrf(request, request.headers.get("x-csrf-token"))
        body = (await request.body()).decode("utf-8", "replace")
        with deps.pool.connection() as conn:
            ok = stepup.register(rp, conn, request.session, human=who, credential_json=body, now=deps.clock())
        return JSONResponse({"registered": ok}, status_code=200 if ok else 400)

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

    def call(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...]) -> Any:
        """A transition function; its refusal (SQLSTATE GXnnn) becomes the HTTP status, nothing is written."""
        try:
            with conn.transaction():
                return (conn.execute(sql, params).fetchone() or (None,))[0]
        except psycopg.Error as exc:
            status = _HTTP.get(exc.sqlstate or "")
            if status is None:
                raise
            raise Refused(status, str(exc.diag.message_primary)[:200]) from None

    return app
