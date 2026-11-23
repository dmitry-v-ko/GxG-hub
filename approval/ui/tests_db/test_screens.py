"""AC-M9-05 (EX-19), AC-M9-06, AC-M9-11 and the screens: lists, card, Telegram link code, readiness."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

import psycopg

HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
VIEWER, ADMIN = "oidc:https://idp.example.invalid:viewer", "oidc:https://idp.example.invalid:admin"
RLO, ZWSP = chr(0x202E), chr(0x200B)  # written as code points: formatters keep them visible
HOSTILE = [
    "<script>alert(1)</script>",
    "**md** _x_ <b>bold</b>",
    "[x](javascript:alert(1)) https://evil.example",
    f"abc{RLO}dcba",
    f"zero{ZWSP}width",
]
LONG = "x" * 20000
TEMPLATE_TAGS = {"section", "h2", "p", "span", "code", "dl", "dt", "dd", "pre", "details", "summary", "table", "tr",
                 "th", "td"}  # fmt: skip


class Preview(HTMLParser):
    """Tags and text inside <section id="preview">."""

    def __init__(self) -> None:
        super().__init__()
        self.depth, self.tags, self.values, self._pre = 0, set[str](), list[str](), False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.depth or ("id", "preview") in attrs:
            self.depth += 1
            self.tags.add(tag)
            self._pre = tag == "pre" and ("class", "pv-text pv-value") in [(k, v) for k, v in attrs]

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            self.depth -= 1
            self._pre = False

    def handle_data(self, data: str) -> None:
        if self._pre:
            self.values.append(data)


def hostile_preview(propose: Any) -> str:
    fields = [{"label": f"f{i}", "value": v, "kind": "multiline"} for i, v in enumerate(HOSTILE)]
    fields.append({"label": "long", "value": LONG, "kind": "text"})
    r = propose(
        args={"text": HOSTILE[0], "n": 1},
        preview={
            "schema_version": 1,
            "title": "<i>Title</i>",
            "target": {"label": "<a href=x>y</a>", "rid": "demo:demo-main/board/b1"},
            "fields": fields,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["intent_id"])


def test_preview_is_plain_text(browser: Any, propose: Any) -> None:
    """AC-M9-05: no element made from a value, invisible characters as markers, a warning, no cut."""
    intent = hostile_preview(propose)
    page = browser.login(HUMAN1).client.get(f"/intents/{intent}").text
    parser = Preview()
    parser.feed(page)
    assert parser.tags <= TEMPLATE_TAGS, parser.tags - TEMPLATE_TAGS
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert (
        "<b>bold" not in page and "<i>Title" not in page and 'href="javascript' not in page and "<a href=x" not in page
    )
    assert parser.values[:5] == [
        "&lt;script&gt;alert(1)&lt;/script&gt;".replace("&lt;", "<").replace("&gt;", ">"),
        "**md** _x_ <b>bold</b>",
        "[x](javascript:alert(1)) https://evil.example",
        "abc⟦U+202E⟧dcba",
        "zero⟦U+200B⟧width",
    ]
    assert len(parser.values[5]) == 20000
    assert f"{RLO}" not in page and f"{ZWSP}" not in page
    assert 'id="hidden-warning"' in page
    assert page.count("<script") == 1  # only the static script in <head>


def test_no_warning_without_hidden_characters(browser: Any, propose: Any) -> None:
    intent = propose().json()["intent_id"]
    page = browser.login(HUMAN1).client.get(f"/intents/{intent}").text
    assert 'id="hidden-warning"' not in page and 'id="hash-warning"' not in page
    assert "hello partner" in page  # canonical args shown under the preview (EFF-33)


def test_card_shows_what_the_spec_lists(browser: Any, propose: Any) -> None:
    """SEC-12, EFF-15: preview, canonical args, requester, business_task_id, rule, deadline, decisions."""
    intent = propose(business_task_id="task-<77>").json()
    page = browser.login(HUMAN1).client.get(f"/intents/{intent['intent_id']}").text
    needles = ("Send a message", "Board b1", "agent:t-alpha:bot", "task-&lt;77&gt;",
               "&#34;text&#34;: &#34;hello partner&#34;", intent["expires_at"].replace("Z", "+00:00"),
               "по умолчанию для класса", 'id="decisions"')  # fmt: skip
    for needle in needles:
        assert needle in page, needle


def test_security_headers_and_cookie(browser: Any, propose: Any) -> None:
    """AC-M9-06: CSP default-src 'self' without unsafe-inline; __Host- cookie HttpOnly, Secure, SameSite=Strict."""
    browser.login(HUMAN1)
    for path in ("/", f"/intents/{propose().json()['intent_id']}", "/history", "/signed-out", "/healthz"):
        r = browser.client.get(path)
        csp = r.headers["content-security-policy"]
        assert csp.startswith("default-src 'self'") and "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert "strict-transport-security" in r.headers
    cookie = browser.client.get("/").headers["set-cookie"]
    name, _, attrs = cookie.partition("=")
    flags = {a.strip().split("=")[0].lower() for a in attrs.split(";")}
    assert name == "__Host-gxg" and {"httponly", "secure", "samesite", "path"} <= flags and "domain" not in flags
    assert "samesite=strict" in cookie.lower() and "path=/;" in cookie.lower().replace(" ", "") + ";"


def test_session_timeouts(browser: Any, clock: Any) -> None:
    """«Вход»: 30 min idle, 8 h absolute."""
    browser.login(HUMAN1)
    clock.now += 29 * 60
    assert browser.client.get("/history").status_code == 200
    clock.now += 31 * 60
    assert browser.client.get("/history").status_code == 403
    browser.login(HUMAN1)
    for _ in range(17):  # active all the time, still out after 8 h
        clock.now += 29 * 60
        browser.client.get("/history")
    assert browser.client.get("/history").status_code == 403


def test_pending_list_sorted_and_grouped(browser: Any, propose: Any, world: Any) -> None:
    """AP-09: by deadline; same requester and preview grouped, each its own link."""
    world.set_policy("t-alpha", [{"id": "short", "match": {"tools": ["demo.wire.propose"]},
                                  "decision": "require_approval", "approval_ttl": "1h"}])  # fmt: skip
    a = propose().json()["intent_id"]
    b = propose().json()["intent_id"]
    c = propose(tool="demo.wire.propose", args={"text": "wire", "n": 9}).json()["intent_id"]  # earlier deadline
    page = browser.login(HUMAN1).client.get("/").text
    assert page.index(c) < page.index(a) < page.index(b)
    assert "2 одинаковых заявки" in page and page.count('class="intent"') == 3


def test_lists_only_own_tenants(browser: Any, propose: Any) -> None:
    beta = propose(tenant="t-beta", requester="agent:t-beta:bot", resource="demo:demo-b/board/b1",
                   connection="demo-b").json()["intent_id"]  # fmt: skip
    alpha = propose().json()["intent_id"]
    page = browser.login(HUMAN1).client.get("/").text
    assert alpha in page and beta not in page
    assert browser.login(VIEWER).client.get("/").text.count('class="intent"') == 0  # a viewer decides nothing


def test_history_shows_terminal(browser: Any, propose: Any) -> None:
    done = propose(tool="demo.note.propose").json()["intent_id"]  # write_reversible: approved, not terminal
    browser.login(HUMAN1)
    rejected = propose().json()
    browser.decide({"intent_id": rejected["intent_id"], "version": 1,
                    "canonical_args_hash": rejected_hash(browser, rejected)}, "reject")  # fmt: skip
    page = browser.login(VIEWER).client.get("/history").text
    assert rejected["intent_id"] in page and done not in page


def rejected_hash(browser: Any, doc: dict[str, Any]) -> str:
    page: str = browser.client.get(f"/intents/{doc['intent_id']}").text
    return page.split('name="args_hash" value="')[1].split('"', maxsplit=1)[0]


def unknown_intent(propose: Any, row_of: Any, browser: Any, execute: Any) -> dict[str, Any]:
    intent = propose().json()
    row = row_of(intent["intent_id"])
    assert browser.login(HUMAN1).decide(row).status_code == 303
    execute(intent["intent_id"], "outcome_unknown")
    out: dict[str, Any] = row_of(intent["intent_id"])
    assert out["status"] == "outcome_unknown"
    return out


def test_manual_reconcile(browser: Any, propose: Any, row_of: Any, execute: Any, owner: Any) -> None:
    """AC-M9-11: a reason under 10 characters → refused; with one → status changes, audit row with the author."""
    row = unknown_intent(propose, row_of, browser, execute)
    url = f"/intents/{row['intent_id']}/resolve"
    browser.login(ADMIN)
    assert str(row["intent_id"]) in browser.client.get("/unknown").text
    form = {"csrf": browser.csrf, "version": row["version"], "outcome": "executed"}
    for reason in ("", "short one", None):
        data = form | ({} if reason is None else {"reason": reason})
        assert browser.client.post(url, data=data).status_code == 400
    assert row_of(row["intent_id"])["status"] == "outcome_unknown"
    assert browser.client.post(url, data=form | {"reason": "provider shows it", "csrf": ""}).status_code == 403
    r = browser.client.post(url, data=form | {"reason": "provider log shows it"}, follow_redirects=False)
    assert r.status_code == 303 and row_of(row["intent_id"])["status"] == "reconciled_succeeded"
    audit = owner.execute(
        "SELECT actor, details FROM audit_events WHERE action = 'resolve_manual' AND intent_id = %s",
        (row["intent_id"],),
    ).fetchall()
    assert audit == [(ADMIN, {"reason": "provider log shows it"})]


def test_manual_reconcile_roles_and_version(browser: Any, propose: Any, row_of: Any, execute: Any) -> None:
    row = unknown_intent(propose, row_of, browser, execute)
    url = f"/intents/{row['intent_id']}/resolve"
    browser.login(VIEWER)
    form = {"csrf": browser.csrf, "version": row["version"], "outcome": "not_executed", "reason": "checked by hand"}
    assert browser.client.post(url, data=form).status_code == 403
    browser.login(HUMAN2)
    form["csrf"] = browser.csrf
    assert browser.client.post(url, data=form | {"version": 1}).status_code == 409
    assert browser.client.post(url, data=form | {"outcome": "maybe"}).status_code == 400
    assert browser.client.post(url, data=form, follow_redirects=False).status_code == 303
    assert row_of(row["intent_id"])["status"] == "reconciled_failed"


def test_database_refuses_machine_resolution(pg: Any, propose: Any, row_of: Any, browser: Any, execute: Any) -> None:
    row = unknown_intent(propose, row_of, browser, execute)
    with pg.connect("gxg_ui") as ui:
        try:
            ui.execute("SELECT gxg_resolve_manual(%s, 'agent:t-alpha:bot', 'executed', %s, 'a long reason')",
                       (row["intent_id"], row["version"]))  # fmt: skip
            raise AssertionError("a machine resolved")
        except psycopg.Error as exc:
            assert exc.sqlstate == "GX403"


def test_telegram_link_code(browser: Any, owner: Any) -> None:
    """«Привязка Telegram»: 6 digits from the UI, 10 minutes; a new code replaces the old one."""
    browser.login(HUMAN1)
    page = browser.client.post("/telegram/code", data={"csrf": browser.csrf}).text
    code = page.split('id="link-command">/link ')[1][:6]
    assert code.isdigit()
    rows = owner.execute("SELECT human_principal, tenant_ids, expires_at - created_at, used_at FROM link_codes")
    assert [(r[0], r[1], r[2].total_seconds(), r[3]) for r in rows] == [(HUMAN1, ["t-alpha"], 600.0, None)]
    browser.client.post("/telegram/code", data={"csrf": browser.csrf})
    live = owner.execute("SELECT count(*) FROM link_codes WHERE expires_at > now()").fetchone()
    assert live == (1,)
    assert browser.client.post("/telegram/code", data={"csrf": "nope"}).status_code == 403
    assert browser.login(VIEWER).client.post("/telegram/code", data={"csrf": browser.csrf}).status_code == 403


def test_readyz(browser: Any) -> None:
    assert browser.client.get("/readyz").json() == {"ready": True, "db": True, "reasons": []}
    assert browser.client.get("/healthz").json() == {"status": "ok"}


def test_unknown_or_malformed_intent(browser: Any) -> None:
    browser.login(HUMAN1)
    assert browser.client.get("/intents/not-a-uuid").status_code == 404
    assert browser.client.get("/intents/00000000-0000-4000-8000-000000000000").status_code == 404


def test_signed_out_page_offers_login(browser: Any) -> None:
    page = browser.client.get("/")
    assert page.status_code == 200 and 'id="login" href="/login"' in page.text


def test_args_not_matching_the_hash_cannot_be_approved(
    browser: Any, propose: Any, row_of: Any, world: Any, owner: Any
) -> None:
    """EFF-33: canonical args that contradict the intent's hash → warning, no approve (409), reject still works."""
    from gxg_connector_kit import canonical

    intent = row_of(propose().json()["intent_id"])
    forged = {"preview": world.body()["preview"], "canonical_args": {"text": "something else", "n": 1}}
    owner.execute(
        "UPDATE intents SET preview_ciphertext = %s WHERE intent_id = %s",
        (world.encryptor.seal("t-alpha", "preview", canonical.dumps(forged)), intent["intent_id"]),
    )
    browser.login(HUMAN1).enroll()
    page = browser.client.get(f"/intents/{intent['intent_id']}").text
    assert 'id="hash-warning"' in page and 'id="approve"' not in page and 'id="reject"' in page
    assert browser.decide(intent).status_code == 409
    assert browser.challenge(intent).status_code == 409  # no approve challenge to smuggle as «reject» (critic M10-1)
    assert row_of(intent["intent_id"])["status"] == "pending_approval"
    assert browser.decide(intent, "reject").status_code == 303


def test_purged_payload(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """RET-01: after purge the card says so and nothing can be approved."""
    intent = row_of(propose().json()["intent_id"])
    owner.execute("ALTER TABLE intents DISABLE TRIGGER USER")
    owner.execute("UPDATE intents SET preview_ciphertext = NULL WHERE intent_id = %s", (intent["intent_id"],))
    owner.execute("ALTER TABLE intents ENABLE TRIGGER USER")
    page = browser.login(HUMAN1).client.get(f"/intents/{intent['intent_id']}").text
    assert "очищены" in page and 'id="decide"' not in page
    assert browser.decide(intent).status_code == 409
    assert "—" in browser.client.get("/").text


def test_passkey_registration_is_checked(browser: Any, soft_authenticator: Any, invite: Any, owner: Any) -> None:
    """AS-17, AS-30: a passkey is stored only through the decider, from a fresh, matching attestation of the
    signed-in human holding an operator invite."""
    browser.login(HUMAN1)
    key, token = soft_authenticator(), invite(HUMAN1)

    def post(path: str, doc: dict[str, Any], csrf: str | None = None) -> Any:
        return browser.client.post(path, json=doc, headers={"X-CSRF-Token": csrf or browser.csrf})

    def register(options: str, who: Any = key) -> Any:
        return post("/passkeys", {"invite": token, "credential": json.loads(who.register(options))})

    assert register('{"challenge": "AAAA"}').status_code == 400  # no options were issued
    assert post("/passkeys/options", {"invite": token}, csrf="wrong").status_code == 403
    assert post("/passkeys/options", {}).status_code == 400
    options = post("/passkeys/options", {"invite": token}).text
    assert post("/passkeys", {"invite": token, "credential": {}}, csrf="wrong").status_code == 403
    assert register(options, soft_authenticator(origin="https://evil.example")).status_code == 400
    owner.execute("UPDATE decider_challenges SET created_at = created_at - interval '301 seconds'")
    assert register(options).status_code == 400  # older than 5 minutes
    assert owner.execute("SELECT count(*) FROM webauthn_credentials").fetchone() == (0,)
    options = post("/passkeys/options", {"invite": token}).text
    assert register(options).json() == {"registered": True}
    assert register(options).status_code == 400  # the challenge and the invite are used once
    assert owner.execute("SELECT human_principal FROM webauthn_credentials").fetchall() == [(HUMAN1,)]
    assert "Passkey зарегистрирован" in browser.client.get("/passkeys").text


def test_no_preview_key_is_unavailable(browser: Any, propose: Any, ui_deps: Any) -> None:
    intent = propose().json()["intent_id"]
    ui_deps.preview_keys.pop("t-alpha")
    assert browser.login(HUMAN1).client.get(f"/intents/{intent}").status_code == 503


def test_preview_key_that_does_not_open_it(browser: Any, propose: Any, ui_deps: Any) -> None:
    from nacl.public import PrivateKey, SealedBox

    intent = propose().json()["intent_id"]
    ui_deps.preview_keys["t-alpha"] = SealedBox(PrivateKey.generate())
    assert browser.login(HUMAN1).client.get(f"/intents/{intent}").status_code == 503
