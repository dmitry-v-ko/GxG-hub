"""`gxg-approval-notifier`: process wiring. Secrets are files (AS-28): the database DSN, the bot token, and the
webhook secrets directory (`GXG_WEBHOOK_SECRETS_DIR`, `secret://<path>` → `<dir>/<path>`). Bundle: `GXG_BUNDLE_*`."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import httpx
import psycopg

from gxg_connector_kit.grants import bundle_store_from_env

from .bot import Bot, BotError, Notifier
from .webhooks import Webhooks

log = logging.getLogger("gxg_approval_notifier")


def main() -> None:
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
    connect = lambda: psycopg.connect(dsn, autocommit=True)  # noqa: E731
    notifier = Notifier(Bot(client, token), connect, env("GXG_UI_PUBLIC_URL"))
    hooks = Webhooks(connect, bundle_store_from_env(os.environ), Path(env("GXG_WEBHOOK_SECRETS_DIR")), httpx.Client())

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
            if hooks.bundles.readiness().ready:  # no bundle: events wait in the outbox (GR-12)
                hooks.fan_out()
                hooks.deliver()
        except psycopg.Error as exc:
            log.warning("outbox: database error %s", type(exc).__name__)
        time.sleep(interval)
