"""What a connector gives the suite: how to start it in the test profile and which calls to make.

The connector exposes `harness() -> Harness` and names it in its pyproject.toml:

    [tool.gxg-connector-check]
    harness = "<package>.conformance:harness"

The suite builds the environment itself (signed test bundle from `fixtures/grants/`, test identity issuer,
fake Approval Service with an executor key) and calls `start(settings, wiring)` once per check.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gxg_connector_kit.effects import Wiring
from gxg_connector_kit.mcp import Settings

FailureMode = str  # rate_limited | temporarily_unavailable | crash | commit_then_drop; None repairs


@dataclass(frozen=True)
class Call:
    tool: str
    args: dict[str, Any]  # a valid call of `Harness.reader` (or `actor` for propose) with a non-empty result
    missing: dict[str, Any] | None = None  # names a resource that does not exist (single; list with an *_ids filter)
    hidden: dict[str, Any] | None = None  # names an existing resource the caller has no grant on (same)
    empty: dict[str, Any] | None = None  # list tools: a valid call that finds nothing (an empty result is a success)


@dataclass
class Running:
    """One started connector."""

    app: Any  # public MCP ASGI app (Streamable HTTP at /mcp, /healthz, /readyz)
    calls: Callable[[], int]  # provider (backend) calls so far: 0 in every refused case
    fail: Callable[[FailureMode | None], None]  # provider failure mode
    internal: Any = None  # internal ASGI app with /internal/v1/{execute,reconcile}/<tool> (effects only)


@dataclass(frozen=True)
class Harness:
    root: Path  # connector directory: gxg-manifest.yaml, fixtures/grants/*.yaml
    settings: type[Settings]  # the connector's kit Settings (its PREFIX names the variables)
    start: Callable[[Settings, Wiring], Running]
    reader: str  # has `read` on part of its tenant's resources
    nobody: str  # same tenant, no grant at all
    stranger: str  # another tenant, with grants on its own resources
    calls: list[Call]  # at least one per public read tool; a list call's args find at least 2 items (pagination)
    secret_env: str  # provider credential variable: its value never appears in a response or a log (SC-09)
    rids: Callable[[str, dict[str, Any]], set[str]]  # RIDs a result of a tool refers to (SC-10)
    actor: str | None = None  # effects: has `read` and `propose` on the resource of `propose`
    propose: Call | None = None  # effects: a valid `propose` of `actor`
    failures: tuple[FailureMode, ...] = field(default=("rate_limited", "temporarily_unavailable", "crash"))
