"""M13 deploy kit, offline: scripts/deploy/railway_deploy.py against a mock Railway GraphQL API (no network)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.deploy import railway_deploy as rd
from tests.deploy.mock_railway import MockRailway

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "fake-token-MARKER-0123456789"  # noqa: S105 (the session proxy substitutes the real one)


def run(mock: MockRailway, *args: str) -> int:
    return rd.main(list(args), transport=mock, out=rd.Out())


def secret_values(mock: MockRailway) -> set[str]:
    """Every value stored in a variable the spec declares secret (read from the mock, i.e. what reached Railway)."""
    spec = rd.load_spec()
    found: set[str] = set()
    for svc in mock.services.values():
        declared = rd.secrets_of(spec["services"][svc["name"]])
        found |= {v for k, v in svc["vars"].items() if k in declared and v}
    return found


def set_owner_values(mock: MockRailway) -> None:
    spec = rd.load_spec()
    for name, svc in spec["services"].items():
        if name in {s["name"] for s in mock.services.values()}:
            for var, sec in rd.secrets_of(svc).items():
                if sec["gen"] == "owner":
                    mock.by_name(name)["vars"][var] = "owner-supplied-value"


@pytest.fixture
def deployed(capsys: pytest.CaptureFixture[str]) -> MockRailway:
    mock = MockRailway()
    assert run(mock) == 0
    capsys.readouterr()
    return mock


def test_first_run_creates_project_services_variables_volumes(deployed: MockRailway) -> None:
    spec = rd.load_spec()
    assert deployed.project == {"name": "gxg-hub-pilot", "env": "production"}
    assert {s["name"] for s in deployed.services.values()} == set(spec["services"])
    api = deployed.by_name("api")["vars"]
    assert api["GXG_LISTEN"] == "0.0.0.0:8090"
    assert api["GXG_SECRET__GXG_DB_DSN"].startswith("postgresql://gxg_api:")
    assert "${{approval-db.RAILWAY_PRIVATE_DOMAIN}}" in api["GXG_SECRET__GXG_DB_DSN"]
    assert deployed.by_name("executor")["volume"] == "/var/lib/gxg"
    assert deployed.by_name("migrate")["instance"]["restartPolicyType"] == "NEVER"
    assert not deployed.service_domains and not deployed.custom_domains and not deployed.tcp


def test_second_run_changes_nothing(deployed: MockRailway) -> None:
    before = copy.deepcopy(deployed.services)
    deployed.mutations.clear()
    assert run(deployed) == 0
    assert deployed.mutations == []  # no create, no variable write, no setting, no redeploy
    assert deployed.services == before  # and the generated secrets were not rotated


def test_secrets_are_not_overwritten_but_literals_are_enforced(deployed: MockRailway) -> None:
    key = deployed.by_name("api")["vars"]["GXG_SECRET__GXG_DB_DSN"]
    deployed.by_name("api")["vars"]["GXG_LISTEN"] = "0.0.0.0:9999"
    assert run(deployed) == 0
    api = deployed.by_name("api")["vars"]
    assert api["GXG_SECRET__GXG_DB_DSN"] == key
    assert api["GXG_LISTEN"] == "0.0.0.0:8090"


def test_every_secret_lands_only_on_its_own_service(deployed: MockRailway) -> None:
    holders: dict[str, set[str]] = {}
    for svc in deployed.services.values():
        for k, v in svc["vars"].items():
            if k.startswith(rd.SECRET_PREFIX) and v:
                holders.setdefault(v, set()).add(svc["name"])
    assert all(len(h) == 1 for h in holders.values())  # AS-28: no secret value on two services
    assert "GXG_SECRET__GXG_PAYLOAD_PRIVATE_KEYS" in deployed.by_name("executor")["vars"]
    assert "GXG_SECRET__GXG_PAYLOAD_PRIVATE_KEYS" not in deployed.by_name("ui")["vars"]
    assert "GXG_SECRET__GXG_PREVIEW_KEYS" in deployed.by_name("ui")["vars"]
    # the role passwords of db-init are the ones in the DSNs of the processes
    roles = json.loads(deployed.by_name("db-init")["vars"]["GXG_SECRET__GXG_ROLE_PASSWORDS"])
    assert roles["gxg_executor"] in deployed.by_name("executor")["vars"]["GXG_SECRET__GXG_DB_DSN"]


def test_no_generated_value_reaches_stdout_or_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    mock = MockRailway()
    assert rd.main([], transport=mock, out=rd.Out()) == 0
    assert run(mock) == 0
    assert run(mock, "--verify") in (0, 1)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    markers = secret_values(mock)
    assert len(markers) >= 15  # the positive control: the generated secrets are there to be found
    assert not [m for m in markers if m in text]
    assert "set variables on executor: " in text  # names are logged
    assert "GXG_SECRET__GXG_EXEC_SIGNING_KEYS" in text


def test_token_and_api_errors_are_redacted(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_TOKEN", TOKEN)
    mock = MockRailway()
    mock.fail_with = f"denied for {TOKEN}"
    assert rd.main([], transport=mock) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "[redacted]" in captured.err


def test_missing_token_is_a_clean_error(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAILWAY_TOKEN", raising=False)
    assert rd.main([]) == 1
    assert "RAILWAY_TOKEN is not set" in capsys.readouterr().err


def test_dry_run_makes_no_api_call_and_prints_the_plan(capsys: pytest.CaptureFixture[str]) -> None:
    def boom(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("dry-run must not call the API")

    assert rd.main(["--dry-run"], transport=boom) == 0
    out = capsys.readouterr().out
    for name in ("contextforge", "api", "mcp", "executor", "decider", "ui", "notifier", "sweeper", "telegram"):
        assert f"- {name} [" in out
    assert "approval-db.railway.internal" in out and "no API calls made" in out


def test_verify_is_green_on_a_clean_deployment(deployed: MockRailway, capsys: pytest.CaptureFixture[str]) -> None:
    set_owner_values(deployed)
    assert run(deployed, "--finalize") == 0
    assert run(deployed, "--verify") == 0, capsys.readouterr().out
    assert "FAIL" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["service", "custom"])
def test_verify_fails_on_a_public_domain(deployed: MockRailway, capsys: pytest.CaptureFixture[str], kind: str) -> None:
    set_owner_values(deployed)
    run(deployed, "--finalize")
    entry = {"id": "d1", "domain": "api-production.up.railway.app" if kind == "service" else "hub.example.com"}
    (deployed.service_domains if kind == "service" else deployed.custom_domains)[deployed.sid("api")] = [entry]
    capsys.readouterr()
    assert run(deployed, "--verify") == 1
    assert "FAIL api: no " in capsys.readouterr().out


def test_verify_fails_on_a_tcp_proxy(deployed: MockRailway, capsys: pytest.CaptureFixture[str]) -> None:
    set_owner_values(deployed)
    run(deployed, "--finalize")
    deployed.tcp[deployed.sid("approval-db")] = [{"id": "t1", "proxyPort": 4242}]
    assert run(deployed, "--verify") == 1
    assert "FAIL approval-db: no TCP proxy" in capsys.readouterr().out


def test_apply_closes_a_domain_someone_added(deployed: MockRailway) -> None:
    deployed.service_domains[deployed.sid("ui")] = [{"id": "d9", "domain": "x.up.railway.app"}]
    assert run(deployed) == 0
    assert deployed.service_domains[deployed.sid("ui")] == []


def test_verify_fails_on_a_foreign_secret_and_a_public_internal_url(
    deployed: MockRailway, capsys: pytest.CaptureFixture[str]
) -> None:
    set_owner_values(deployed)
    run(deployed, "--finalize")
    deployed.by_name("executor")["vars"]["GXG_SECRET__GXG_PREVIEW_KEYS"] = "stolen-preview-key"  # noqa: S105
    deployed.by_name("mcp")["vars"]["GXG_API_URL"] = "https://api-production.up.railway.app"
    capsys.readouterr()
    assert run(deployed, "--verify") == 1
    out = capsys.readouterr().out
    assert "FAIL executor: secret variables equal the declared set (AS-28)" in out
    assert "GXG_SECRET__GXG_PREVIEW_KEYS" in out and "stolen-preview-key" not in out
    assert "FAIL mcp: GXG_API_URL points to *.railway.internal" in out


def test_verify_fails_while_db_init_is_still_there_and_on_shared_variables(
    deployed: MockRailway, capsys: pytest.CaptureFixture[str]
) -> None:
    set_owner_values(deployed)
    assert run(deployed, "--verify") == 1
    assert "FAIL db-init: transient job removed" in capsys.readouterr().out
    run(deployed, "--finalize")
    deployed.shared["API_KEY"] = "x"
    assert run(deployed, "--verify") == 1
    assert "FAIL (project): no shared variables" in capsys.readouterr().out


def test_a_partly_set_keyset_is_refused_not_rotated(deployed: MockRailway, capsys: pytest.CaptureFixture[str]) -> None:
    del deployed.by_name("api")["vars"]["GXG_CONFIG__GXG_EXEC_JWKS"]
    deployed.mutations.clear()
    assert run(deployed) == 1
    assert "keyset exec-signing is partly set" in capsys.readouterr().err
    assert deployed.mutations == []


def test_spec_is_valid_and_rejects_as28_violations() -> None:
    spec = rd.load_spec()
    assert rd.validate_spec(spec) == []
    bad = copy.deepcopy(spec)
    bad["services"]["executor"]["secrets"]["GXG_SECRET__GXG_PREVIEW_KEYS"] = {
        "gen": "random:32",
        "as28": "payload_preview",
    }
    bad["services"]["sweeper"]["secrets"]["GXG_SECRET__GXG_DB_DSN"]["gen"] = "db_dsn:gxg_executor"
    bad["services"]["api"]["domain"] = "api.example.com"
    problems = " | ".join(rd.validate_spec(bad))
    assert "executor: secret GXG_SECRET__GXG_PREVIEW_KEYS (payload_preview) is not allowed" in problems
    assert "sweeper: GXG_SECRET__GXG_DB_DSN uses another process's database role" in problems
    assert "api: public domain" in problems


def test_spec_matches_the_compose_layout_of_the_approval_processes() -> None:
    spec = rd.load_spec()
    compose = yaml.safe_load((ROOT / "approval/deploy/compose.yaml").read_text("utf-8"))["services"]
    for proc in ("api", "mcp", "executor", "sweeper", "decider", "ui", "notifier"):
        assert proc in spec["services"] and proc in compose
    # nothing in the spec publishes anything: no ports mapping, no public host
    text = (ROOT / "deploy/railway/services.yaml").read_text("utf-8")
    assert "up.railway.app" not in text and "TCP_PROXY" not in text
    assert "TS_AUTHKEY: {gen: owner" in text and "authkey=" not in text.lower()  # the key is the owner's, never in Git
