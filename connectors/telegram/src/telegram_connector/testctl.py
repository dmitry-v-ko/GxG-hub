"""Test controls of the fake service (GXG_PROFILE=test, TELEGRAM_TEST_CONTROLS=1, 127.0.0.1 only).
Not shipped in the production image."""

from __future__ import annotations

import re
from typing import cast, get_args

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .fake import FakeTelegramService
from .service import Kind

KINDS = get_args(Kind)
_MODE = re.compile(r"(slow|flood_wait):([0-9]{1,6})")


def create_app(service: FakeTelegramService) -> Starlette:
    async def fail(request: Request) -> JSONResponse:
        """`{"mode": <kind> | "flood_wait:<s>" | "slow:<ms>" | null}` for every request to the service; null repairs."""
        body = await request.json() if await request.body() else {}
        mode = body.get("mode", "") if isinstance(body, dict) else ""
        m = _MODE.fullmatch(mode) if isinstance(mode, str) else None
        if mode is not None and mode not in KINDS and m is None:
            return JSONResponse(
                {"error": f"expected one of {KINDS}, flood_wait:<s>, slow:<ms> or null"}, status_code=400
            )
        service.failure = cast(Kind, mode) if mode in KINDS else ("flood_wait" if m and m[1] == "flood_wait" else None)
        service.retry_after_s = float(m[2]) if m and m[1] == "flood_wait" else 0.0
        service.delay_s = int(m[2]) / 1000 if m and m[1] == "slow" else 0.0
        return JSONResponse({"mode": mode})

    async def stats(_: Request) -> JSONResponse:
        return JSONResponse({"calls": dict(service.calls), "open_sessions": service.open_sessions})

    async def secrets(_: Request) -> JSONResponse:
        """What must never leave the service (AC-M11-05): fixture texts, peer IDs and session strings."""
        peers = [ch.peer_id for chats in service.chats.values() for ch in chats.values()]
        return JSONResponse({"texts": service.texts(), "peer_ids": peers, "sessions": list(service.sessions.values())})

    return Starlette(
        routes=[
            Route("/_test/service/fail", fail, methods=["POST"]),
            Route("/_test/service/stats", stats, methods=["GET"]),
            Route("/_test/service/secrets", secrets, methods=["GET"]),
        ]
    )
