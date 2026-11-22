"""Harness of `demo` for `check-connectors` (gxg_connector_check): how to start it and which calls to make.

Imported only by the conformance suite (a dev dependency); not part of the service.
"""

from __future__ import annotations

import os
from typing import Any, get_args

from gxg_connector_check import Call, Harness, Running

from gxg_connector_kit.effects import Wiring
from gxg_connector_kit.mcp import Settings as KitSettings

from . import server
from .backend import Backend, BackendFailure
from .tools import ROOT, rid

SECRET_ENV = "DEMO_PROVIDER_TOKEN"  # noqa: S105 - the name of the variable, not a credential
BACKEND_FAILURES: tuple[str, ...] = get_args(BackendFailure)
MISSING_NOTE = "00000000-0000-4000-8000-000000000000"


def start(settings: KitSettings, wiring: Wiring) -> Running:
    app, connector = server.build(settings, Backend(scan_delay_s=0))
    backend = connector.backend
    enter = backend._enter

    def crash(kind: str) -> None:  # the backend fails with an exception whose text holds the credential
        backend.calls[kind] += 1
        raise RuntimeError(f"backend exploded: {os.environ.get(SECRET_ENV)}")

    def fail(mode: str | None) -> None:
        backend.failure = mode if mode in BACKEND_FAILURES else None  # type: ignore[assignment]
        backend._enter = crash if mode == "crash" else enter  # type: ignore[method-assign]

    return Running(app=app, calls=lambda: sum(backend.calls.values()), fail=fail)


def rids(tool: str, result: dict[str, Any]) -> set[str]:
    if tool == "demo.boards.list":
        return {rid("demo-main", b["id"]) for b in result["boards"]}
    if tool == "demo.notes.search":
        return {i["source_ref"] for i in result["items"]}
    return {rid("demo-main", result["board_id"])}


def harness() -> Harness:
    boards = Backend(scan_delay_s=0).boards["t-alpha"]
    return Harness(
        root=ROOT,
        settings=server.Settings,
        start=start,
        reader="agent:t-alpha:reader",
        nobody="agent:t-alpha:nobody",
        stranger="agent:t-beta:reader",
        calls=[
            Call("demo.boards.list", {}),
            Call("demo.notes.search", {"query": "status"}),
            Call(
                "demo.note.get",
                {"note_id": boards["a-public"].notes[0].id},
                missing={"note_id": MISSING_NOTE},
                hidden={"note_id": boards["a-secret"].notes[0].id},  # only an expired grant (GR-06)
            ),
        ],
        secret_env=SECRET_ENV,
        rids=rids,
        failures=(*BACKEND_FAILURES, "crash"),
    )
