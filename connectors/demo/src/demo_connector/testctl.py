"""Test controls (GXG_PROFILE=test, DEMO_TEST_CONTROLS=1, 127.0.0.1 only). Not shipped in the production image."""

from __future__ import annotations

from typing import cast, get_args

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .backend import Backend, IndexFailure

FAILURES = get_args(IndexFailure)


def create_app(backend: Backend) -> Starlette:
    async def index_fail(request: Request) -> JSONResponse:
        """Break `index` with the given error (default temporarily_unavailable); `{"error": null}` repairs it."""
        body = await request.json() if await request.body() else {}
        error = body.get("error", FAILURES[0]) if isinstance(body, dict) else ""
        if error is not None and error not in FAILURES:
            return JSONResponse({"error": f"expected one of {FAILURES} or null"}, status_code=400)
        backend.index_failure = cast(IndexFailure | None, error)
        return JSONResponse({"index_failure": error})

    return Starlette(routes=[Route("/_test/index/fail", index_fail, methods=["POST"])])
