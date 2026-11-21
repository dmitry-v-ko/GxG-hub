"""Notifier tests: the Approval Service fixtures (approval/tests_db/conftest.py) and the fake Bot API in process
(an httpx MockTransport over the same handler the compose test profile serves over HTTP; no TCP in tests)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:  # one copy per session (the ui tests load the fixtures too)
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("approval_db_fixtures", ROOT / "approval/tests_db/conftest.py")
fake = _load("fake_bot_api", ROOT / "approval/notifier/testing/fake_bot_api.py")
from approval_db_fixtures import *  # noqa: E402, F403
from approval_db_fixtures import Pg  # noqa: E402

from gxg_approval_notifier.bot import Bot, Notifier  # noqa: E402

TOKEN = "123456:test-only-token"
UI = "https://ui.example.test"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def telegram() -> Any:
    return fake.FakeBotApi(TOKEN, max_wait_s=0)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier(telegram: Any, pg: Pg, clock: Clock, owner: Any) -> Notifier:
    def handler(request: httpx.Request) -> httpx.Response:
        status, doc = telegram.handle(request.url.path, json.loads(request.content or b"{}"))
        return httpx.Response(status, json=doc)

    client = httpx.Client(base_url="https://api.telegram.org", transport=httpx.MockTransport(handler))
    return Notifier(Bot(client, TOKEN), lambda: pg.connect("gxg_notifier"), UI, clock=clock)
