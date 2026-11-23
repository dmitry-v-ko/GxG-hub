"""AC-M9-02, AC-M9-03, AC-M9-04 (EX-07, EX-08), AC-M10-08 (AS-30): who may decide, with which form, and with
which passkey assertion. Since M10 every decision is signed and checked by the decider, not by the ui."""

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
    assert owner.execute("SELECT decided_by, step_up, comment FROM decisions").fetchall() == [(HUMAN1, True, None)]


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
    assert browser.decide(intent, version=0).status_code == 400  # no intent has version 0
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
    assert browser.decide(intent, sign=False).status_code == 403  # no passkey, no assertion
    assert browser.challenge(intent).status_code == 403  # no enrolled passkey: no challenge either (AS-17)
    key = passkey(browser)
    assert browser.decide(intent, sign=False).status_code == 403  # a passkey, but no assertion
    assert browser.decide(intent, assertion="{}").status_code == 403
    options = browser.challenge(intent).text
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 303
    assert row_of(intent["intent_id"])["status"] == "approved"
    assert owner.execute("SELECT step_up FROM decisions").fetchall() == [(True,)]


def _challenge(options_json: str) -> bytes:
    import base64
    import json

    c = json.loads(options_json)["challenge"]
    return base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))


def test_challenge_binds_intent_hash_version_decision(browser: Any, passkey: Any, propose: Any, row_of: Any) -> None:
    """AS-30: challenge = SHA-256(intent_id ‖ args_hash ‖ version ‖ decision) ‖ 16 random bytes (decision 1)."""
    import hashlib

    intent = new_intent(propose, row_of)
    passkey(browser.login(HUMAN1))
    for decision in ("approve", "reject"):
        raw = _challenge(browser.challenge(intent, decision).text)
        bound = f"{intent['intent_id']}{intent['canonical_args_hash']}{intent['version']}{decision}".encode()
        assert raw[:32] == hashlib.sha256(bound).digest() and len(raw) == 48
    assert _challenge(browser.challenge(intent).text)[32:] != _challenge(browser.challenge(intent).text)[32:]


def _age(owner: Any, seconds: int) -> None:
    owner.execute("UPDATE decider_challenges SET created_at = created_at - make_interval(secs => %s)", (seconds,))


def test_assertion_older_than_five_minutes(browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M9-04, AS-16: an assertion over a challenge issued more than 5 minutes ago → 403, no decision."""
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.challenge(intent).text
    _age(owner, 301)
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 403
    assert decisions(owner) == 0


def test_assertion_at_just_under_five_minutes_is_accepted(
    browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.challenge(intent).text
    _age(owner, 290)
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 303


def test_assertion_for_another_intent(browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M10-08: an assertion made for intent A does not decide intent B."""
    a = new_intent(propose, row_of, tool=WIRE)
    b = new_intent(propose, row_of, tool=WIRE, args={"text": "other", "n": 2})
    key = passkey(browser.login(HUMAN1))
    options_a = browser.challenge(a).text
    assert browser.decide(b, assertion=key.assertion(options_a)).status_code == 403
    assert decisions(owner) == 0


def test_assertion_for_another_decision_version_or_hash(
    browser: Any, make_browser: Any, passkey: Any, world: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    """AC-M10-08: signed «reject» sent as «approve» records a reject (the challenge decides, not the form); signed
    for version 1, sent after the version moved → 409; a challenge for another args hash is refused at issue."""
    intent = new_intent(propose, row_of)
    key = passkey(browser.login(HUMAN1))
    signed_reject = key.assertion(browser.challenge(intent, "reject").text)
    assert browser.challenge(intent, args_hash="0" * 64).status_code == 409
    world.set_policy("t-alpha", [{"id": "two", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvals": 2}])  # fmt: skip
    quorum = new_intent(propose, row_of, args={"text": "quorum", "n": 3})
    stale = key.assertion(browser.challenge(quorum).text)  # version 1
    assert make_browser().login(HUMAN2).decide(quorum).status_code == 303  # version 2 now
    assert browser.decide(quorum, assertion=stale).status_code == 409
    assert browser.decide(intent, "approve", assertion=signed_reject).status_code == 303
    assert row_of(intent["intent_id"])["status"] == "rejected"
    rows = owner.execute("SELECT decision FROM decisions WHERE intent_id = %s", (intent["intent_id"],)).fetchall()
    assert rows == [("reject",)]


def test_assertion_is_used_once(browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AC-M10-08: a replayed assertion is refused by the decider's challenge row, not only by one-vote-per-human."""
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    key.zero_counter = True  # a synced passkey: the counter shows no replay
    assertion = key.assertion(browser.challenge(intent, "reject").text)
    assert browser.decide(intent, "reject", assertion=assertion).status_code == 303
    assert browser.decide(intent, "reject", assertion=assertion).status_code == 403  # challenge used: refused
    assert decisions(owner) == 1


def test_assertion_from_another_origin_or_another_human(
    browser: Any, make_browser: Any, passkey: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    intent = new_intent(propose, row_of, tool=WIRE)
    key = passkey(browser.login(HUMAN1))
    options = browser.challenge(intent).text
    assert browser.decide(intent, assertion=key.assertion(options, origin="https://evil.example")).status_code == 403
    other = make_browser().login(HUMAN2)
    passkey(other)
    options = other.challenge(intent).text
    assert other.decide(intent, assertion=key.assertion(options)).status_code == 403  # HUMAN1's passkey
    assert decisions(owner) == 0


def test_passkey_not_enrolled_through_the_decider(
    browser: Any, propose: Any, row_of: Any, owner: Any, soft_authenticator: Any
) -> None:
    """AC-M10-08: a credential written around the decider (M9 ui, or a compromised ui with old rights) is refused."""
    import json

    from webauthn.helpers import base64url_to_bytes, parse_attestation_object

    intent = new_intent(propose, row_of)
    browser.login(HUMAN1)
    key = soft_authenticator()
    reg = json.loads(key.register('{"challenge": "AAAA"}'))
    att = parse_attestation_object(base64url_to_bytes(reg["response"]["attestationObject"]))
    assert att.auth_data.attested_credential_data is not None
    public_key = att.auth_data.attested_credential_data.credential_public_key
    owner.execute(
        "INSERT INTO webauthn_credentials (credential_id, human_principal, public_key, sign_count, created_at) "
        "VALUES (%s, %s, %s, 0, now())",
        (key.credential_id, HUMAN1, public_key),
    )
    assert browser.challenge(intent).status_code == 403  # no enrolled passkey: no options
    owner.execute(  # with an enrolled one next to it, the foreign credential's valid signature is still refused
        "INSERT INTO webauthn_credentials VALUES ('\\x0102', %s, '\\x00', 0, now(), 'oidc:op')", (HUMAN1,)
    )
    options = browser.challenge(intent).text
    assert browser.decide(intent, assertion=key.assertion(options)).status_code == 403
    assert decisions(owner) == 0


def test_reject_needs_a_passkey_too(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """AS-30 (M10): «каждое решение (approve и reject…)» is signed; T4 no longer skips it."""
    intent = new_intent(propose, row_of, tool=WIRE)
    browser.login(HUMAN1)
    assert browser.decide(intent, "reject", sign=False).status_code == 403
    assert browser.decide(intent, "reject").status_code == 303
    assert row_of(intent["intent_id"])["status"] == "rejected"
    assert owner.execute("SELECT webauthn_assertion ? 'signature' FROM decisions").fetchall() == [(True,)]


def test_no_passkey_is_shown(browser: Any, propose: Any, row_of: Any) -> None:
    """AS-17: an approver without a passkey sees why nothing can be decided."""
    intent = new_intent(propose, row_of, tool=WIRE)
    page = browser.login(HUMAN1).client.get(f"/intents/{intent['intent_id']}").text
    assert 'id="no-passkey"' in page and 'id="approve"' not in page and 'id="reject"' not in page


def test_ui_role_cannot_call_gxg_decide(pg: Any, propose: Any, row_of: Any) -> None:
    """AC-M10-08: `EXECUTE gxg_decide` only for gxg_decider; the ui's role gets a permission error."""
    intent = new_intent(propose, row_of)
    with pg.connect("gxg_ui") as ui:
        for sql in ("SELECT gxg_decide('\\x01', '\\x01', 0, null, '{}')",
                    "SELECT gxg_decision_challenge('oidc:x', %s, 'approve', 1, %s)",
                    "SELECT _gxg_decide(%s, 'oidc:x', 'approve', 1, %s, null, '{}')"):  # fmt: skip
            try:
                args = () if "\\x01" in sql else (intent["intent_id"], intent["canonical_args_hash"])
                ui.execute(sql, args)
                raise AssertionError(f"gxg_ui ran {sql}")
            except psycopg.errors.InsufficientPrivilege:
                pass


def test_database_refuses_a_human_outside_the_frozen_set(decide_db: Any, propose: Any, row_of: Any) -> None:
    """Even the decider records no decision of a non-approver or for another tenant (gxg_decide re-checks T2)."""
    intent = new_intent(propose, row_of)
    for who in ("oidc:https://idp.example.invalid:intruder", BETA_HUMAN, VIEWER):
        try:
            decide_db(intent["intent_id"], who, "approve", 1, intent["canonical_args_hash"])
            raise AssertionError(f"{who} decided")
        except psycopg.Error as exc:
            assert exc.sqlstate == "GX403"


def test_database_refuses_rows_t1_would_never_build(decide_db: Any, propose: Any, row_of: Any, owner: Any) -> None:
    """Defence in depth for gxg_decide: no frozen set (NULL) means nobody; an agent in the set is still no human."""
    intent = new_intent(propose, row_of)
    cases = [(None, HUMAN2), (["agent:t-alpha:other"], "agent:t-alpha:other")]
    for allowed, who in cases:
        owner.execute("UPDATE intents SET allowed_approvers = %s WHERE intent_id = %s", (allowed, intent["intent_id"]))
        try:
            decide_db(intent["intent_id"], who, "approve", 1, intent["canonical_args_hash"])
            raise AssertionError(f"{who} decided with allowed_approvers={allowed}")
        except psycopg.Error as exc:
            assert exc.sqlstate == "GX403"
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
