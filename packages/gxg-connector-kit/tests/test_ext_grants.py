"""AC-M11.2-03: ext.* grant groups and per-tenant enablement (MCC-11 п.5, GR-19…GR-24)."""

from __future__ import annotations

from typing import Any

import pytest
from kit_testing import BundleKit, Clock

from gxg_connector_kit.grants import BundleError, BundleStore, ExtGrantResolver, ext_group_covers

HERMES, ANALYST = "agent:t-alpha:hermes", "agent:t-alpha:analyst"
READ, MEMBERS = "ext:read", "ext:read:members"
CHAT, OTHER_CHAT = "telegram:tg-main/chat/-1001234567890", "telegram:tg-main/chat/-1009876543210"


def _store(kit: BundleKit, clock: Clock, grants: list[dict[str, Any]], enabled: list[str] | None = None) -> BundleStore:
    alpha = kit.tenants["t-alpha"]["grants"]
    alpha["grants"] += grants
    if enabled is not None:
        alpha["extensions"] = {"enabled": enabled}
    s = BundleStore(kit.keys, state_file=kit.state_file, now=clock)
    s.load(*kit.signed())
    return s


def _g(gid: str, subjects: list[str], groups: list[str], actions: list[str] | None = None) -> dict[str, Any]:
    return {"id": gid, "subjects": subjects, "actions": actions or ["read", *groups], "resources": [CHAT]}


@pytest.fixture
def store(kit: BundleKit, clock: Clock) -> BundleStore:
    return _store(
        kit,
        clock,
        [_g("ext-read", [HERMES], [READ]), _g("ext-members", [ANALYST], [MEMBERS])],
        ["telegram:chats.history"],
    )


@pytest.fixture
def kit(bundle_kit: BundleKit) -> BundleKit:
    return bundle_kit


def test_store_is_an_ext_resolver(store: BundleStore) -> None:
    resolver: ExtGrantResolver = store
    assert resolver.ext_check("t-alpha", HERMES, READ, CHAT).allowed


@pytest.mark.parametrize(
    ("principal", "group", "allowed"),
    [
        (HERMES, READ, True),  # class grant
        (HERMES, MEMBERS, True),  # class grant covers its subgroups
        (HERMES, "ext:read:other", True),
        (HERMES, "ext:external_effect", False),  # other class
        (HERMES, "ext:write_reversible:members", False),
        (ANALYST, MEMBERS, True),  # subgroup grant
        (ANALYST, READ, False),  # subgroup grant is not the whole class
        (ANALYST, "ext:read:other", False),  # other subgroup
        ("agent:t-alpha:signals-actions", READ, False),  # no ext grant
        ("agent:t-beta:spy", READ, False),  # foreign tenant's principal
        ("agent:t-alpha:nobody", READ, False),  # unknown principal
    ],
)
def test_ext_check(store: BundleStore, principal: str, group: str, allowed: bool) -> None:
    assert store.ext_check("t-alpha", principal, group, CHAT).allowed is allowed


def test_ext_check_unknown_tenant_denied(store: BundleStore) -> None:
    assert not store.ext_check("t-other", HERMES, READ, CHAT).allowed


def test_ext_check_returns_grant_id(store: BundleStore) -> None:
    assert store.ext_check("t-alpha", HERMES, READ, CHAT).grant_id == "ext-read"


def test_group_without_read_grant_is_denied(kit: BundleKit, clock: Clock) -> None:
    s = _store(kit, clock, [_g("ext-w", [ANALYST], [READ], ["ext:read"])])  # ANALYST has no `read` on the chat
    assert not s.ext_check("t-alpha", ANALYST, READ, CHAT).allowed  # GR-23


def test_group_does_not_reach_another_chat(store: BundleStore) -> None:
    assert not store.ext_check("t-alpha", HERMES, READ, OTHER_CHAT).allowed


def test_expired_group_grant_is_denied(kit: BundleKit, clock: Clock) -> None:
    g = _g("ext-x", [HERMES], [READ])
    g["expires_at"] = "2020-01-01T00:00:00Z"
    assert not _store(kit, clock, [g]).ext_check("t-alpha", HERMES, READ, CHAT).allowed


def test_record_enablement_is_per_tenant_and_off_by_default(store: BundleStore) -> None:
    assert store.ext_enabled("t-alpha", "telegram", "chats.history")
    assert not store.ext_enabled("t-alpha", "telegram", "chats.members")  # not listed -> off
    assert not store.ext_enabled("t-beta", "telegram", "chats.history")  # another tenant
    assert not store.ext_enabled("t-alpha", "kb", "chats.history")  # another domain
    assert not store.ext_enabled("t-ghost", "telegram", "chats.history")


def test_enablement_is_off_without_the_list(kit: BundleKit, clock: Clock) -> None:
    s = _store(kit, clock, [_g("ext-read", [HERMES], [READ])])
    assert not s.ext_enabled("t-alpha", "telegram", "chats.history")


@pytest.mark.parametrize(
    "bad", ["telegram.ext", "telegram.ext:", "telegram.ext:*", "telegram.ext:all", "telegram:read"]
)
def test_group_without_class_is_rejected_by_the_bundle(kit: BundleKit, clock: Clock, bad: str) -> None:
    kit.tenants["t-alpha"]["grants"]["grants"].append(_g("ext-bad", [HERMES], [bad]))
    s = BundleStore(kit.keys, state_file=kit.state_file, now=clock)
    with pytest.raises(BundleError):
        s.load(*kit.signed())


def test_coverage_rule() -> None:
    assert ext_group_covers(READ, READ)
    assert ext_group_covers(READ, MEMBERS)
    assert not ext_group_covers(MEMBERS, READ)
    assert not ext_group_covers("ext:read", "ext:readx")
