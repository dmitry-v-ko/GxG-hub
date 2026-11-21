"""Notify bot (EFF-13, EFF-14, AS-18…AS-20, AP-08).

Two loops of one process:
- outbox → Telegram: each `notify` row (written at T1, AS-19 fields only) goes once to every linked approver of
  the intent, then is marked delivered. A failed send is logged and not repeated: the bot is a hint (EFF-14), the
  list in the UI is the source, and «не больше одного уведомления на intent» (AP-08) holds.
- long polling `getUpdates` (no public webhook endpoint): only `/start` and `/link <code>` do anything; every
  other message, callback or button press is ignored (AS-18, EX-15). Messages carry a URL button only (AS-20).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg

log = logging.getLogger("gxg_approval_notifier")
WINDOW_S = 600  # AP-08: «больше 5 новых заявок за 10 минут» → one digest
MAX_SINGLE = 5
POLL_TIMEOUT_S = 25
LINK = re.compile(r"/link(?:@\w+)?\s+(\d{6})")
START = re.compile(r"/start(?:@\w+)?(?:\s.*)?", re.DOTALL)

_SQL_PENDING = (
    "SELECT id, payload FROM outbox WHERE topic = 'notify' AND delivered_at IS NULL AND available_at <= now() "
    "ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED"
)
_SQL_CHATS = (
    "SELECT telegram_user_id FROM approver_links WHERE tenant_id = %s AND human_principal = ANY(%s) "
    "ORDER BY telegram_user_id"
)
_SQL_DELIVERED = "UPDATE outbox SET delivered_at = now(), attempts = attempts + 1 WHERE id = %s"
_SQL_CLAIM = "SELECT gxg_link_claim(%s, %s)"


class BotError(Exception):
    pass


class Bot:
    """Telegram Bot API: the two methods the notifier needs."""

    def __init__(self, client: httpx.Client, token: str) -> None:
        self._client, self._prefix = client, f"/bot{token}/"

    def call(self, method: str, http_timeout: float = 10, **params: Any) -> Any:
        try:
            doc = self._client.post(self._prefix + method, json=params, timeout=http_timeout).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BotError(f"{method}: {type(exc).__name__}") from None  # never the URL: it holds the token
        if not doc.get("ok"):
            raise BotError(f"{method}: {doc.get('error_code')} {doc.get('description', '')[:100]}")
        return doc["result"]

    def send(self, chat_id: int, text: str, url: str) -> None:
        button = {"text": "Открыть в GxG Approval", "url": url}  # AS-20: a URL, never callback_data
        self.call("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": [[button]]})


@dataclass
class Throttle:
    """AP-08 per chat. shortcut: in memory, one instance (Telegram allows one getUpdates consumer per token);
    a restart forgets the window, at most 5 extra hints; move to a table if the bot ever runs in more instances."""

    singles: dict[int, deque[float]] = field(default_factory=dict)
    digests: dict[int, float] = field(default_factory=dict)

    def offer(self, chat: int, now: float) -> str | None:
        """'single', 'digest' or None (covered by a digest of the window)."""
        sent = self.singles.setdefault(chat, deque())
        while sent and now - sent[0] >= WINDOW_S:
            sent.popleft()
        if len(sent) < MAX_SINGLE:
            sent.append(now)
            return "single"
        last = self.digests.get(chat)
        if last is None or now - last >= WINDOW_S:
            self.digests[chat] = now
            return "digest"
        return None


def message(event: Mapping[str, Any]) -> str:
    """AS-19: tenant, title, target label, requester, deadline, link. No fields, changes, amounts or args."""
    return (
        "Новая заявка на согласование\n"
        f"Tenant: {event['tenant_id']}\n"
        f"Что: {event['title']}\n"
        f"Кому: {event['target_label']}\n"
        f"Запросил: {event['requester']}\n"
        f"Срок решения: {event['expires_at']}"
    )


@dataclass
class Notifier:
    bot: Bot
    connect: Callable[[], psycopg.Connection[Any]]
    ui_url: str
    clock: Callable[[], float] = time.time
    throttle: Throttle = field(default_factory=Throttle)

    def deliver(self) -> int:
        """One outbox pass; returns the number of rows handled."""
        with self.connect() as conn, conn.transaction():
            rows = conn.execute(_SQL_PENDING).fetchall()
            for row_id, event in rows:
                chats = [r[0] for r in conn.execute(_SQL_CHATS, (event["tenant_id"], event.get("approvers") or []))]
                for chat in chats:
                    self._notify(chat, event)
                conn.execute(_SQL_DELIVERED, (row_id,))
        return len(rows)

    def _notify(self, chat: int, event: Mapping[str, Any]) -> None:
        kind = self.throttle.offer(chat, self.clock())
        base = self.ui_url.rstrip("/")
        try:
            if kind == "single":
                url = f"{base}/intents/{event['intent_id']}"
                self.bot.send(chat, message(event) + f"\n{url}", url)
            elif kind == "digest":
                self.bot.send(chat, f"Новых заявок больше {MAX_SINGLE} за 10 минут. Список: {base}/", f"{base}/")
        except BotError as exc:
            log.warning("notify failed for intent %s: %s", event.get("intent_id"), exc)

    def handle(self, update: Mapping[str, Any]) -> None:
        msg = update.get("message") or {}
        chat, sender, text = msg.get("chat") or {}, msg.get("from") or {}, (msg.get("text") or "").strip()
        if chat.get("type") != "private" or not isinstance(sender.get("id"), int):
            return  # a link is only for a private chat with the person
        if m := LINK.fullmatch(text):
            with self.connect() as conn:
                human = (conn.execute(_SQL_CLAIM, (m[1], sender["id"])).fetchone() or (None,))[0]
            reply = (
                "Аккаунт привязан, уведомления придут сюда."
                if human
                else "Код не подошёл: он неверный, истёк или уже использован."
            )
            self.bot.call("sendMessage", chat_id=chat["id"], text=reply)
        elif START.fullmatch(text):
            self.bot.call(
                "sendMessage",
                chat_id=chat["id"],
                text="Это бот уведомлений GxG Approval. Решения принимаются только в веб-интерфейсе. "
                "Чтобы получать уведомления, получите код на странице «Telegram» и пришлите /link <код>.",
            )

    def poll(self, offset: int | None) -> int | None:
        """One long poll; returns the next offset."""
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for update in self.bot.call("getUpdates", http_timeout=POLL_TIMEOUT_S + 10, **params):
            offset = int(update["update_id"]) + 1
            try:
                self.handle(update)
            except (BotError, psycopg.Error) as exc:
                log.warning("update %s not handled: %s", update.get("update_id"), type(exc).__name__)
        return offset


def main() -> None:  # pragma: no cover (process wiring; exercised by the compose stand)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    def env(name: str, default: str | None = None) -> str:
        value = os.environ.get(name, default)
        if not value:
            raise SystemExit(f"missing configuration: {name}")
        return value

    dsn = Path(env("GXG_DB_DSN_FILE")).read_text("utf-8").strip()
    token = Path(env("GXG_TELEGRAM_TOKEN_FILE")).read_text("utf-8").strip()
    interval = float(env("GXG_NOTIFY_INTERVAL_S", "2"))
    client = httpx.Client(base_url=env("GXG_TELEGRAM_API_URL", "https://api.telegram.org"))
    notifier = Notifier(Bot(client, token), lambda: psycopg.connect(dsn, autocommit=True), env("GXG_UI_PUBLIC_URL"))

    def updates() -> None:
        offset = None
        while True:
            try:
                offset = notifier.poll(offset)
            except BotError as exc:
                log.warning("getUpdates: %s", exc)
                time.sleep(5)

    threading.Thread(target=updates, name="telegram-updates", daemon=True).start()
    while True:
        try:
            notifier.deliver()
        except psycopg.Error as exc:
            log.warning("outbox: database error %s", type(exc).__name__)
        time.sleep(interval)
