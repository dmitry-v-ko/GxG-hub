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


def test_relink_moves_notifications_to_the_new_account(notifier: Any, telegram: Any, owner: Any, pg: Any) -> None:
    for tg in (TG1, TG2):
        telegram.push_text(tg, f"/link {code_for(HUMAN1, owner, pg)}")
        notifier.poll(None if tg == TG1 else 2)
    assert owner.execute("SELECT telegram_user_id FROM approver_links").fetchall() == [(TG2,)]


def test_exact_texts(notifier: Any, telegram: Any, propose: Any, owner: Any, pg: Any) -> None:
    """AS-19 message shape, digest, replies: compared whole."""
    from gxg_approval_notifier import bot

    link(owner, HUMAN1, TG1)
    doc = propose().json()
    notifier.deliver()
    url = f"{UI_URL}/intents/{doc['intent_id']}"
    assert telegram.sent[-1]["text"] == (
        f"{bot.TEXT_NEW}\nTenant: t-alpha\nЧто: Send a message\nКому: Board b1\nЗапросил: agent:t-alpha:bot\n"
        f"Срок решения: {doc['expires_at']}\n{url}"
    )
    telegram.push_text(TG2, "/start")
    telegram.push_text(TG2, "/link 000000")
    telegram.push_text(TG2, f"/link {code_for(HUMAN2, owner, pg)}")
    notifier.poll(None)
    assert [m["text"] for m in telegram.sent[-3:]] == [bot.TEXT_START, bot.TEXT_NOT_LINKED, bot.TEXT_LINKED]
    for _ in range(6):
        propose(args={"text": random_text(), "n": 1})
    notifier.deliver()
    assert telegram.sent[-1]["text"] == f"Новых заявок больше 5 за 10 минут. Список: {UI_URL}/"


UI_URL = "https://ui.example.test"


def test_trailing_slash_of_the_ui_url(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    notifier.ui_url = UI_URL + "/"
    link(owner, HUMAN1, TG1)
    doc = propose().json()
    notifier.deliver()
    assert telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"] == f"{UI_URL}/intents/{doc['intent_id']}"


def test_window_boundary(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    """AP-08: «за 10 минут» — exactly 600 s later the first message has left the window; so has the digest."""
    link(owner, HUMAN1, TG1)
    for i in range(6):
        propose(args={"text": f"w{i}", "n": i})
        notifier.deliver()
    assert len(telegram.sent) == 6 and telegram.sent[-1]["reply_markup"]["inline_keyboard"][0][0]["url"] == UI_URL + "/"
    notifier.clock.now += 600
    for i in range(6):
        propose(args={"text": f"v{i}", "n": i})
        notifier.deliver()
    urls = [m["reply_markup"]["inline_keyboard"][0][0]["url"] for m in telegram.sent[6:]]
    assert len(urls) == 6 and urls[-1] == UI_URL + "/" and all("/intents/" in u for u in urls[:5])


def test_windows_are_per_chat(notifier: Any, telegram: Any, propose: Any, owner: Any) -> None:
    link(owner, HUMAN1, TG1)
    for i in range(6):
        propose(args={"text": f"a{i}", "n": i})
    notifier.deliver()
    link(owner, HUMAN2, TG2)
    doc = propose(args={"text": "b", "n": 100}).json()
    notifier.deliver()
    (m,) = sent_to(telegram, TG2)
    assert m["reply_markup"]["inline_keyboard"][0][0]["url"] == f"{UI_URL}/intents/{doc['intent_id']}"


def test_failed_send_is_logged_once(notifier: Any, telegram: Any, propose: Any, owner: Any, caplog: Any) -> None:
    """EFF-14: a refused send (user blocked the bot) is logged; others still get theirs; the row is done."""
    link(owner, HUMAN1, TG1)
    link(owner, HUMAN2, TG2)
    telegram.blocked_chats.add(TG1)
    doc = propose().json()
    assert notifier.deliver() == 1
    assert [m["chat_id"] for m in telegram.sent] == [TG2]
    assert ("gxg_approval_notifier", 30, f"notify failed for intent {doc['intent_id']}: sendMessage: 403 Forbidden: "
            "bot was blocked by the user") in caplog.record_tuples  # fmt: skip
    assert notifier.deliver() == 0


def test_bot_api_errors_do_not_leak_the_token(pg: Any, caplog: Any) -> None:
    import httpx

    from gxg_approval_notifier.bot import Bot, BotError

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    bot = Bot(httpx.Client(base_url="https://api.telegram.org", transport=httpx.MockTransport(broken)), "1:secret")
    try:
        bot.call("getMe")
        raise AssertionError("no error")
    except BotError as exc:
        assert str(exc) == "getMe: ConnectError"
    bad = Bot(
        httpx.Client(base_url="https://x", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<"))), "t"
    )
    try:
        bad.call("getMe")
        raise AssertionError("no error")
    except BotError as exc:
        assert str(exc) == "getMe: JSONDecodeError"
    long = "x" * 300
    api = Bot(httpx.Client(base_url="https://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "error_code": 400, "description": long}))), "t")  # fmt: skip
    try:
        api.call("sendMessage", chat_id=1, text="t")
        raise AssertionError("no error")
    except BotError as exc:
        assert str(exc) == "sendMessage: 400 " + "x" * 100
    api = Bot(httpx.Client(base_url="https://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False}))), "t")  # fmt: skip
    try:
        api.call("getMe")
        raise AssertionError("no error")
    except BotError as exc:
        assert str(exc) == "getMe: None "


def test_timeouts(notifier: Any, telegram: Any) -> None:
    """Long poll: Telegram holds the request 25 s, the HTTP client waits 10 s longer; other calls 10 s."""
    import httpx

    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path.rsplit("/", 1)[1], request.extensions["timeout"]["read"]))
        status, doc = telegram.handle(request.url.path, __import__("json").loads(request.content or b"{}"))
        return httpx.Response(status, json=doc)

    notifier.bot._client = httpx.Client(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler))
    telegram.push_text(TG1, "/start")
    notifier.poll(1)
    assert seen == [("getUpdates", 35), ("sendMessage", 10)]
    assert telegram.bodies[0] == ("getUpdates", {"timeout": 25, "allowed_updates": ["message"], "offset": 1})


def test_update_failure_is_logged_and_skipped(notifier: Any, telegram: Any, caplog: Any) -> None:
    import psycopg

    def down() -> Any:
        raise psycopg.OperationalError("db down")

    notifier.connect = down
    telegram.push_text(TG1, "/link 123456")
    telegram.push_text(TG1, "/start")
    assert notifier.poll(None) == 3  # the failed update is confirmed too: it is not retried forever
    assert ("gxg_approval_notifier", 30, "update 1 not handled: OperationalError") in caplog.record_tuples
    assert telegram.sent[-1]["chat_id"] == TG1  # the next update was still answered


def test_messages_without_text_are_ignored(notifier: Any, telegram: Any) -> None:
    telegram.push({"message": {"message_id": 1, "date": 0, "chat": {"id": TG1, "type": "private"},
                               "from": {"id": TG1, "is_bot": False, "first_name": "T"}, "sticker": {}}})  # fmt: skip
    telegram.push({"message": {"message_id": 2, "date": 0, "chat": {"id": TG1, "type": "private"},
                               "from": {"id": "1001"}, "text": "/start"}})  # fmt: skip
    notifier.poll(None)
    assert telegram.sent == []


def test_refused_claims_are_counted_on_an_index(owner: Any) -> None:
    owner.execute("SET enable_seqscan = off")  # empty table: ask whether the index can serve the query at all
    try:
        plan = owner.execute(
            "EXPLAIN SELECT count(*) FROM audit_events WHERE action = 'telegram_link_refused' "
            "AND at > now() - interval '10 minutes' AND details->>'telegram_user_id' = '1'"
        ).fetchall()
    finally:
        owner.execute("RESET enable_seqscan")
    assert any("audit_events_at_idx" in r[0] for r in plan), plan
