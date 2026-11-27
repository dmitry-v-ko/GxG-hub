"""MC-11…MC-13 (M11.4) run against a minimal fake `participants.collect` connector, plus mutants.

A real connector reuses `ext_suite.check_mc11..13` through the `participants_factory` fixture. In the stage F report
these ids stay `not_run` until they run on the Telegram service (see scripts/ci/stage_f.py).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import ext_suite as s
import pytest

from gxg_connector_kit.errors import GxgError

PII = "ext:read:pii"
CURSOR_MAX = 16 * 1024


class FakeParticipants:
    """`telegram.participants.collect` pipeline: `collect` on every chat, dedupe per user, `created`/`updated` against
    the snapshot in the cursor, pii fields only with `ext:read:pii` (GR-25). Mutations: the keyword flags."""

    def __init__(
        self,
        *,
        leak_pii: bool = False,
        skip_collect_check: bool = False,
        no_dedupe: bool = False,
        audit_phone: bool = False,
        ignore_cursor: bool = False,
    ) -> None:
        self.provider_calls: list[tuple[str, dict[str, Any]]] = []
        self.audit: list[dict[str, Any]] = []
        self.owner = {s.CHAT_OK: s.T1, s.CHAT_READ_ONLY: s.T1, s.CHAT_NOT_GRANTED: s.T1, s.CHAT_OTHER_TENANT: s.T2}
        self.collect = {s.P: {s.CHAT_OK}, s.P_PII: {s.CHAT_OK}}
        self.read = {s.P: {s.CHAT_OK, s.CHAT_READ_ONLY}, s.P_PII: {s.CHAT_OK}}
        self.pii = {s.P_PII: {s.CHAT_OK}}
        self.users = {
            "u1": {"display_name": "Ivan", "photo_count": 1, "phone": s.PHONE, "photo_refs": [s.PHOTO_REF]},
            "u2": {"display_name": "Anna", "photo_count": 0},
            "u3": {"display_name": "Olga", "photo_count": 0},
        }
        self.events = {(s.CHAT_OK, "u1", "member"), (s.CHAT_OK, "u1", "author"), (s.CHAT_OK, "u1", "reactor")}
        self.events.add((s.CHAT_OK, "u2", "member"))
        self.snapshots: dict[str, dict[str, frozenset[str]]] = {}
        self._ids = itertools.count(1)
        self.leak_pii, self.skip_collect, self.no_dedupe = leak_pii, skip_collect_check, no_dedupe
        self.audit_phone, self.ignore_cursor = audit_phone, ignore_cursor

    def add_event(self, chat: str, user: str, role: str) -> None:
        self.events.add((chat, user, role))

    def collect_participants(  # noqa: PLR0912 - a flat pipeline, one branch per mutation
        self,
        tenant: str,
        principal: str,
        chat_refs: list[str],
        *,
        roles: list[str] | None = None,
        include_pii: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        for chat in chat_refs:  # every chat is checked before the transport is touched (GR-17: same answer for both)
            if self.owner.get(chat) != tenant:
                raise GxgError("not_found", "chat not found")
            if not self.skip_collect and chat not in self.collect.get(principal, set()):
                raise GxgError("resource_forbidden", "chat is outside the grant")
        if cursor is not None and (len(cursor) > CURSOR_MAX or cursor not in self.snapshots):
            raise GxgError("invalid_input", "bad cursor")
        known = {} if (cursor is None or self.ignore_cursor) else self.snapshots[cursor]
        self.provider_calls.append(("participants", {"chats": list(chat_refs)}))
        now: dict[str, frozenset[str]] = {}
        for chat, user, role in sorted(self.events):
            if chat in chat_refs and (roles is None or role in roles):
                now[user] = now.get(user, frozenset()) | {f"{chat}:{role}"}
        may_pii = all(c in self.pii.get(principal, set()) for c in chat_refs)
        items, pii_fields = [], 0
        for user, rs in now.items():
            if known.get(user) == rs:
                continue
            data: dict[str, Any] = {k: v for k, v in self.users[user].items() if k not in s.PII_FIELDS} | {
                "roles": [{"chat_ref": r.split(":")[0], "role": r.split(":")[1], "events": 1} for r in sorted(rs)]
            }
            data["completeness"] = {"member": "complete"}
            if (include_pii and may_pii) or self.leak_pii:
                for f in s.PII_FIELDS:
                    if f in self.users[user]:
                        data[f] = self.users[user][f]
                        pii_fields += 1
            action = "created" if user not in known else "updated"
            if self.no_dedupe:  # mutant: one element per role event
                items += [
                    {"key": f"telegram:tg/user/{user}", "action": "created", "structured_data": data | {"roles": [r]}}
                    for r in data["roles"]
                ]
            else:
                items.append({"key": f"telegram:tg/user/{user}", "action": action, "structured_data": data})
        errors = (
            [{"fields": list(s.PII_FIELDS), "code": "permission_denied", "chat_ref": c} for c in chat_refs[:1]]
            if include_pii and not may_pii
            else []
        )
        token = f"tgq1.{next(self._ids)}"
        self.snapshots[token] = now
        rec: dict[str, Any] = {
            "tool": "telegram.participants.collect",
            "tenant": tenant,
            "principal": principal,
            "result": "ok",
            "items": len(items),
            "pii_fields": pii_fields,
        }
        if self.audit_phone:
            rec["phone"] = s.PHONE  # mutant
        self.audit.append(rec)
        return {"items": items, "next_cursor": token, "field_errors": errors}


@pytest.fixture
def participants_factory() -> Callable[..., s.ParticipantsConnector]:
    return FakeParticipants


CHECKS = [s.check_mc11, s.check_mc12, s.check_mc13]


@pytest.mark.parametrize("check", CHECKS, ids=["mc11", "mc12", "mc13"])
def test_mc11_to_mc13_pass_on_conforming_connector(
    participants_factory: Callable[..., s.ParticipantsConnector], check: Callable[..., None]
) -> None:
    check(participants_factory())


MUTANTS = [
    pytest.param(s.check_mc11, {"leak_pii": True}, id="pii-without-the-grant"),
    pytest.param(s.check_mc11, {"audit_phone": True}, id="phone-in-audit"),
    pytest.param(s.check_mc12, {"skip_collect_check": True}, id="no-collect-check"),
    pytest.param(s.check_mc13, {"no_dedupe": True}, id="no-dedupe"),
    pytest.param(s.check_mc13, {"ignore_cursor": True}, id="repeat-returns-everything"),
]


@pytest.mark.parametrize(("check", "mutation"), MUTANTS)
def test_mutation_is_caught(
    participants_factory: Callable[..., s.ParticipantsConnector], check: Callable[..., None], mutation: dict[str, bool]
) -> None:
    with pytest.raises(AssertionError):
        check(participants_factory(**mutation))
