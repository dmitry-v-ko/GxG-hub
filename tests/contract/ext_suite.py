"""Reusable conformance checks MC-06…MC-13 for a connector with ext.* (MCC-11 п.7) and participants.collect (M11.4).

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


# --- MC-11…MC-13: telegram.participants.collect (M11.4, GR-25, SEC-23) ---------------------------------------------

P_PII = "agent-pii"  # holds `collect` + `ext:read:pii` on CHAT_OK; P holds `collect` + `ext:read` (no pii) on CHAT_OK
CHAT_READ_ONLY = "chat-read-only"  # T1; P holds `read` and `ext:read` there, but NOT `collect`
PHONE, PHOTO_REF, PHOTO_FILE = "+4915112345678", "tgp1-photo-ref-1", "avatar-ivan.jpg"
PII_FIELDS = ("phone", "photo_refs")


class ParticipantsConnector(Protocol):
    """Fixtures: users u1 (member, author, reactor in CHAT_OK; phone PHONE, photo ref PHOTO_REF, file PHOTO_FILE) and
    u2 (member only). `add_event(chat, user, role)` changes the source between runs."""

    provider_calls: list[tuple[str, dict[str, Any]]]
    audit: list[dict[str, Any]]

    def collect_participants(
        self,
        tenant: str,
        principal: str,
        chat_refs: list[str],
        *,
        roles: list[str] | None = None,
        include_pii: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...
    def add_event(self, chat: str, user: str, role: str) -> None: ...


def _items(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {i["key"]: i for i in out["items"]}


def check_mc11(c: ParticipantsConnector) -> None:
    def run(principal: str, include_pii: bool) -> dict[str, Any]:
        return c.collect_participants(T1, principal, [CHAT_OK], include_pii=include_pii)

    for include in (False, True):  # no pii grant: the fields are absent, not null; the rest is returned
        out = run(P, include)
        assert out["items"], "no items without pii"
        for item in out["items"]:
            data = item["structured_data"]
            assert not any(f in data for f in PII_FIELDS), f"pii field present without the grant: {data}"
            assert "display_name" in data and "photo_count" in data and data["roles"], data
        errors = out["field_errors"]
        if include:
            assert [e["code"] for e in errors] == ["permission_denied"] and set(errors[0]["fields"]) == set(PII_FIELDS)
        else:
            assert errors == []
    out = run(P_PII, False)  # include_pii defaults to false: nothing is added
    assert all(not any(f in i["structured_data"] for f in PII_FIELDS) for i in out["items"])
    out = run(P_PII, True)
    u1 = next(i["structured_data"] for i in out["items"] if i["structured_data"]["display_name"] == "Ivan")
    assert u1["phone"] == PHONE and u1["photo_refs"] == [PHOTO_REF], u1
    assert out["field_errors"] == []
    dump = repr(c.audit)
    for secret in (PHONE, PHOTO_REF, PHOTO_FILE, "AAAA-photo-bytes"):
        assert secret not in dump, f"{secret!r} leaked into the audit"
    rec = [r for r in c.audit if r.get("tool") == "telegram.participants.collect"]
    assert rec and all(r.get("tenant") == T1 and r.get("result") for r in rec), rec
    assert any(r.get("pii_fields") == 2 for r in rec), "audit lacks the number of pii fields returned"


def check_mc12(c: ParticipantsConnector) -> None:
    before = len(c.provider_calls)
    for chats in ([CHAT_READ_ONLY], [CHAT_NOT_GRANTED], [CHAT_OTHER_TENANT], [CHAT_OK, CHAT_READ_ONLY]):
        e = _err(lambda chats=chats: c.collect_participants(T1, P, chats))
        assert e.code in ("resource_forbidden", "not_found"), (chats, e.code)
        assert len(c.provider_calls) == before, f"transport reached for {chats}"
    other = _err(lambda: c.collect_participants(T1, P, [CHAT_OTHER_TENANT]))
    missing = _err(lambda: c.collect_participants(T1, P, ["chat-nowhere"]))
    assert (other.code, other.message) == (missing.code, missing.message), "a foreign chat is distinguishable (GR-17)"
    assert c.collect_participants(T1, P, [CHAT_OK])["items"]  # the allowed chat still works


def check_mc13(c: ParticipantsConnector) -> None:
    first = c.collect_participants(T1, P, [CHAT_OK])
    items = _items(first)
    assert len(items) == len(first["items"]) == 2, "one element per user"
    u1 = next(i for i in items.values() if i["structured_data"]["display_name"] == "Ivan")
    assert {r["role"] for r in u1["structured_data"]["roles"]} == {"member", "author", "reactor"}
    assert {i["action"] for i in items.values()} == {"created"}
    again = c.collect_participants(T1, P, [CHAT_OK], cursor=first["next_cursor"])
    assert again["items"] == [], "unchanged repeat run returned items"
    c.add_event(CHAT_OK, "u3", "member")
    new_user = c.collect_participants(T1, P, [CHAT_OK], cursor=again["next_cursor"])
    assert [i["action"] for i in new_user["items"]] == ["created"], new_user["items"]
    c.add_event(CHAT_OK, "u2", "author")
    new_role = c.collect_participants(T1, P, [CHAT_OK], cursor=new_user["next_cursor"])
    assert [i["action"] for i in new_role["items"]] == ["updated"], new_role["items"]
    assert {r["role"] for r in new_role["items"][0]["structured_data"]["roles"]} == {"member", "author"}
    e = _err(lambda: c.collect_participants(T1, P, [CHAT_OK], cursor="x" * 17000))
    assert e.code == "invalid_input", e.code  # an oversized cursor fails closed
