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
from typing import Any, Literal

from gxg_connector_kit.effects import Wiring
from gxg_connector_kit.mcp import Settings

FailureMode = str  # rate_limited | temporarily_unavailable | crash | commit_then_drop; None repairs
READ_VERBS = {"search", "get", "list", "read", "collect"}
EXT_VERBS = {"describe", "call"}  # MCC-01, MCC-11: only as the extension meta-tools `<domain>.ext.<verb>`
REGISTRY_VERBS = {"search", "describe"}  # MCC-11: the meta-tools that read the registry, not the provider


def read_verb_ok(name: str) -> bool:
    """MCC-01 (checked by MC-01): the verb of a `read` tool is a read verb, or `<domain>.ext.describe`/`.call`."""
    parts = name.split(".")
    return parts[-1] in READ_VERBS or (len(parts) == 3 and parts[1] == "ext" and parts[2] in EXT_VERBS)


def registry_read(name: str) -> bool:
    """MCC-11: `<domain>.ext.search` and `<domain>.ext.describe` read the connector's registry of capabilities
    (entries, their schema and class), not provider resources. SC-03 and SC-10 do not ask them for a provider call,
    resource ids or a result that differs per tenant; every other rule applies. `<domain>.ext.call` is not one of
    them: it runs an entry and passes the checks of an ordinary tool (MCC-11 п.4)."""
    parts = name.split(".")
    return len(parts) == 3 and parts[1] == "ext" and parts[2] in REGISTRY_VERBS


def item_key(tool: str, items: dict[str, Any]) -> str:
    """The identity of a list item: `id` (search-result.schema.json, MCC-04). A `collect` tool whose items have no
    `id` names each item by its dedupe key, `key` (service-integration-v1 «Участники и профили»: elements
    `person_candidate.v1`, «ключ dedupe — `telegram:<connection>/user/<user_ref>`»)."""
    required = items.get("items", {}).get("required", [])
    if "id" in required:
        return "id"
    if tool.rsplit(".", 1)[1] == "collect" and "key" in required:
        return "key"
    raise AssertionError(f"{tool}: a list item requires `id`")  # a red check, as the asserts of checks.py


@dataclass(frozen=True)
class Call:
    tool: str
    args: dict[str, Any]  # a valid call of `Harness.reader` (or `actor` for propose) with a non-empty result
    missing: dict[str, Any] | None = None  # names a resource that does not exist (single; list with an *_ids filter)
    hidden: dict[str, Any] | None = None  # names an existing resource the caller has no grant on (same)
    empty: dict[str, Any] | None = None  # list tools: a valid call that finds nothing (an empty result is a success)
    caller: Literal["reader", "operator"] = "reader"  # "operator": the tool answers only Harness.operator


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
    reader: str  # has `read` on part of its tenant's resources (every SC check of grants runs as the reader)
    nobody: str  # same tenant, no grant at all
    stranger: str  # another tenant, with grants on its own resources
    calls: list[Call]  # at least one per public read tool; a list call's args find at least 2 items (pagination)
    secret_env: str  # provider credential variable: its value never appears in a response or a log (SC-09)
    rids: Callable[[str, dict[str, Any]], set[str]]  # RIDs a result of a tool refers to (SC-10)
    actor: str | None = None  # effects: has `read` and `propose` on the resource of `propose`
    operator: str | None = None  # operator-only tools (Call.caller == "operator"): the principal they answer
    propose: Call | None = None  # effects: a valid `propose` of `actor`
    failures: tuple[FailureMode, ...] = field(default=("rate_limited", "temporarily_unavailable", "crash"))
