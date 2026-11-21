"""AC-M9-02, AC-M9-03, AC-M9-04 (EX-07, EX-08): who may decide, with which form, and with which step-up."""

from __future__ import annotations

from typing import Any

import psycopg

HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
VIEWER, BETA_HUMAN = "oidc:https://idp.example.invalid:viewer", "oidc:https://idp.example.invalid:beta"
WIRE = "demo.wire.propose"  # high_risk: step-up by default (AP-01)


def decisions(owner: psycopg.Connection[Any]) -> int:
    return int((owner.execute("SELECT count(*) FROM decisions").fetchone() or (0,))[0])


def new_intent(propose: Any, row_of: Any, **kw: Any) -> dict[str, Any]:
    r = propose(**kw)
    assert r.status_code == 201, r.text
    row: dict[str, Any] = row_of(r.json()["intent_id"])
    return row


def test_approve_then_status(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    intent = new_intent(propose, row_of)
    r = browser.login(HUMAN1).decide(intent)
    assert r.status_code == 303 and r.headers["location"] == f"/intents/{intent['intent_id']}"
    assert row_of(intent["intent_id"])["status"] == "approved"
    assert owner.execute("SELECT decided_by, step_up, comment FROM decisions").fetchall() == [(HUMAN1, False, None)]


def test_reject(browser: Any, propose: Any, row_of: Any) -> None:
    intent = new_intent(propose, row_of)
    assert browser.login(HUMAN1).decide(intent, "reject", comment="wrong chat").status_code == 303
    assert row_of(intent["intent_id"])["status"] == "rejected"


def test_csrf_missing_or_foreign(browser: Any, make_browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-02: no CSRF token, another session's token → 403, nothing written."""
    intent = new_intent(propose, row_of)
    other = make_browser().login(HUMAN2)
    browser.login(HUMAN1)
    assert browser.decide(intent, csrf="").status_code == 403
    assert browser.decide(intent, csrf=other.csrf).status_code == 403
    body = {"version": intent["version"], "args_hash": intent["canonical_args_hash"], "decision": "approve"}
    assert browser.client.post(f"/intents/{intent['intent_id']}/decision", data=body).status_code == 403
    assert decisions(owner) == 0 and row_of(intent["intent_id"])["status"] == "pending_approval"


def test_stale_version_or_changed_hash(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-02: a stale screen (version) or other args (hash) → 409, nothing written (EFF-11)."""
    intent = new_intent(propose, row_of)
    browser.login(HUMAN1)
    assert browser.decide(intent, version=intent["version"] + 1).status_code == 409
    assert browser.decide(intent, version=0).status_code == 409
    assert browser.decide(intent, args_hash="0" * 64).status_code == 409
    assert browser.decide(intent, "reject", args_hash="f" * 64).status_code == 409
    assert browser.decide(intent, version="x").status_code == 400
    assert decisions(owner) == 0 and row_of(intent["intent_id"])["status"] == "pending_approval"


def test_decided_intent_cannot_be_decided_again(browser: Any, make_browser: Any, propose: Any, row_of: Any) -> None:
    intent = new_intent(propose, row_of)
    assert browser.login(HUMAN1).decide(intent).status_code == 303
    assert make_browser().login(HUMAN2).decide(intent, "reject").status_code == 409  # old version on the screen
    assert row_of(intent["intent_id"])["status"] == "approved"


def test_machine_credentials_cannot_decide(browser: Any, world: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-03, SEC-06: the agent's service JWT (and no session at all) → 403."""
    intent = new_intent(propose, row_of)
    body = {"version": 1, "args_hash": intent["canonical_args_hash"], "decision": "approve", "csrf": "x"}
    no_session: list[dict[str, str]] = [world.auth("demo"), world.auth("approval-mcp"), {}]
    for headers in no_session:
        r = browser.client.post(f"/intents/{intent['intent_id']}/decision", data=body, headers=headers)
        assert r.status_code == 403
    assert decisions(owner) == 0


def test_requester_cannot_approve_own_intent(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-03, SEC-07: the same OIDC subject as the requester → 403 (refused by gxg_decide)."""
    intent = new_intent(propose, row_of, requester=HUMAN1)
    assert HUMAN1 in intent["allowed_approvers"]  # only the requester check stops it
    assert browser.login(HUMAN1).decide(intent).status_code == 403
    assert decisions(owner) == 0
    assert browser.login(HUMAN2).decide(intent).status_code == 303


def test_approver_of_another_tenant(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-03: an approver of t-beta on a t-alpha intent → 403; the card is not found (no cross-tenant read)."""
    intent = new_intent(propose, row_of)
    browser.login(BETA_HUMAN)
    assert browser.decide(intent).status_code == 403
    assert browser.client.get(f"/intents/{intent['intent_id']}").status_code == 404
    assert decisions(owner) == 0


def test_viewer_sees_but_cannot_decide(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    intent = new_intent(propose, row_of)
    browser.login(VIEWER)
    page = browser.client.get(f"/intents/{intent['intent_id']}")
    assert page.status_code == 200 and 'id="decide"' not in page.text
    assert browser.decide(intent).status_code == 403 and decisions(owner) == 0


def test_approver_removed_from_bundle_after_t1(
    browser: Any, world: Any, propose: Any, row_of: Any, owner: Any, monkeypatch: Any
) -> None:
    """T2 «роль approver по bundle»: the current bundle decides, even though the frozen set still has the human."""
    intent = new_intent(propose, row_of)
    names = type(world).reload.__globals__  # the module that builds the bundle
    original = names["tenant_file"]

    def without_human2(tenant: str, agents: list[str], connection: str) -> dict[str, Any]:
        doc: dict[str, Any] = original(tenant, agents, connection)
        doc["principals"]["humans"] = [h for h in doc["principals"]["humans"] if h["id"] != HUMAN2]
        return doc

    monkeypatch.setitem(names, "tenant_file", without_human2)
    world.reload("c" * 40)
    assert browser.login(HUMAN2).decide(intent).status_code == 403
    assert decisions(owner) == 0


def test_database_refuses_a_human_outside_the_frozen_set(pg: Any, propose: Any, row_of: Any) -> None:
    """M9 narrowing: even a compromised ui cannot record a decision of a non-approver or for another tenant."""
    intent = new_intent(propose, row_of)
    with pg.connect("gxg_ui") as ui:
        for who in ("oidc:https://idp.example.invalid:intruder", BETA_HUMAN, VIEWER):
            try:
                ui.execute(
                    "SELECT gxg_decide(%s, %s, 'approve', 1, %s, false, null)",
                    (intent["intent_id"], who, intent["canonical_args_hash"]),
                )
                raise AssertionError(f"{who} decided")
            except psycopg.Error as exc:
                assert exc.sqlstate == "GX403"


def test_quorum_of_two_needs_two_people(
    browser: Any, make_browser: Any, world: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    """AC-M9-04, EX-08: one human twice → the second decision 409, status stays pending_approval."""
    world.set_policy("t-alpha", [{"id": "two", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvals": 2}])  # fmt: skip
    intent = new_intent(propose, row_of)
    browser.login(HUMAN1)
    assert browser.decide(intent).status_code == 303
    again = row_of(intent["intent_id"])
    assert again["status"] == "pending_approval" and again["version"] == 2
    assert browser.decide(again).status_code == 409
    assert row_of(intent["intent_id"])["status"] == "pending_approval" and decisions(owner) == 1
    assert make_browser().login(HUMAN2).decide(again).status_code == 303
    assert row_of(intent["intent_id"])["status"] == "approved"


def test_high_risk_needs_step_up(browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-04: high_risk without step-up → 403; with a fresh assertion for this intent → approved."""
    intent = new_intent(propose, row_of, tool=WIRE)
    assert intent["step_up_required"] is True
    browser.login(HUMAN1)
    assert browser.decide(intent).status_code == 403  # no passkey, no assertion
    key = passkey(browser)
    assert browser.decide(intent).status_code == 403  # a passkey, but no assertion
    assert browser.decide(intent, assertion="{}").status_code == 403
    options = browser.client.get(f"/intents/{intent['intent_id']}/step-up").text
    assert str(intent["intent_id"]) in _challenge(options)
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 303
    assert row_of(intent["intent_id"])["status"] == "approved"
    assert owner.execute("SELECT step_up FROM decisions").fetchall() == [(True,)]


def _challenge(options_json: str) -> str:
    import base64
    import json

    c = json.loads(options_json)["challenge"]
    return base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode()


def test_step_up_challenge_names_intent_and_hash(browser: Any, passkey: Any, propose: Any, row_of: Any) -> None:
    """AS-16: the challenge contains intent_id and args_hash."""
    intent = new_intent(propose, row_of, tool=WIRE)
    passkey(browser.login(HUMAN1))
    text = _challenge(browser.client.get(f"/intents/{intent['intent_id']}/step-up").text)
    assert f"|{intent['intent_id']}|{intent['canonical_args_hash']}|" in text
    nonce = text.rsplit("|", 1)[1]
    assert len(nonce) == 32 and int(nonce, 16) >= 0  # 16 random bytes


def test_step_up_older_than_five_minutes(
    browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any, clock: Any
) -> None:
    """AC-M9-04: an assertion over a challenge issued more than 5 minutes ago → 403."""
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.client.get(f"/intents/{intent['intent_id']}/step-up").text
    clock.now += 301
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 403
    assert decisions(owner) == 0


def test_step_up_at_five_minutes_is_accepted(browser: Any, passkey: Any, propose: Any, row_of: Any, clock: Any) -> None:
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.client.get(f"/intents/{intent['intent_id']}/step-up").text
    clock.now += 300
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 303


def test_step_up_for_another_intent(browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-04: an assertion made for intent A does not approve intent B."""
    a = new_intent(propose, row_of, tool=WIRE)
    b = new_intent(propose, row_of, tool=WIRE, args={"text": "other", "n": 2})
    key = passkey(browser.login(HUMAN1))
    options_a = browser.client.get(f"/intents/{a['intent_id']}/step-up").text
    assert browser.decide(b, assertion=key.assertion(options_a)).status_code == 403
    assert decisions(owner) == 0


def test_step_up_is_used_once(
    browser: Any, make_browser: Any, passkey: Any, world: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    """A replayed assertion (same challenge, same counter) does not serve a second decision."""
    world.set_policy("t-alpha", [{"id": "two", "match": {"tools": [WIRE]}, "decision": "require_approval",
                                  "approvals": 2}])  # fmt: skip
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.client.get(f"/intents/{intent['intent_id']}/step-up").text
    assertion = key.assertion(options)
    cookie = dict(browser.client.cookies)
    assert browser.decide(intent, assertion=assertion).status_code == 303
    replay = make_browser()
    replay.client.cookies.clear()
    for name, value in cookie.items():  # the session as it was before the decision, challenge included
        replay.client.cookies.set(name, value, domain="testserver.local")
    replay.session = browser.session
    fresh = row_of(intent["intent_id"])
    assert replay.decide(fresh, assertion=assertion).status_code in (403, 409)
    assert decisions(owner) == 1


def test_step_up_from_another_origin_or_another_human(
    browser: Any, make_browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.client.get(f"/intents/{intent['intent_id']}/step-up").text
    assert browser.decide(intent, assertion=key.assertion(options, origin="https://evil.example")).status_code == 403
    other = make_browser().login(HUMAN2)
    passkey(other)
    options = other.client.get(f"/intents/{intent['intent_id']}/step-up").text
    assert other.decide(intent, assertion=key.assertion(options)).status_code == 403  # HUMAN1's passkey
    assert decisions(owner) == 0


def test_reject_needs_no_step_up(browser: Any, propose: Any, row_of: Any) -> None:
    """T4: «как T2, кроме step-up»."""
    intent = new_intent(propose, row_of, tool=WIRE)
    assert browser.login(HUMAN1).decide(intent, "reject").status_code == 303
    assert row_of(intent["intent_id"])["status"] == "rejected"


def test_no_passkey_is_shown(browser: Any, propose: Any, row_of: Any) -> None:
    """AS-17: an approver without a passkey sees why the high_risk intent cannot be approved."""
    intent = new_intent(propose, row_of, tool=WIRE)
    page = browser.login(HUMAN1).client.get(f"/intents/{intent['intent_id']}").text
    assert 'id="no-passkey"' in page and 'id="approve"' not in page


def test_database_refuses_rows_t1_would_never_build(pg: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """Defence in depth for gxg_decide: no frozen set (NULL) means nobody; an agent in the set is still no human."""
    intent = new_intent(propose, row_of)
    cases = [(None, HUMAN2), (["agent:t-alpha:other"], "agent:t-alpha:other")]
    for allowed, who in cases:
        owner.execute("UPDATE intents SET allowed_approvers = %s WHERE intent_id = %s", (allowed, intent["intent_id"]))
        with pg.connect("gxg_ui") as ui:
            try:
                ui.execute(
                    "SELECT gxg_decide(%s, %s, 'approve', 1, %s, false, null)",
                    (intent["intent_id"], who, intent["canonical_args_hash"]),
                )
                raise AssertionError(f"{who} decided with allowed_approvers={allowed}")
            except psycopg.Error as exc:
                assert exc.sqlstate == "GX403"
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
