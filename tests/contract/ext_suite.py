"""Reusable conformance checks MC-06…MC-10 for a connector with ext.* (MCC-11 п.7).

A connector plugs in by providing a factory (pytest fixture `ext_factory` in
test_ext_conformance.py) that builds an object satisfying `ExtConnector`. Each `check_*`
raises AssertionError when the connector violates the requirement.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from gxg_connector_kit.errors import GxgError

T1, T2, P = "tenant-1", "tenant-2", "agent-1"
CHAT_OK, CHAT_OTHER_TENANT, CHAT_NOT_GRANTED = "chat-ok", "chat-t2", "chat-denied"
SECRET_TEXT, SECRET_PEER = "secret message text 42", "peer-987654"


class ExtConnector(Protocol):
    """What the suite needs. Fixtures: T1 holds `telegram.ext:read` for CHAT_OK only;
    CHAT_OTHER_TENANT belongs to T2; CHAT_NOT_GRANTED to T1 but outside the grant.
    The registry has: `chats.history` (read, enabled for T1), `chats.members` (read,
    enabled_default false, not enabled for T1), `messages.delete` (external_effect)."""

    provider_calls: list[tuple[str, dict[str, Any]]]  # every call that reached the provider
    audit: list[dict[str, Any]]  # audit records written by ext.call and logs

    def search(self, tenant: str, principal: str, query: str) -> list[str]: ...
    def call(self, tenant: str, principal: str, name: str, args: dict[str, Any]) -> Any: ...
    def set_flood_wait(self, seconds: int) -> None: ...


Factory = Callable[..., ExtConnector]
# A factory for MC-06: starts a connector from a raw registry document (raises if it refuses).
StartFn = Callable[[object], object]

FORBIDDEN_FRAGMENTS = ("invoke", "2fa", "password", "session", "delete_account", "export")


def _err(fn: Callable[[], Any]) -> GxgError:
    try:
        fn()
    except GxgError as e:
        return e
    raise AssertionError("expected GxgError, call succeeded")


def check_mc06(start: StartFn, valid_doc: dict[str, Any]) -> None:
    start(valid_doc)  # a valid registry starts
    for field in ("effect_class", "input_schema", "action", "resource"):
        bad = _copy(valid_doc)
        del bad["capabilities"][0][field]
        _must_refuse(start, bad, f"entry without {field}")
    bad = _copy(valid_doc)
    bad["capabilities"][0]["resource"] = {}
    _must_refuse(start, bad, "entry without resource type")
    bad = _copy(valid_doc)
    bad["capabilities"][0]["name"] = "chats.invoke"
    _must_refuse(start, bad, "forbidden name")


def check_mc07(c: ExtConnector) -> None:
    before = len(c.provider_calls)
    e = _err(lambda: c.call(T1, P, "chats.history", {"chat_id": CHAT_NOT_GRANTED}))
    assert e.code == "resource_forbidden", e.code
    e = _err(lambda: c.call(T1, P, "chats.history", {"chat_id": CHAT_OTHER_TENANT}))
    assert e.code in ("resource_forbidden", "not_found"), e.code
    assert len(c.provider_calls) == before, "provider reached before the resource check"
    c.call(T1, P, "chats.history", {"chat_id": CHAT_OK})  # the allowed chat works
    assert len(c.provider_calls) == before + 1


def check_mc08(c: ExtConnector) -> None:
    before = len(c.provider_calls)
    e = _err(lambda: c.call(T1, P, "messages.delete", {"chat_id": CHAT_OK, "message_id": 1}))
    assert e.code == "permission_denied", e.code
    assert "telegram.message.propose" in (e.message + str(e.details)), "no pointer to the propose-tool"
    assert len(c.provider_calls) == before, "non-read entry reached the provider"
    assert "chats.history" in c.search(T1, P, "") and "chats.members" not in c.search(T1, P, "")  # enabled only
    for q in ("", "invoke", "2fa", "session", "delete account", "export", "chats"):
        for name in c.search(T1, P, q):
            assert not any(f in name for f in FORBIDDEN_FRAGMENTS), f"forbidden entry in search: {name}"


def check_mc09(c: ExtConnector) -> None:
    before = len(c.provider_calls)
    e = _err(lambda: c.call(T1, P, "chats.members", {"chat_id": CHAT_OK}))
    assert e.code == "effect_disabled", e.code
    e = _err(lambda: c.call(T1, P, "no.such_entry", {"chat_id": CHAT_OK}))
    assert e.code == "not_found", e.code
    assert len(c.provider_calls) == before
    # `ext` grant without a class is rejected by GrantResolver: AC-M11.2-03, blocked_on_owner (O-1).


def check_mc10(c: ExtConnector) -> None:
    c.call(T1, P, "chats.history", {"chat_id": CHAT_OK, "text": SECRET_TEXT, "peer": SECRET_PEER})
    assert any(r.get("capability") == "chats.history" for r in c.audit), "audit lacks the registry entry name"
    rec = next(r for r in c.audit if r.get("capability") == "chats.history")
    assert rec.get("tenant") == T1 and rec.get("principal") == P and rec.get("result"), rec
    assert rec.get("version") == "1.0.0" and rec.get("group") == "ext:read:history", rec  # Q7
    dump = repr(c.audit)
    assert SECRET_TEXT not in dump and SECRET_PEER not in dump, "text or peer id leaked into audit"
    c.set_flood_wait(7)
    e = _err(lambda: c.call(T1, P, "chats.history", {"chat_id": CHAT_OK}))
    assert e.code == "rate_limited" and e.retry_after_s == 7, (e.code, e.retry_after_s)


def _copy(doc: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(doc)


def _must_refuse(start: StartFn, doc: object, label: str) -> None:
    try:
        start(doc)
    except Exception:
        return
    raise AssertionError(f"connector started with invalid registry: {label}")
