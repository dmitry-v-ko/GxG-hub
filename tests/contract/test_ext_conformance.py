"""MC-06…MC-10 (AC-M11.2-02, -04) run against a minimal fake ext.* connector, plus mutants.

A real connector (M11.3) reuses `ext_suite` by overriding the `ext_factory` and
`ext_start` fixtures. Each mutation of the fake must make the matching check fail.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ext_suite as s
import pytest
import yaml

from gxg_connector_kit.capabilities import Registry, capability_group, load_registry
from gxg_connector_kit.errors import GxgError

ROOT = Path(__file__).resolve().parents[2]
DOC = yaml.safe_load((ROOT / "schemas/examples/telegram.gxg-capabilities.yaml").read_text("utf-8"))


def _doc() -> dict[str, Any]:
    d = copy.deepcopy(DOC)
    d["capabilities"] = [
        {**c, "enabled_default": False} if c["effect_class"] == "read" else c for c in d["capabilities"]
    ]
    d["capabilities"][1]["group"] = "history"
    d["capabilities"].append(
        {**copy.deepcopy(d["capabilities"][1]), "name": "chats.members", "description": "Участники"}
    )
    return d


class FakeExt:
    """Minimal ext.call pipeline. `skip_*`/`enabled_default_true`/`audit_ext_call_only` are mutations."""

    def __init__(
        self,
        doc: dict[str, Any],
        *,
        skip_chat_check: bool = False,
        skip_non_read_refusal: bool = False,
        enable_all: bool = False,
        audit_ext_call_only: bool = False,
    ) -> None:
        self.registry: Registry = load_registry(doc)
        self.provider_calls: list[tuple[str, dict[str, Any]]] = []
        self.audit: list[dict[str, Any]] = []
        self.flood = 0
        self.owner = {s.CHAT_OK: s.T1, s.CHAT_NOT_GRANTED: s.T1, s.CHAT_OTHER_TENANT: s.T2}
        self.granted = {(s.T1, s.P): {s.CHAT_OK}}
        self.enabled = {s.T1: {"chats.history"}}  # per-tenant decision on top of enabled_default
        self.skip_chat_check, self.skip_non_read = skip_chat_check, skip_non_read_refusal
        self.enable_all, self.audit_ext_call_only = enable_all, audit_ext_call_only

    def set_flood_wait(self, seconds: int) -> None:
        self.flood = seconds

    def search(self, tenant: str, principal: str, query: str) -> list[str]:
        # Q6: only entries that are enabled for the tenant (and are read) are offered
        return [
            n
            for n, c in self.registry.entries.items()
            if (query in n or query == "") and c.effect_class == "read" and n in self.enabled.get(tenant, set())
        ]

    def call(self, tenant: str, principal: str, name: str, args: dict[str, Any]) -> Any:
        cap = self.registry.get(name)
        if cap is None:
            raise GxgError("not_found", "unknown capability")
        chat = str(args.get("chat_id"))
        if not self.skip_chat_check:
            if self.owner.get(chat) != tenant:
                raise GxgError("not_found", "chat not found")
            if chat not in self.granted.get((tenant, principal), set()):
                raise GxgError("resource_forbidden", "chat is outside the grant")
        if cap.effect_class != "read" and not self.skip_non_read:
            raise GxgError("permission_denied", f"use {cap.propose_tool}", details={"propose_tool": cap.propose_tool})
        enabled = cap.enabled_default or self.enable_all or name in self.enabled.get(tenant, set())
        if not enabled:
            raise GxgError("effect_disabled", "capability is off for this tenant")
        self.audit.append(
            {
                "capability": "ext.call" if self.audit_ext_call_only else name,
                "version": cap.version,
                "group": capability_group(cap),
                "tenant": tenant,
                "principal": principal,
                "result": "ok",
            }
        )
        if self.flood:
            raise GxgError("rate_limited", "flood wait", retry_after_s=self.flood)
        self.provider_calls.append((name, {"chat_id": chat}))
        return {"items": []}


@pytest.fixture
def ext_factory() -> s.Factory:
    return lambda **mut: FakeExt(_doc(), **mut)


@pytest.fixture
def ext_start() -> s.StartFn:
    return lambda doc: FakeExt(doc)  # type: ignore[arg-type]  # noqa: PLW0108


def test_mc06_registry_validation(ext_start: s.StartFn) -> None:
    s.check_mc06(ext_start, _doc())


@pytest.mark.parametrize("check", [s.check_mc07, s.check_mc08, s.check_mc09, s.check_mc10])
def test_mc07_to_mc10_pass_on_conforming_connector(ext_factory: s.Factory, check: Callable[..., None]) -> None:
    check(ext_factory())


# AC-M11.2-04: each mutation makes the matching check fail.
MUTANTS = [
    pytest.param(s.check_mc07, {"skip_chat_check": True}, id="no-chat-check"),
    pytest.param(s.check_mc08, {"skip_non_read_refusal": True}, id="no-non-read-refusal"),
    pytest.param(s.check_mc09, {"enable_all": True}, id="enabled-default-true"),
    pytest.param(s.check_mc10, {"audit_ext_call_only": True}, id="no-entry-name-in-audit"),
]


@pytest.mark.parametrize(("check", "mutation"), MUTANTS)
def test_mutation_is_caught(ext_factory: s.Factory, check: Callable[..., None], mutation: dict[str, bool]) -> None:
    with pytest.raises(AssertionError):
        check(ext_factory(**mutation))


def test_mc06_mutant_loader_accepting_anything_is_caught() -> None:
    class Lax:
        def __init__(self, doc: object) -> None:
            pass

    with pytest.raises(AssertionError):
        s.check_mc06(Lax, _doc())
