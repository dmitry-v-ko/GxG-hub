"""Edges of the ui: session limits, form fields, headers, WebAuthn options, lists. Each pins one condition."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from gxg_approval_ui import app as ui

HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
WIRE = "demo.wire.propose"
RLO = chr(0x202E)


def intent(propose: Any, row_of: Any, **kw: Any) -> dict[str, Any]:
    r = propose(**kw)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = row_of(r.json()["intent_id"])
    return out


def error_text(r: Any) -> str:
    import html

    return html.unescape(str(r.text.split('id="error">')[1].split("<", 1)[0]))


def test_refusals_carry_their_text(browser: Any, propose: Any, row_of: Any) -> None:
    row = intent(propose, row_of)
    assert error_text(browser.client.get("/history")) == ui.SIGN_IN[1]
    browser.login(HUMAN1)
    assert error_text(browser.decide(row, csrf="x")) == ui.STALE_FORM[1]
    assert error_text(browser.client.get("/intents/x")) == ui.NOT_FOUND[1]
    assert error_text(browser.decide(row, version="")) == ui.NO_VERSION[1]
    assert error_text(browser.decide(row, comment="c" * 501)) == ui.LONG_COMMENT[1]
    assert "already decided" not in error_text(browser.decide(row, args_hash="0" * 64))  # the DB's own message
    assert error_text(browser.decide(row, args_hash="0" * 64)) == "stale decision: version, args hash or status"
    browser.login("oidc:https://idp.example.invalid:beta")
    assert error_text(browser.decide(row)) == ui.NOT_APPROVER[1]


def test_page_shows_who_and_the_form_token(browser: Any) -> None:
    page = browser.login(HUMAN1).client.get("/history").text
    assert f'<span class="who">{HUMAN1}</span>' in page and f'name="csrf" value="{browser.csrf}"' in page


def test_session_idle_boundary(browser: Any, clock: Any) -> None:
    browser.login(HUMAN1)
    clock.now += ui.SESSION_IDLE_S
    assert browser.client.get("/history").status_code == 200  # exactly 30 min idle: still in
    clock.now += 20 * 60
    assert browser.client.get("/history").status_code == 200  # activity moved the idle clock
    clock.now += ui.SESSION_IDLE_S + 0.001
    assert browser.client.get("/history").status_code == 403


def test_session_absolute_boundary(browser: Any, clock: Any) -> None:
    browser.login(HUMAN1)
    for _ in range(16):
        clock.now += 30 * 60
        assert browser.client.get("/history").status_code == 200
    assert clock.now - browser.session["iat"] == ui.SESSION_MAX_S  # exactly 8 h: still in
    clock.now += 0.001
    assert browser.client.get("/history").status_code == 403
    assert browser.client.get("/history").status_code == 403  # and the session is gone


def test_incomplete_session_is_no_session(browser: Any, clock: Any) -> None:
    for drop in ("sub", "iat", "seen"):
        browser.login(HUMAN1)
        session = {k: v for k, v in browser.session.items() if k != drop}
        from itsdangerous import TimestampSigner

        signed = TimestampSigner(browser.key).sign(base64.b64encode(json.dumps(session).encode())).decode()
        browser.client.cookies.clear()
        browser.client.cookies.set(ui.SESSION_COOKIE, signed, domain="testserver.local")
        assert browser.client.get("/history").status_code == 403, drop


def test_session_without_csrf_token(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """A session that never got a CSRF token accepts no form, not even one with an empty token."""
    from itsdangerous import TimestampSigner

    row = intent(propose, row_of)
    browser.login(HUMAN1)
    session = {k: v for k, v in browser.session.items() if k != "csrf"}
    browser.client.cookies.clear()
    signed = TimestampSigner(browser.key).sign(base64.b64encode(json.dumps(session).encode())).decode()
    browser.client.cookies.set(ui.SESSION_COOKIE, signed, domain="testserver.local")
    assert browser.decide(row, csrf="", sign=False).status_code == 403
    assert browser.decide(row, csrf="anything", sign=False).status_code == 403
    assert browser.challenge(row).status_code == 403
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_logout(browser: Any) -> None:
    browser.login(HUMAN1)
    assert browser.client.post("/logout", data={"csrf": "x"}).status_code == 403
    assert browser.client.get("/history").status_code == 200
    r = browser.client.post("/logout", data={"csrf": browser.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/signed-out"
    assert browser.client.get("/history").status_code == 403


def test_comment_limit(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    a, b = intent(propose, row_of), intent(propose, row_of, args={"text": "b", "n": 2})
    browser.login(HUMAN1)
    assert browser.decide(a, "reject", comment="c" * 501).status_code == 400
    assert browser.decide(a, "reject", comment="c" * 500).status_code == 303
    assert browser.decide(b, "reject", comment="").status_code == 303
    rows = owner.execute("SELECT length(comment) FROM decisions ORDER BY decided_at").fetchall()
    assert rows == [(500,), (None,)]  # an empty comment is no comment


def test_missing_fields(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    row = intent(propose, row_of)
    browser.login(HUMAN1)
    url = f"/intents/{row['intent_id']}/challenge"
    base = {"version": 1, "args_hash": row["canonical_args_hash"], "decision": "approve"}
    headers = {"X-CSRF-Token": browser.csrf}
    for drop, status in (("version", 400), ("args_hash", 409), ("decision", 400)):
        r = browser.client.post(url, data={k: v for k, v in base.items() if k != drop}, headers=headers)
        assert r.status_code == status, drop
    files = {"decision": ("d.txt", b"approve")}
    data = {k: v for k, v in base.items() if k != "decision"}
    assert browser.client.post(url, data=data, files=files, headers=headers).status_code == 400  # a file is no field
    assert browser.client.post(url, data=base).status_code == 403  # no CSRF header
    decision = {"csrf": browser.csrf, "decision": "approve"}  # no assertion: refused before the decider
    assert browser.client.post(f"/intents/{row['intent_id']}/decision", data=decision).status_code == 403
    assert owner.execute("SELECT count(*) FROM decider_challenges").fetchone() == (0,)  # none for a broken form
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_headers_exact(browser: Any) -> None:
    r = browser.login(HUMAN1).client.get("/history")
    for name, value in ui.SECURITY_HEADERS.items():
        assert r.headers[name] == value
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    cookie = r.headers["set-cookie"]
    assert "; Max-Age=28800;" in cookie or "; max-age=28800;" in cookie.lower()
    assert "samesite=strict" in cookie and "secure" in cookie and "httponly" in cookie and "path=/" in cookie


def test_no_api_documentation_pages(browser: Any) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert browser.client.get(path).status_code == 404


def test_static_files(browser: Any) -> None:
    js = browser.client.get("/static/webauthn.js")
    assert js.status_code == 200 and "navigator.credentials.get" in js.text
    assert browser.client.get("/static/app.css").status_code == 200


def test_readyz_not_ready(browser: Any, ui_deps: Any, world: Any) -> None:
    ui_deps.pool.close()
    r = browser.client.get("/readyz")
    assert r.status_code == 503 and r.json()["db"] is False


def test_readyz_without_bundle(ui_deps: Any, owner: Any, tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from gxg_connector_kit.grants import BundleStore

    ui_deps.bundles = BundleStore({}, state_file=tmp_path / "state.json")
    with TestClient(ui.create_app(ui_deps), base_url="https://testserver") as c:
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["db"] is True and r.json()["reasons"]
        assert c.get("/history").status_code == 403  # no session first
    with TestClient(ui.create_app(ui_deps), base_url="https://testserver", raise_server_exceptions=False) as c:
        from itsdangerous import TimestampSigner

        session = {"sub": HUMAN1, "iat": ui_deps.clock(), "seen": ui_deps.clock(), "csrf": "t"}
        signed = TimestampSigner(ui_deps.session_key).sign(base64.b64encode(json.dumps(session).encode())).decode()
        c.cookies.set(ui.SESSION_COOKIE, signed, domain="testserver.local")
        r = c.get("/history")
        assert r.status_code == 503 and error_text(r) == ui.UNAVAILABLE[1]  # GR-12: no bundle, no roles


def test_list_rows_show_marked_text(browser: Any, propose: Any) -> None:
    preview = {"schema_version": 1, "title": f"<i>T{RLO}</i>", "target": {"label": f"L{RLO}",
               "rid": "demo:demo-main/board/b1"}, "fields": [{"label": "x", "value": "y", "kind": "text"}]}  # fmt: skip
    doc = propose(preview=preview).json()
    page = browser.login(HUMAN1).client.get("/").text
    row = page.split(f'href="/intents/{doc["intent_id"]}">')[1].split("</li>")[0]
    expires = doc["expires_at"].replace("Z", "+00:00")
    assert row == (
        "&lt;i&gt;T⟦U+202E⟧&lt;/i&gt;</a> · L⟦U+202E⟧ · t-alpha · agent:t-alpha:bot · "
        f'<span class="status">pending_approval</span> · срок {expires}'
    )


def test_same_preview_other_args_are_grouped(browser: Any, propose: Any) -> None:
    """AP-09: the group is requester + preview (what the human sees), not the args."""
    preview = {"schema_version": 1, "title": "Same", "target": {"label": "Board b1", "rid": "demo:demo-main/board/b1"},
               "fields": [{"label": "Text", "value": "same", "kind": "text"}]}  # fmt: skip
    propose(args={"text": "one", "n": 1}, preview=preview)
    propose(args={"text": "two", "n": 2}, preview=preview)
    propose(args={"text": "two", "n": 2}, preview=preview, requester="agent:t-alpha:other")
    page = browser.login(HUMAN1).client.get("/").text
    assert page.count('class="group"') == 2 and "2 одинаковых заявки" in page


def test_decision_options_name_the_enrolled_passkeys(
    browser: Any, passkey: Any, propose: Any, row_of: Any, make_browser: Any
) -> None:
    row = intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    passkey(make_browser().login(HUMAN2))
    options = json.loads(browser.challenge(row).text)
    cid = base64.urlsafe_b64encode(key.credential_id).decode().rstrip("=")
    assert options["allowCredentials"] == [{"id": cid, "type": "public-key"}]
    assert options["userVerification"] == "required" and options["rpId"] == "ui.example.test"
    assert browser.login(HUMAN1).challenge({**row, "intent_id": "x"}).status_code == 404
    browser.login("oidc:https://idp.example.invalid:viewer")
    assert browser.challenge(row).status_code == 403


def _options(browser: Any, invite: str) -> Any:
    return browser.client.post("/passkeys/options", json={"invite": invite}, headers={"X-CSRF-Token": browser.csrf})


def _register(browser: Any, invite: str, credential_json: str) -> Any:
    doc = {"invite": invite, "credential": json.loads(credential_json)}
    return browser.client.post("/passkeys", json=doc, headers={"X-CSRF-Token": browser.csrf})


def test_registration_options(browser: Any, passkey: Any, invite: Any) -> None:
    browser.login(HUMAN1)
    first = json.loads(_options(browser, invite(HUMAN1)).text)
    assert first["rp"] == {"name": "GxG Approval", "id": "ui.example.test"}
    user_id = base64.urlsafe_b64encode(hashlib.sha256(HUMAN1.encode()).digest()).decode().rstrip("=")
    assert first["user"] == {"id": user_id, "name": HUMAN1, "displayName": HUMAN1}
    assert len(base64.urlsafe_b64decode(first["challenge"] + "==")) == 32
    assert first["authenticatorSelection"] == {"residentKey": "preferred", "requireResidentKey": False,
                                               "userVerification": "required"}  # fmt: skip
    assert first["excludeCredentials"] == []
    key = passkey(browser)
    again = json.loads(_options(browser, invite(HUMAN1)).text)
    cid = base64.urlsafe_b64encode(key.credential_id).decode().rstrip("=")
    assert again["excludeCredentials"] == [{"id": cid, "type": "public-key"}]


def test_registration_needs_this_humans_unused_invite(
    browser: Any, soft_authenticator: Any, invite: Any, owner: Any
) -> None:
    """AS-30: no invite, another human's invite, a used or an expired invite → no passkey."""
    browser.login(HUMAN1)
    assert _options(browser, "no-such-invite-0000").status_code == 403
    assert _options(browser, invite(HUMAN2)).status_code == 403
    token = invite(HUMAN1)
    key = soft_authenticator()
    assert _register(browser, token, key.register(_options(browser, token).text)).json() == {"registered": True}
    assert _options(browser, token).status_code == 403  # used
    late = invite(HUMAN1)
    owner.execute("UPDATE passkey_invites SET expires_at = now() WHERE used_at IS NULL")
    assert _options(browser, late).status_code == 403
    rows = owner.execute("SELECT human_principal, enrolled_by FROM webauthn_credentials").fetchall()
    assert rows == [(HUMAN1, "oidc:https://idp.example.invalid:operator")]


def test_user_verification_is_required(
    browser: Any, soft_authenticator: Any, propose: Any, row_of: Any, owner: Any, invite: Any
) -> None:
    """AS-16: a passkey touch without biometrics/PIN (UV flag off) neither registers nor signs a decision."""
    browser.login(HUMAN1)
    weak = soft_authenticator(user_verified=False)
    token = invite(HUMAN1)
    assert _register(browser, token, weak.register(_options(browser, token).text)).status_code == 400
    strong = browser.enroll(soft_authenticator())
    row = intent(propose, row_of, tool=WIRE)
    options = browser.challenge(row).text
    strong.user_verified = False
    assert browser.decide(row, assertion=strong.assertion(options)).status_code == 403
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_registration_challenge_lives_five_minutes(
    browser: Any, soft_authenticator: Any, owner: Any, invite: Any
) -> None:
    browser.login(HUMAN1)
    key, token = soft_authenticator(), invite(HUMAN1)
    options = _options(browser, token).text
    owner.execute("UPDATE decider_challenges SET created_at = created_at - interval '301 seconds'")
    assert _register(browser, token, key.register(options)).status_code == 400
    options = _options(browser, token).text  # the invite is still unused
    owner.execute(
        "UPDATE decider_challenges SET created_at = created_at - interval '290 seconds' WHERE used_at IS NULL"
    )
    assert _register(browser, token, key.register(options)).json() == {"registered": True}


def test_render_card_args_json() -> None:
    from gxg_approval_ui import render

    assert render.card({"title": "t"}, {"b": 1, "a": "é"})[1] == '{\n  "a": "é",\n  "b": 1\n}'


def test_purged_row_in_the_list(browser: Any, propose: Any, owner: Any) -> None:
    doc = propose().json()
    owner.execute("ALTER TABLE intents DISABLE TRIGGER USER")
    owner.execute("UPDATE intents SET preview_ciphertext = NULL")
    owner.execute("ALTER TABLE intents ENABLE TRIGGER USER")
    page = browser.login(HUMAN1).client.get("/").text
    row = page.split(f'href="/intents/{doc["intent_id"]}">')[1].split(" · t-alpha")[0]
    assert row == "—</a> · —"


def test_one_unreadable_preview_does_not_hide_the_list(browser: Any, propose: Any, owner: Any) -> None:
    """Critic M9-1 F4: the bad row says «превью недоступно», the others are listed, the card of the bad one is 503."""
    bad, good = propose().json(), propose(args={"text": "good", "n": 2}).json()
    owner.execute("UPDATE intents SET preview_ciphertext = '\\x00ff' WHERE intent_id = %s", (bad["intent_id"],))
    page = browser.login(HUMAN1).client.get("/")
    assert page.status_code == 200 and good["intent_id"] in page.text
    row = page.text.split(f'href="/intents/{bad["intent_id"]}">')[1].split(" · t-alpha")[0]
    assert row == f"{ui.PREVIEW_UNAVAILABLE}</a> · —"
    assert browser.client.get(f"/intents/{bad['intent_id']}").status_code == 503


def test_version_out_of_range(browser: Any, propose: Any, row_of: Any, execute: Any, owner: Any) -> None:
    """Critic M9-1 F9: the functions take a PostgreSQL integer; anything else is the form's fault (400)."""
    row = intent(propose, row_of)
    browser.login(HUMAN1)
    for bad in (2**31, -1, 0):
        assert browser.decide(row, version=bad).status_code == 400, bad
    assert browser.decide(row, version=2**31 - 1).status_code == 409  # in range, just stale
    url = f"/intents/{row['intent_id']}/resolve"
    data = {"csrf": browser.csrf, "outcome": "executed", "reason": "a long enough reason"}
    assert browser.client.post(url, data=data | {"version": 2**31}).status_code == 400
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_assertion_replay_with_a_zero_counter(
    browser: Any, make_browser: Any, soft_authenticator: Any, world: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    """Critic M9-1 F5, AC-M10-08: a synced passkey (counter always 0) replayed from an old cookie: the decider's
    challenge row is used once, so the replay is refused before the one-vote rule is even asked."""
    world.set_policy("t-alpha", [{"id": "two", "match": {"tools": [WIRE]}, "decision": "require_approval",
                                  "approvals": 2}])  # fmt: skip
    row = intent(propose, row_of, tool=WIRE)
    browser.login(HUMAN1)
    key = browser.enroll(soft_authenticator(zero_counter=True))
    assertion = key.assertion(browser.challenge(row).text)
    old_cookie = [c for c in browser.client.cookies.jar]
    assert browser.decide(row, assertion=assertion).status_code == 303
    replay = make_browser()
    for c in old_cookie:
        replay.client.cookies.jar.set_cookie(c)
    replay.session = browser.session
    assert replay.decide(row_of(row["intent_id"]), assertion=assertion).status_code == 403
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (1,)
