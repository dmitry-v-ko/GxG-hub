"""Harness of `demo` for `check-connectors` (gxg_connector_check): how to start it and which calls to make.

Imported only by the conformance suite (a dev dependency); not part of the service.
"""

from __future__ import annotations

import dataclasses
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
    if isinstance(settings, server.Settings):  # stage C: the fake provider's journal next to the bundle state
        settings = dataclasses.replace(settings, provider_db=settings.state_file.parent / "provider.sqlite3")
    demo_wiring = server.Wiring(approval=wiring.approval, jwks=wiring.jwks)
    app, connector = server.build(settings, Backend(scan_delay_s=0), demo_wiring)
    backend = connector.backend
    enter = backend._enter

    def crash(kind: str) -> None:  # the backend fails with an exception whose text holds the credential
        backend.calls[kind] += 1
        raise RuntimeError(f"backend exploded: {os.environ.get(SECRET_ENV)}")

    provider = connector.effects.provider if connector.effects is not None else None

    def fail(mode: str | None) -> None:
        backend.failure = mode if mode in BACKEND_FAILURES else None  # type: ignore[assignment]
        backend._enter = crash if mode == "crash" else enter  # type: ignore[method-assign]
        if provider is not None:  # stage C: the effect goes to demo's own fake provider (provider.py)
            provider.set_mode("commit_then_drop" if mode == "commit_then_drop" else "ok")

    def calls() -> int:  # backend reads plus provider sends
        return sum(backend.calls.values()) + (len(provider.calls()) if provider is not None else 0)

    return Running(app=app, calls=calls, fail=fail, internal=connector.internal)  # the app server.configs serves


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
            Call(
                "demo.notes.search",
                {"query": "status"},
                missing={"query": "status", "board_ids": ["no-such-board"]},
                hidden={"query": "status", "board_ids": ["a-secret"]},
                empty={"query": "no note has this text"},
            ),
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
        actor="agent:t-alpha:actor",  # read and propose on a-public (fixtures/grants)
        propose=Call("demo.message.propose", {"board_id": "a-public", "recipient": "partner_1", "text": "hello"}),
    )
