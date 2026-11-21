"""Test controls (GXG_PROFILE=test, DEMO_TEST_CONTROLS=1, 127.0.0.1 only). Not shipped in the production image."""

from __future__ import annotations

import re
from typing import cast, get_args

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backend import Backend, BackendFailure, IndexFailure

FAILURES = get_args(IndexFailure)
BACKEND_FAILURES = get_args(BackendFailure)
_SLOW = re.compile(r"slow:([0-9]{1,6})")


async def _body(request: Request) -> object:
    return await request.json() if await request.body() else {}


def create_app(backend: Backend) -> Starlette:
    async def index_fail(request: Request) -> JSONResponse:
        """Break `index` with the given error (default temporarily_unavailable); `{"error": null}` repairs it."""
        body = await _body(request)
        error = body.get("error", FAILURES[0]) if isinstance(body, dict) else ""
        if error is not None and error not in FAILURES:
            return JSONResponse({"error": f"expected one of {FAILURES} or null"}, status_code=400)
        backend.index_failure = cast(IndexFailure | None, error)
        return JSONResponse({"index_failure": error})

    async def backend_fail(request: Request) -> JSONResponse:
        """`{"mode": <error code> | "slow:<ms>" | null}`: every backend call fails or waits; null repairs it."""
        body = await _body(request)
        mode = body.get("mode", "") if isinstance(body, dict) else ""
        slow = _SLOW.fullmatch(mode) if isinstance(mode, str) else None
        if mode is not None and mode not in BACKEND_FAILURES and slow is None:
            return JSONResponse({"error": f"expected one of {BACKEND_FAILURES}, slow:<ms> or null"}, status_code=400)
        backend.failure = cast(BackendFailure, mode) if mode in BACKEND_FAILURES else None
        backend.delay_s = int(slow[1]) / 1000 if slow else 0.0
        return JSONResponse({"mode": mode})

    async def backend_calls(_: Request) -> JSONResponse:
        return JSONResponse(dict(backend.calls))

    return Starlette(
        routes=[
            Route("/_test/index/fail", index_fail, methods=["POST"]),
            Route("/_test/backend/fail", backend_fail, methods=["POST"]),
            Route("/_test/backend/calls", backend_calls, methods=["GET"]),
        ]
    )
