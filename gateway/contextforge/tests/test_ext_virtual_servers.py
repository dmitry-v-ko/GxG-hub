"""M11.3/M11.4, Hub side: what each role sees in ContextForge and which registry entries the connector shows it.

Config and fixtures only (gateway/contextforge/examples/tenants/example-ext*.yaml,
gateway/contextforge/ext_roles.py); not run against a live ContextForge (see ext_roles.py). Layer 1: virtual server
and `unified_pdp` rules per agent. Layer 2: entries the connector lists in `ext.search` (enabled, read, group covered
by the caller's grant; GR-22, GR-23, GR-25).
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from kit_testing import BundleKit, Clock

from gxg_connector_kit.grants import BundleStore

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "uv.lock").exists())  # also from a mutants/ copy
_spec = importlib.util.spec_from_file_location("ext_roles", ROOT / "gateway/contextforge/ext_roles.py")
assert _spec and _spec.loader
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

TENANTS = ROOT / "gateway/contextforge/examples/tenants"  # fixtures, not policies/: main's gate must parse policies/
TEST_FILE, EXAMPLE_FILE = (TENANTS / f"{n}.yaml" for n in ("example-ext-test", "example-ext"))
TEST, EXAMPLE = (yaml.safe_load(f.read_text("utf-8")) for f in (TEST_FILE, EXAMPLE_FILE))
READER, COLLECTOR = "agent:t-ext-test:reader", "agent:t-ext-test:collector"
CHAT = "telegram:tg-test/chat/tgc1_EXAMPLEtestChat0000000000000000000"

# The service registry (gxg-telegram-service/gxg-capabilities.yaml, M11.3 + M11.4): entry -> group.
REGISTRY = {
    "chats.get_info": "info",
    "chats.history": "history",
    "chats.pinned": "pinned",
    "chats.members": "members",
    "messages.get": "message",
    "users.get_info": "users",
    "messages.reactions": "reactions",
    "users.profile_photos": "pii",
    "users.profile_photo_get": "pii",
}
PII = {n for n, g in REGISTRY.items() if g == "pii"}


@pytest.mark.parametrize("name", ["example-ext-test", "example-ext"])
def test_committed_server_config_matches_the_policy_file(name: str) -> None:
    doc = yaml.safe_load((TENANTS / f"{name}.yaml").read_text("utf-8"))
    committed = json.loads((ROOT / f"gateway/contextforge/examples/telegram-ext-{name}.json").read_text("utf-8"))
    assert committed == json.loads(json.dumps(R.build(doc)))  # regenerate: python gateway/contextforge/ext_roles.py


def test_roles_see_core_ext_and_participants_tools_separately() -> None:
    assert R.agent_tools(TEST, READER) == [*R.CORE[:3], *R.EXT]  # read only: no collect tools; message.get is read
    assert R.agent_tools(TEST, COLLECTOR) == [*R.CORE, R.PARTICIPANTS, *R.EXT]
    ex = R.build(EXAMPLE)
    assert ex["servers"]["vs-tg-t-ext-example-ext"] == sorted([*R.CORE[:3], *R.EXT])
    assert R.PARTICIPANTS in ex["servers"]["vs-tg-t-ext-example-people"]
    assert not set(R.EXT) & set(ex["servers"]["vs-tg-t-ext-example-people"])  # collector has no ext grant


def test_an_agent_without_telegram_grants_gets_no_server_and_no_rule() -> None:
    doc = yaml.safe_load(EXAMPLE_FILE.read_text("utf-8"))
    doc["principals"]["agents"].append({"id": "agent:t-ext-example:idle"})
    built = R.build(doc)
    assert "agent:t-ext-example:idle" not in built["agents"]
    assert all("idle@" not in json.dumps(r) for r in built["pdp_rules"])


@pytest.mark.parametrize("doc", [TEST, EXAMPLE], ids=["test", "example"])
def test_no_rule_allows_execute_reconcile_or_propose(doc: dict[str, Any]) -> None:
    for rule in R.build(doc)["pdp_rules"]:
        assert not [a for a in rule["actions"] if any(f in a for f in R.EXECUTE_FRAGMENTS)], rule["id"]


def test_rules_are_exact_email_and_exact_tool_names_like_the_stand() -> None:
    stand = json.loads((ROOT / "stand/unified_pdp.rules.json").read_text("utf-8"))
    sample = next(r for r in stand if r["id"] == "t-alpha.reader")
    rule = R.build(TEST)["pdp_rules"][0]
    assert set(rule) == set(sample)
    assert rule["conditions"] == {"subject.email": "reader@t-ext-test.agents.gxg.example.com"}
    assert not any("*" in a for r in R.build(TEST)["pdp_rules"] for a in r["actions"])


def test_the_pilot_tenant_has_no_ext_or_participants_wiring() -> None:
    """O-4: the extensions are not part of the pilot; t-pilot gets no ext tools, no pii, no extensions block."""
    pilot = yaml.safe_load((ROOT / "policies/pilot/tenants/t-pilot.yaml").read_text("utf-8"))
    assert "extensions" not in pilot
    assert not any(a.startswith("ext:") for g in pilot["grants"] for a in g["actions"])
    assert not set(R.EXT) & set(R.agent_tools(pilot, "agent:t-pilot:pilot"))
    stand = (ROOT / "stand/unified_pdp.rules.json").read_text("utf-8")
    assert "ext-" not in stand and "participants" not in stand


@pytest.mark.parametrize(
    "rules", ["stand/unified_pdp.rules.json", "deploy/railway/contextforge/unified_pdp.rules.json"]
)
def test_the_script_executor_facade_is_not_hit_by_a_deny_rule(rules: str) -> None:
    """ADR-0007 facade `hub.script.run` (owner decision 2026-11-26: renamed from `hub.execute`, which EFF-02's
    `deny:agents-never-execute` refused on the stand): no deny pattern matches it; the old name stays refused."""
    deny = [
        a for r in json.loads((ROOT / rules).read_text("utf-8")) if r["id"].startswith("deny") for a in r["actions"]
    ]
    assert "tools.invoke.*execute*" in deny  # EFF-02 unchanged
    tool = "tools.invoke.hubexec-t-ext-test-" + "hub.script.run".replace(".", "-")
    assert not [p for p in deny if fnmatch.fnmatchcase(tool, p)]
    assert [p for p in deny if fnmatch.fnmatchcase("tools.invoke.hubexec-t-ext-test-hub-execute", p)]
    assert not any(f in "hub.script.run" for f in R.EXECUTE_FRAGMENTS)


# --- layer 2: what the connector lists, on the real kit resolver and the committed test tenant -------------------


@pytest.fixture
def store(tmp_path: Path) -> BundleStore:
    kit = BundleKit(tmp_path / "state.json")
    approval = {
        "tenant": "t-ext-test",
        "schema_version": 1,
        "rules": [
            {
                "id": "msg",
                "match": {"tools": ["telegram.message.propose"]},
                "decision": "require_approval",
                "approvers": ["oidc:https://accounts.example.invalid:2002"],
            }
        ],
    }
    kit.tenants = {"t-ext-test": {"grants": TEST, "approval_policy": approval}}
    s = BundleStore(kit.keys, state_file=kit.state_file, now=Clock())
    s.load(*kit.signed())
    return s


def search(store: BundleStore, tenant: str, principal: str, rid: str) -> set[str]:
    """Reference model of `ext.search` (GR-23): enabled for the tenant AND the caller's grant covers the group."""
    return {
        n
        for n, g in REGISTRY.items()
        if store.ext_enabled(tenant, "telegram", n) and store.ext_check(tenant, principal, f"ext:read:{g}", rid).allowed
    }


def test_ext_read_shows_every_enabled_entry_except_pii(store: BundleStore) -> None:
    seen = search(store, "t-ext-test", READER, CHAT)
    assert seen == set(REGISTRY) - PII


def test_the_explicit_pii_grant_adds_exactly_the_pii_entries(store: BundleStore) -> None:
    assert search(store, "t-ext-test", COLLECTOR, CHAT) == set(REGISTRY)


def test_pii_entries_are_denied_to_ext_read_even_when_enabled(store: BundleStore) -> None:
    for n in PII:
        assert store.ext_enabled("t-ext-test", "telegram", n)
        assert not store.ext_check("t-ext-test", READER, "ext:read:pii", CHAT).allowed


def test_nothing_is_offered_for_another_chat(store: BundleStore) -> None:
    assert search(store, "t-ext-test", COLLECTOR, CHAT.replace("testChat", "otherChat")) == set()
