"""AC-M1-05…07: GrantResolver on policies/tenants/example.yaml and the signed bundle (GR-01…GR-17, GR-11a)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kit_testing import BUNDLE_KID, T0, BundleKit, Clock, iso, sign_bundle

from gxg_connector_kit import _jwt
from gxg_connector_kit.errors import GxgError
from gxg_connector_kit.grants import AllowedSet, BundleError, BundleStore, Decision, GrantResolver, load_bundle

EXPIRY = 1_803_859_200  # 2027-03-01T00:00:00Z, expires_at of tg-hermes-chats in example.yaml
DAY = 24 * 3600

# Second tenant, only to have a foreign tenant, a type mask and a group of its own.
T_BETA: dict[str, Any] = {
    "tenant": "t-beta",
    "schema_version": 1,
    "connections": [
        {"id": "tg-main", "domain": "telegram", "credential_ref": "secret://t-beta/telegram/main"},
        {"id": "kb-b", "domain": "kb", "credential_ref": "secret://t-beta/kb/service"},
    ],
    "principals": {"agents": [{"id": "agent:t-beta:spy"}, {"id": "agent:t-beta:ops"}]},
    "groups": {"operators": ["agent:t-beta:ops"]},
    "grants": [
        {"id": "b-chats", "subjects": ["agent:t-beta:spy"], "actions": ["read"],
         "resources": ["telegram:tg-main/chat/-1001234567890"]},
        {"id": "b-kb-all", "subjects": ["agent:t-beta:spy"], "actions": ["read", "collect"],
         "resources": ["kb:kb-b/collection/*"]},
        {"id": "b-ops", "subjects": ["group:operators", "group:missing"], "actions": ["propose"],
         "resources": ["telegram:tg-main/chat/-100555"]},
        # Lint (GR-05, GR-09) rejects the next grant; the resolver must deny it anyway.
        {"id": "b-lint-bypass", "subjects": ["agent:t-alpha:hermes", "agent:t-beta:spy"], "actions": ["write"],
         "resources": ["kb:kb-ghost/collection/x", "telegram:kb-b/collection/x", "kb:kb-b/collection/w"]},
    ],
}  # fmt: skip

HERMES, ANALYST, SIGNALS = "agent:t-alpha:hermes", "agent:t-alpha:analyst", "agent:t-alpha:signals-actions"
APPROVER = "oidc:https://accounts.example.invalid:1001"
SPY, OPS = "agent:t-beta:spy", "agent:t-beta:ops"
TG_CHAT, TG_SIGNALS = "telegram:tg-main/chat/-1001234567890", "telegram:tg-main/chat/-1009876543210"

# (tenant, principal, action, rid, expected grant id or None for deny)
TABLE: list[tuple[str, str, str, str, str | None]] = [
    ("t-alpha", HERMES, "read", "kb:kb-main/collection/public", "kb-research"),  # via group
    ("t-alpha", ANALYST, "read", "kb:kb-main/collection/sales", "kb-research"),  # via group
    ("t-alpha", HERMES, "read", TG_CHAT, "tg-hermes-chats"),
    ("t-alpha", SIGNALS, "propose", TG_SIGNALS, "tg-signals-propose"),
    ("t-alpha", ANALYST, "read", TG_CHAT, None),  # not a subject
    ("t-alpha", HERMES, "collect", TG_CHAT, None),  # action not in the grant
    ("t-alpha", HERMES, "propose", TG_CHAT, None),  # action not in the grant
    ("t-alpha", SIGNALS, "read", TG_SIGNALS, None),  # propose does not give read
    ("t-alpha", HERMES, "write", "kb:kb-main/collection/public", None),
    ("t-alpha", HERMES, "read", "kb:kb-main/collection/private", None),  # resource not granted
    ("t-alpha", HERMES, "read", "kb:kb-other/collection/public", None),  # unknown connection
    ("t-alpha", HERMES, "read", "kb:kb-b/collection/public", None),  # connection of t-beta (GR-02)
    ("t-alpha", HERMES, "read", "telegram:kb-main/collection/public", None),  # domain ≠ connection domain
    ("t-alpha", HERMES, "read", "kb:kb-main/collection/*", None),  # a mask is not a RID
    ("t-alpha", HERMES, "read", "kb:kb-main/collection/public/x", None),  # malformed RID
    ("t-alpha", HERMES, "read", "kb-main/collection/public", None),  # malformed RID
    ("t-alpha", "agent:t-alpha:ghost", "read", "kb:kb-main/collection/public", None),  # unknown principal
    ("t-alpha", APPROVER, "read", "kb:kb-main/collection/public", None),  # human without grant
    ("t-alpha", SPY, "read", TG_CHAT, None),  # principal of another tenant
    ("t-beta", HERMES, "read", TG_CHAT, None),  # t-alpha agent in t-beta
    ("t-gamma", HERMES, "read", "kb:kb-main/collection/public", None),  # unknown tenant
    ("t-beta", SPY, "read", TG_CHAT, "b-chats"),  # same RID, own tenant
    ("t-beta", SPY, "read", "kb:kb-b/collection/anything", "b-kb-all"),  # mask *
    ("t-beta", SPY, "collect", "kb:kb-b/collection/other", "b-kb-all"),  # mask *
    ("t-beta", SPY, "read", "kb:kb-b/document/d1", None),  # mask covers one type only
    ("t-beta", SPY, "propose", "kb:kb-b/collection/anything", None),
    ("t-beta", OPS, "propose", "telegram:tg-main/chat/-100555", "b-ops"),  # group of t-beta
    ("t-beta", OPS, "read", "kb:kb-b/collection/x", None),
    ("t-beta", HERMES, "write", "kb:kb-b/collection/w", None),  # subject of another tenant (GR-05)
    ("t-beta", SPY, "write", "kb:kb-b/collection/w", "b-lint-bypass"),
    ("t-beta", SPY, "write", "kb:kb-ghost/collection/x", None),  # undeclared connection (GR-02)
    ("t-beta", SPY, "write", "telegram:kb-b/collection/x", None),  # domain ≠ connection domain
    ("t-beta", SPY, "read", "kb:kb-b/collection/x\n", None),  # trailing newline is not a RID
    ("t-alpha", HERMES, "read", "kb:kb-main/collection/public\n", None),
]


@pytest.fixture
def kit(bundle_kit: BundleKit) -> BundleKit:
    bundle_kit.tenants["t-beta"] = {"grants": copy.deepcopy(T_BETA), "approval_policy": None}
    return bundle_kit


@pytest.fixture
def store(kit: BundleKit, clock: Clock) -> BundleStore:
    s = BundleStore(kit.keys, now=clock)
    s.load(*kit.signed())
    return s


def test_store_is_a_grant_resolver(store: BundleStore) -> None:
    resolver: GrantResolver = store
    assert resolver.version == "a" * 40


def test_table_has_at_least_20_cases() -> None:
    assert len(TABLE) >= 20


@pytest.mark.parametrize("case", TABLE, ids=lambda c: f"{c[0]}|{c[1]}|{c[2]}|{c[3]}")
def test_check_table(store: BundleStore, case: tuple[str, str, Any, str, str | None]) -> None:
    tenant, principal, action, rid, grant = case
    assert store.check(tenant, principal, action, rid) == Decision(grant is not None, grant)


@pytest.mark.parametrize(("t", "allowed"), [(EXPIRY - 1, True), (EXPIRY, False), (EXPIRY + DAY, False)])
def test_expired_grant(kit: BundleKit, clock: Clock, t: int, allowed: bool) -> None:
    clock.t = t
    store = BundleStore(kit.keys, now=clock)
    store.load(*kit.signed(built_at=t))
    assert store.check("t-alpha", HERMES, "read", TG_CHAT).allowed is allowed


def test_unknown_group_grants_nobody(store: BundleStore) -> None:
    assert not store.check("t-beta", "group:missing", "propose", "telegram:tg-main/chat/-100555").allowed


def test_already_expanded_groups(kit: BundleKit, clock: Clock) -> None:
    """GR-11a shape: subjects are principals, no `groups` key."""
    alpha = kit.tenants["t-alpha"]["grants"]
    del alpha["groups"]
    alpha["grants"][0]["subjects"] = [HERMES, ANALYST]
    store = BundleStore(kit.keys, now=clock)
    store.load(*kit.signed())
    assert store.check("t-alpha", ANALYST, "read", "kb:kb-main/collection/sales") == Decision(True, "kb-research")


def test_allowed_sets(store: BundleStore) -> None:
    public, sales = "kb:kb-main/collection/public", "kb:kb-main/collection/sales"
    assert store.allowed("t-alpha", HERMES, "read", "kb-main", "collection") == AllowedSet(
        False, frozenset({public, sales})
    )
    assert store.allowed("t-alpha", HERMES, "read", "tg-main", "chat") == AllowedSet(False, frozenset({TG_CHAT}))
    assert store.allowed("t-beta", SPY, "read", "kb-b", "collection") == AllowedSet(True, frozenset())
    assert store.allowed("t-beta", SPY, "read", "kb-b", "document") == AllowedSet(False, frozenset())
    assert store.allowed("t-alpha", HERMES, "read", "kb-b", "collection") == AllowedSet(False, frozenset())
    assert store.allowed("t-alpha", SPY, "read", "tg-main", "chat") == AllowedSet(False, frozenset())
    assert store.allowed("t-alpha", HERMES, "collect", "tg-main", "chat") == AllowedSet(False, frozenset())


def test_allowed_excludes_expired_grant(kit: BundleKit, clock: Clock) -> None:
    clock.t = EXPIRY
    store = BundleStore(kit.keys, now=clock)
    store.load(*kit.signed(built_at=EXPIRY))
    assert store.allowed("t-alpha", HERMES, "read", "tg-main", "chat") == AllowedSet(False, frozenset())


def test_tenant_slice(kit: BundleKit, clock: Clock) -> None:
    store = BundleStore(kit.keys, tenant="t-beta", now=clock)
    store.load(*kit.signed())
    assert set(store.current.tenants) == {"t-beta"}
    assert store.current.approval_policies == {"t-beta": None}
    assert not store.check("t-alpha", HERMES, "read", TG_CHAT).allowed
    with pytest.raises(BundleError, match="no slice"):
        BundleStore(kit.keys, tenant="t-gamma", now=clock).load(*kit.signed())


def test_approval_policy_is_kept(store: BundleStore) -> None:
    policy = store.current.approval_policies["t-alpha"]
    assert policy is not None
    assert policy["rules"][0]["id"] == "tg-partner-auto"


# --- AC-M1-06: signature ------------------------------------------------------------------


def assert_rejected(kit: BundleKit, clock: Clock, data: bytes, sig: bytes, match: str) -> None:
    store = BundleStore(kit.keys, now=clock)
    with pytest.raises(BundleError, match=match):
        store.load(data, sig)
    r = store.readiness()
    assert not r.ready
    assert match.split(":", maxsplit=1)[0] in r.reasons[0]
    with pytest.raises(GxgError) as exc:
        store.check("t-alpha", HERMES, "read", TG_CHAT)
    assert exc.value.code == "temporarily_unavailable"


def test_valid_bundle_is_ready(store: BundleStore) -> None:
    assert store.readiness().ready
    assert store.readiness().reasons == ()


def test_not_loaded_is_not_ready(kit: BundleKit, clock: Clock) -> None:
    store = BundleStore(kit.keys, now=clock)
    assert not store.readiness().ready
    with pytest.raises(GxgError, match="not ready"):
        _ = store.version


@pytest.mark.parametrize("where", [0, 1, 0.5, -2, -1])
def test_changed_byte(kit: BundleKit, clock: Clock, where: float) -> None:
    data, sig = kit.signed()
    i = int(len(data) * where) if isinstance(where, float) else where
    tampered = bytearray(data)
    tampered[i] ^= 0x01
    assert_rejected(kit, clock, bytes(tampered), sig, "signature: does not verify")


def test_appended_byte(kit: BundleKit, clock: Clock) -> None:
    data, sig = kit.signed()
    assert_rejected(kit, clock, data + b" ", sig, "signature: does not verify")


@pytest.mark.parametrize("sig", [b"", b"{}", b"null", b"[1]", b'{"alg":"EdDSA","kid":"ci-2026-11"}'])
def test_missing_or_empty_signature(kit: BundleKit, clock: Clock, sig: bytes) -> None:
    assert_rejected(kit, clock, kit.data(), sig, "signature")


def test_signed_by_foreign_key(kit: BundleKit, clock: Clock) -> None:
    data = kit.data()
    assert_rejected(kit, clock, data, sign_bundle(Ed25519PrivateKey.generate(), data), "signature: does not verify")


def test_unknown_kid(kit: BundleKit, clock: Clock) -> None:
    data = kit.data()
    assert_rejected(kit, clock, data, sign_bundle(kit.ci_key, data, kid="ci-old"), "signature: unknown kid")


def test_kid_must_be_a_string(kit: BundleKit, clock: Clock) -> None:
    data = kit.data()
    sig = json.loads(sign_bundle(kit.ci_key, data))
    assert_rejected(kit, clock, data, json.dumps({**sig, "kid": ["ci-2026-11"]}).encode(), "signature: unknown kid")


@pytest.mark.parametrize("alg", ["none", "HS256", None])
def test_wrong_alg(kit: BundleKit, clock: Clock, alg: str | None) -> None:
    data = kit.data()
    sig = {**json.loads(sign_bundle(kit.ci_key, data)), "alg": alg}
    assert_rejected(kit, clock, data, json.dumps(sig).encode(), "signature: alg")


def test_signature_with_padding_or_bad_alphabet(kit: BundleKit, clock: Clock) -> None:
    data = kit.data()
    raw = kit.ci_key.sign(data)
    good = _jwt.b64url_encode(raw)
    for encoded in (good + "==", good[:-1] + "+", good[:-1] + "!", 7):
        sig = json.dumps({"alg": "EdDSA", "kid": BUNDLE_KID, "sig": encoded}).encode()
        assert_rejected(kit, clock, data, sig, "signature: malformed")


def test_verification_key_comes_from_config_not_bundle(kit: BundleKit, clock: Clock) -> None:
    with pytest.raises(BundleError, match="unknown kid"):
        BundleStore({}, now=clock).load(*kit.signed())


# --- AC-M1-07: staleness (GR-13) ----------------------------------------------------------


def test_bundle_older_than_24h_is_not_ready(kit: BundleKit, clock: Clock) -> None:
    store = BundleStore(kit.keys, now=clock)
    store.load(*kit.signed(built_at=T0 - DAY - 1))
    r = store.readiness()
    assert not r.ready
    assert "older than 24 h" in r.reasons[0]
    with pytest.raises(GxgError) as exc:
        store.check("t-alpha", HERMES, "read", TG_CHAT)
    assert exc.value.code == "temporarily_unavailable"


def test_bundle_of_exactly_24h_is_ready(kit: BundleKit, clock: Clock) -> None:
    store = BundleStore(kit.keys, now=clock)
    store.load(*kit.signed(built_at=T0 - DAY))
    assert store.readiness().ready


def test_loaded_bundle_goes_stale(store: BundleStore, clock: Clock) -> None:
    clock.t = T0 + DAY + 1
    assert not store.readiness().ready
    with pytest.raises(GxgError):
        store.registry()


# --- reload ------------------------------------------------------------------------------


def test_bundle_from_the_future_is_rejected_and_does_not_block_the_next(kit: BundleKit, clock: Clock) -> None:
    store = BundleStore(kit.keys, now=clock)
    with pytest.raises(BundleError, match="future"):
        store.load(*kit.signed(built_at=T0 + 6, version="f" * 40))
    assert not store.readiness().ready
    assert "future" in store.readiness().reasons[0]
    store.load(*kit.signed(built_at=T0 + 5))  # AS-11 skew is tolerated
    clock.t = T0 + 10
    store.load(*kit.signed(built_at=T0 + 10, version="b" * 40))
    assert store.version == "b" * 40
    assert store.readiness().ready


def test_reload_with_the_same_built_at_is_accepted(kit: BundleKit, store: BundleStore) -> None:
    # GR-13a rejects only an older built_at; a restart or poll may load the same bundle again.
    store.load(*kit.signed(version="c" * 40))
    assert store.version == "c" * 40


def test_failed_reload_keeps_previous_bundle(kit: BundleKit, store: BundleStore) -> None:
    data, sig = kit.signed(built_at=T0 + 10, version="b" * 40)
    with pytest.raises(BundleError):
        store.load(data[:-1], sig)
    assert store.readiness().ready
    assert store.version == "a" * 40


def test_newer_bundle_replaces_and_older_is_rejected(kit: BundleKit, store: BundleStore, clock: Clock) -> None:
    clock.t = T0 + 20
    store.load(*kit.signed(built_at=T0 + 10, version="b" * 40))
    assert store.version == "b" * 40
    with pytest.raises(BundleError, match="rollback"):
        store.load(*kit.signed(built_at=T0, version="a" * 40))
    assert store.version == "b" * 40
    assert store.readiness().ready


# --- malformed content (signed, so only a CI bug can produce it; still fail closed) ---------


def _signed_doc(kit: BundleKit, mutate: Any) -> tuple[bytes, bytes]:
    doc = json.loads(kit.data())
    mutate(doc)
    data = json.dumps(doc).encode()
    return data, sign_bundle(kit.ci_key, data)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(bundle_version=""), "bundle_version"),
        (lambda d: d.pop("bundle_version"), "malformed"),
        (lambda d: d.update(built_at="2027-01-15T11:00:00+03:00"), "UTC"),
        (lambda d: d.update(built_at="2027-01-15T08:00:00"), "malformed"),
        (lambda d: d.update(built_at=T0), "malformed"),
        (lambda d: d.update(tenants=[]), "malformed"),
        (lambda d: d["tenants"].update({"t-beta": d["tenants"]["t-alpha"]}), "another tenant"),
        (lambda d: d["tenants"]["t-alpha"]["approval_policy"].update(tenant="t-beta"), "another tenant"),
        (lambda d: d["tenants"]["t-alpha"]["grants"].update(tenant="t-beta"), "another tenant"),
        (lambda d: d["tenants"]["t-alpha"]["grants"].update(schema_version=2), "malformed"),
        (lambda d: d["tenants"]["t-alpha"]["grants"]["grants"][0].update(resources=["kb:kb-main/*"]), "malformed"),
        (lambda d: d["tenants"]["t-alpha"]["grants"]["grants"][0].update(expires_at="soon"), "malformed"),
        (lambda d: d["tenants"]["t-alpha"].pop("approval_policy"), "malformed"),
        (lambda d: d["platform"].update(services=[{"id": "x"}]), "malformed"),
        (lambda d: d.pop("platform"), "malformed"),
    ],
)
def test_malformed_signed_bundle(kit: BundleKit, clock: Clock, mutate: Any, match: str) -> None:
    assert_rejected(kit, clock, *_signed_doc(kit, mutate), match="bundle: " if match != "malformed" else "bundle")
    with pytest.raises(BundleError, match=match):
        load_bundle(*_signed_doc(kit, mutate), kit.keys)


@pytest.mark.parametrize("data", [b"\xff\xfe", b"not json", b"[]"])
def test_signed_garbage(kit: BundleKit, clock: Clock, data: bytes) -> None:
    assert_rejected(kit, clock, data, sign_bundle(kit.ci_key, data), "bundle")


def test_built_at_iso_helper_matches_spec_format() -> None:
    assert iso(T0) == "2027-01-15T08:00:00Z"
