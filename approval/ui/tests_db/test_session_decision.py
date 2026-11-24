"""EX-26 through the ui (AS-30a): the one-button decision of an intent without step-up, and what stays refused."""

from __future__ import annotations

from typing import Any

import psycopg

HUMAN1 = "oidc:https://idp.example.invalid:1"
BETA_HUMAN = "oidc:https://idp.example.invalid:beta"
WIRE = "demo.wire.propose"  # high_risk: step-up by default


def pending(propose: Any, row_of: Any, **kw: Any) -> dict[str, Any]:
    r = propose(**kw)
    assert r.status_code == 201, r.text
    row: dict[str, Any] = row_of(r.json()["intent_id"])
    return row


def press(browser: Any, intent: dict[str, Any], decision: str = "approve", **over: Any) -> Any:
    """The form of the card without an assertion: what the button sends."""
    body = {"csrf": browser.csrf, "version": intent["version"], "args_hash": intent["canonical_args_hash"],
            "decision": decision, **over}  # fmt: skip
    return browser.client.post(f"/intents/{intent['intent_id']}/decision", data=body)


def decisions(owner: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return owner.execute("SELECT decided_by, decision, step_up FROM decisions").fetchall()


def test_one_button_approve_and_reject(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    a, b = pending(propose, row_of), pending(propose, row_of)
    browser.login(HUMAN1)
    assert press(browser, a).status_code == 303 and row_of(a["intent_id"])["status"] == "approved"
    assert press(browser, b, "reject").status_code == 303 and row_of(b["intent_id"])["status"] == "rejected"
    assert decisions(owner) == [(HUMAN1, "approve", False), (HUMAN1, "reject", False)]


def test_the_card_has_the_buttons_without_webauthn(browser: Any, propose: Any, row_of: Any) -> None:
    intent = pending(propose, row_of)
    page = browser.login(HUMAN1).client.get(f"/intents/{intent['intent_id']}").text
    assert 'id="decide-session"' in page and 'id="approve"' in page and 'id="reject"' in page
    assert 'name="assertion"' not in page and "data-challenge" not in page


def test_a_step_up_intent_keeps_the_passkey_path(browser: Any, propose: Any, row_of: Any, owner: Any) -> None:
    intent = pending(propose, row_of, tool=WIRE, args={"text": "wire", "n": 9})
    browser.login(HUMAN1)
    assert press(browser, intent).status_code == 403  # no assertion: the decider's path is the only one
    page = browser.client.get(f"/intents/{intent['intent_id']}").text
    assert "data-challenge" in page and 'id="decide-session"' not in page
    assert decisions(owner) == [] and row_of(intent["intent_id"])["status"] == "pending_approval"


def test_csrf_foreign_approver_and_stale_card_are_refused(
    browser: Any, make_browser: Any, propose: Any, row_of: Any, owner: Any
) -> None:
    intent = pending(propose, row_of)
    other = make_browser().login(BETA_HUMAN)
    browser.login(HUMAN1)
    assert press(browser, intent, csrf="").status_code == 403
    assert press(browser, intent, csrf=other.csrf).status_code == 403
    assert press(other, intent).status_code == 403  # an approver of another tenant: the intent looks like a missing one
    assert press(browser, intent, version=intent["version"] + 1).status_code == 409
    assert press(browser, intent, args_hash="0" * 64).status_code == 409
    assert decisions(owner) == [] and row_of(intent["intent_id"])["status"] == "pending_approval"
