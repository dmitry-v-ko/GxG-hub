"""AC-M9-07…AC-M9-10 (AS-18…AS-20, AP-08, EX-15) against the fake Bot API and a real PostgreSQL."""

from __future__ import annotations

import json
import secrets
import string
from typing import Any

import psycopg

HUMAN1, HUMAN2 = "oidc:https://idp.example.invalid:1", "oidc:https://idp.example.invalid:2"
TG1, TG2 = 1001, 1002


def link(owner: psycopg.Connection[Any], human: str, tg: int, tenant: str = "t-alpha") -> None:
    owner.execute("INSERT INTO approver_links VALUES (%s, %s, %s, now())", (human, tenant, tg))


def random_text(n: int = 40) -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def sent_to(telegram: Any, chat: int) -> list[dict[str, Any]]:
    return [m for m in telegram.sent if m["chat_id"] == chat]


def test_message_has_no_payload_and_links_the_card(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    """AC-M9-07: 20 intents with random text → 0 occurrences of fields/changes values; link to /intents/<id>."""
    link(owner, HUMAN1, TG1)
    secrets_in_payload, ids = [], []
    for _ in range(20):
        text, before, after, amount = random_text(), random_text(), random_text(), str(secrets.randbelow(10**9))
        secrets_in_payload += [text, before, after, amount]
        preview = {"schema_version": 1, "title": "Send a message", "target": {"label": "Board b1",
                   "rid": "demo:demo-main/board/b1"},
                   "fields": [{"label": "Text", "value": text, "kind": "multiline"},
                              {"label": "Amount", "value": amount, "kind": "amount"}],
                   "changes": [{"field": "stage", "before": before, "after": after}]}  # fmt: skip
        r = propose(args={"text": text, "n": 1}, preview=preview)
        assert r.status_code == 201, r.text
        ids.append(r.json()["intent_id"])
        notifier.deliver()
        notifier.clock.now += 121  # 5 per 10 minutes at most: every intent gets its own message
    messages = sent_to(telegram, TG1)
    assert len(messages) == 20
    dumped = json.dumps(messages, ensure_ascii=False)
    assert [s for s in secrets_in_payload if s in dumped] == []
    for intent_id in ids:
        url = f"{notifier.ui_url}/intents/{intent_id}"
        (m,) = [m for m in messages if url in m["text"]]
        assert m["reply_markup"] == {"inline_keyboard": [[{"text": "Открыть в GxG Approval", "url": url}]]}
        for needle in ("t-alpha", "Send a message", "Board b1", "agent:t-alpha:bot", "Срок решения"):
            assert needle in m["text"]  # AS-19
    assert owner.execute("SELECT count(*) FROM outbox WHERE topic = 'notify' AND delivered_at IS NULL").fetchone() == (
        0,
    )


def test_only_linked_approvers_of_the_intent(
    notifier: Any, telegram: Any, propose: Any, owner: Any, world: Any
) -> None:
    link(owner, HUMAN1, TG1)
    link(owner, HUMAN2, TG2)
    link(owner, HUMAN1, 3003, tenant="t-beta")  # a link in another tenant is not used for t-alpha
    world.set_policy("t-alpha", [{"id": "only2", "match": {"tools": ["demo.message.propose"]},
                                  "decision": "require_approval", "approvers": [HUMAN2]}])  # fmt: skip
    propose()
    propose(tool="demo.note.propose")  # write_reversible: no human, no notification
    notifier.deliver()
    assert [m["chat_id"] for m in telegram.sent] == [TG2]
    assert notifier.deliver() == 0  # delivered once (AP-08: one notification per intent)


def test_no_callback_buttons_and_commands_do_not_decide(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    """AC-M9-08, EX-15: URL buttons only; «approve <id>» and a forged callback change nothing."""
    link(owner, HUMAN1, TG1)
    intent = propose().json()["intent_id"]
    notifier.deliver()
    for m in telegram.sent:
        for row in m["reply_markup"]["inline_keyboard"]:
            for button in row:
                assert set(button) == {"text", "url"} and button["url"].startswith("https://")
    before = owner.execute("SELECT status, version FROM intents").fetchall()
    sent = len(telegram.sent)
    for text in (f"approve {intent}", f"/approve {intent}", "/link", "/link 12345", "hello"):
        telegram.push_text(TG1, text)
    telegram.push_callback(TG1, f"approve:{intent}")
    notifier.poll(None)
    assert owner.execute("SELECT status, version FROM intents").fetchall() == before == [("pending_approval", 1)]
    assert owner.execute("SELECT count(*) FROM decisions").fetchone() == (0,)
    assert len(telegram.sent) == sent  # ignored: not even a reply
    assert "callback_query" not in json.dumps([c for c in telegram.calls])


def test_long_polling_asks_only_for_messages(notifier: Any, telegram: Any) -> None:
    telegram.push_callback(TG1, "approve:x")
    assert notifier.poll(None) is None  # the callback update is not even delivered (allowed_updates)
    telegram.push_text(TG1, "/start")
    offset = notifier.poll(None)
    assert offset == 3 and "Решения принимаются только в веб-интерфейсе" in telegram.sent[-1]["text"]
    assert notifier.poll(offset) == offset  # confirmed updates are not delivered again
    assert len(telegram.sent) == 1


def test_digest_after_five(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    """AC-M9-09, AP-08: 8 intents within 2 minutes → at most 5 single messages + 1 digest."""
    link(owner, HUMAN1, TG1)
    for i in range(8):
        propose(args={"text": f"m{i}", "n": i})
        notifier.deliver()
        notifier.clock.now += 15
    messages = sent_to(telegram, TG1)
    singles = [m for m in messages if "/intents/" in m["reply_markup"]["inline_keyboard"][0][0]["url"]]
    digests = [m for m in messages if m["reply_markup"]["inline_keyboard"][0][0]["url"] == f"{notifier.ui_url}/"]
    assert (len(singles), len(digests), len(messages)) == (5, 1, 6)
    assert "больше 5" in digests[0]["text"]
    notifier.clock.now += 600  # a new window: single messages again
    propose(args={"text": "later", "n": 99})
    notifier.deliver()
    assert len(sent_to(telegram, TG1)) == 7 and "/intents/" in telegram.sent[-1]["text"]


def test_digest_per_approver(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    link(owner, HUMAN1, TG1)
    for i in range(6):
        propose(args={"text": f"a{i}", "n": i})
    notifier.deliver()
    link(owner, HUMAN2, TG2)
    propose(args={"text": "b", "n": 100})
    notifier.deliver()
    assert len(sent_to(telegram, TG1)) == 6 and len(sent_to(telegram, TG2)) == 1


def code_for(browser_human: str, owner: psycopg.Connection[Any], pg: Any) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    with pg.connect("gxg_ui") as ui:
        assert ui.execute("SELECT gxg_link_code(%s, %s, %s)", (browser_human, ["t-alpha"], code)).fetchone() == (True,)
    return code


def test_link_code_once_and_ten_minutes(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    """AC-M9-10: a code works once and for 10 minutes; reuse and an expired code are refused."""
    code = code_for(HUMAN1, owner, pg)
    telegram.push_text(TG1, f"/link {code}")
    offset = notifier.poll(None)
    assert "привязан" in telegram.sent[-1]["text"]
    assert owner.execute("SELECT human_principal, tenant_id, telegram_user_id FROM approver_links").fetchall() == [
        (HUMAN1, "t-alpha", TG1)
    ]
    telegram.push_text(TG2, f"/link {code}")  # reuse, by someone else
    offset = notifier.poll(offset)
    assert "не подошёл" in telegram.sent[-1]["text"]
    assert owner.execute("SELECT telegram_user_id FROM approver_links").fetchall() == [(TG1,)]
    expired = code_for(HUMAN2, owner, pg)
    owner.execute("UPDATE link_codes SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
    telegram.push_text(TG2, f"/link {expired}")
    notifier.poll(offset)
    assert "не подошёл" in telegram.sent[-1]["text"]
    assert owner.execute("SELECT count(*) FROM approver_links WHERE human_principal = %s", (HUMAN2,)).fetchone() == (0,)
    audit = owner.execute("SELECT actor, action FROM audit_events WHERE action LIKE 'telegram_%' ORDER BY id")
    assert audit.fetchall() == [
        (HUMAN1, "telegram_link_code"), (HUMAN1, "telegram_linked"), ("notifier", "telegram_link_refused"),
        (HUMAN2, "telegram_link_code"), ("notifier", "telegram_link_refused"),
    ]  # fmt: skip


def test_code_valid_just_before_ten_minutes(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    code = code_for(HUMAN1, owner, pg)
    owner.execute("UPDATE link_codes SET expires_at = now() + interval '2 seconds', created_at = now() - interval "
                  "'598 seconds'")  # fmt: skip
    telegram.push_text(TG1, f"/link {code}")
    notifier.poll(None)
    assert "привязан" in telegram.sent[-1]["text"]


def test_link_only_in_a_private_chat(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    code = code_for(HUMAN1, owner, pg)
    telegram.push_text(TG1, f"/link {code}", chat_type="group")
    notifier.poll(None)
    assert telegram.sent == [] and owner.execute("SELECT count(*) FROM approver_links").fetchone() == (0,)


def test_guessing_codes_is_limited(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    """Five wrong codes in 10 minutes block the sender; even the right code then fails."""
    code = code_for(HUMAN1, owner, pg)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(5):
        telegram.push_text(TG1, f"/link {wrong}")
    telegram.push_text(TG1, f"/link {code}")
    offset = notifier.poll(None)
    assert owner.execute("SELECT count(*) FROM approver_links").fetchone() == (0,)
    telegram.push_text(TG2, f"/link {code}")  # another sender is not blocked
    notifier.poll(offset)
    assert owner.execute("SELECT telegram_user_id FROM approver_links").fetchall() == [(TG2,)]


def test_new_code_replaces_the_old_one(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    old = code_for(HUMAN1, owner, pg)
    code_for(HUMAN1, owner, pg)
    telegram.push_text(TG1, f"/link {old}")
    notifier.poll(None)
    assert owner.execute("SELECT count(*) FROM approver_links").fetchone() == (0,)


def test_live_code_digits_are_not_given_twice(owner: Any, pg: Any) -> None:
    code = code_for(HUMAN1, owner, pg)
    with pg.connect("gxg_ui") as ui:
        assert ui.execute("SELECT gxg_link_code(%s, %s, %s)", (HUMAN2, ["t-alpha"], code)).fetchone() == (False,)
        for bad in (("agent:x", ["t-alpha"], "123456"), (HUMAN2, ["t-alpha"], "12345"), (HUMAN2, [], "123456")):
            try:
                ui.execute("SELECT gxg_link_code(%s, %s, %s)", bad)
                raise AssertionError(bad)
            except psycopg.Error as exc:
                assert exc.sqlstate == "GX400"
