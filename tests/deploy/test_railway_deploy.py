"""M13 deploy kit, offline: scripts/deploy/railway_deploy.py against a mock Railway GraphQL API (no network)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt
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
    for proc in ("api", "mcp", "executor", "sweeper", "decider", "ui", "notifier", "admin"):
        assert proc in spec["services"] and proc in compose
    # nothing in the spec publishes anything: no ports mapping, no public host
    text = (ROOT / "deploy/railway/services.yaml").read_text("utf-8")
    assert "up.railway.app" not in text and "TCP_PROXY" not in text
    assert "TS_AUTHKEY: {gen: owner" in text and "authkey=" not in text.lower()  # the key is the owner's, never in Git


def test_admin_is_a_tailnet_entry_with_its_own_key_and_role(deployed: MockRailway) -> None:
    admin = deployed.by_name("admin")["vars"]
    assert admin["TS_TAGS"] == "tag:approval-admin" and admin["GXG_LISTEN"].startswith(
        "127.0.0.1:"
    )  # not on the private net
    assert admin["GXG_SECRET__GXG_DB_DSN"].startswith("postgresql://gxg_admin:")
    assert admin["GXG_ADMIN_OIDC_ISSUER"] == "${{ui.GXG_OIDC_ISSUER}}"  # the ui's IdP, by reference
    assert "GXG_SECRET__GXG_OIDC_CLIENT_SECRET" not in admin  # the client secret stays with the ui (AS-28)
    assert "gxg_admin" in json.loads(deployed.by_name("db-init")["vars"]["GXG_SECRET__GXG_ROLE_PASSWORDS"])


def test_verify_covers_admin(deployed: MockRailway, capsys: pytest.CaptureFixture[str]) -> None:
    set_owner_values(deployed)
    run(deployed, "--finalize")
    deployed.by_name("admin")["vars"]["GXG_SECRET__GXG_OIDC_CLIENT_SECRET"] = "leaked-client-secret"  # noqa: S105
    deployed.service_domains[deployed.sid("admin")] = [{"id": "d2", "domain": "admin.up.railway.app"}]
    capsys.readouterr()
    assert run(deployed, "--verify") == 1
    out = capsys.readouterr().out
    assert "FAIL admin: secret variables equal the declared set (AS-28)" in out
    assert "FAIL admin: no Railway public domain" in out
    assert "leaked-client-secret" not in out


def test_repository_services_follow_the_configured_branch(
    deployed: MockRailway, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = rd.load_spec()
    for name, svc in spec["services"].items():
        if svc.get("image") or svc["kind"] == "vault":
            assert deployed.by_name(name)["trigger"] is None
        else:
            want = svc["source"].get("branch") or spec["repo"]["branch"]
            assert deployed.by_name(name)["trigger"]["branch"] == want, (
                name
            )  # not the `main` serviceCreate fell back to
    assert deployed.by_name("telegram")["trigger"] == {
        "repository": "dmitry-v-ko/gxg-telegram-service",
        "branch": "claude/m13-railway",
    }
    # drifted by hand: the next run reconnects it, then nothing more happens
    deployed.by_name("api")["trigger"]["branch"] = "main"
    deployed.mutations.clear()
    assert run(deployed) == 0
    assert deployed.mutations == ["ServiceConnect"]
    deployed.mutations.clear()
    assert run(deployed) == 0 and deployed.mutations == []
    # and --verify reports a wrong branch
    set_owner_values(deployed)
    run(deployed, "--finalize")
    deployed.by_name("telegram")["trigger"]["branch"] = "main"
    capsys.readouterr()
    assert run(deployed, "--verify") == 1
    assert "FAIL telegram: built from dmitry-v-ko/gxg-telegram-service@claude/m13-railway" in capsys.readouterr().out


def test_any_shared_variable_fails_verify(deployed: MockRailway, capsys: pytest.CaptureFixture[str]) -> None:
    set_owner_values(deployed)
    run(deployed, "--finalize")
    capsys.readouterr()
    assert run(deployed, "--verify") == 0
    deployed.shared["OPENROUTER_API_KEY"] = "foreign-key-value"  # owner decision 2026-11-24: no exception any more
    assert run(deployed, "--verify") == 1
    out = capsys.readouterr().out
    assert "FAIL (project): no shared variables" in out and "foreign-key-value" not in out


def test_the_approval_ui_service_key_stays_with_ui_and_its_public_half_goes_to_telegram(
    deployed: MockRailway, capsys: pytest.CaptureFixture[str]
) -> None:
    """M13: the operator wizard. The seed lives in `ui` only (AS-28); telegram gets `approval-ui=ed25519:<pub>`."""
    ui, tg = deployed.by_name("ui")["vars"], deployed.by_name("telegram")["vars"]
    seed = ui["GXG_SECRET__GXG_UI_SVC_KEY"]
    pub = rd.b64url(Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(seed)).public_key().public_bytes_raw())
    assert tg["TELEGRAM_ADMIN_CALLER_KEYS"] == f"approval-ui=ed25519:{pub}"
    assert ui["GXG_TELEGRAM_ADMIN_URL"] == "http://${{telegram.RAILWAY_PRIVATE_DOMAIN}}:8082"
    assert not any(seed in v for n, s in deployed.services.items() if s["name"] != "ui" for v in s["vars"].values())
    set_owner_values(deployed)
    run(deployed, "--finalize")
    capsys.readouterr()
    assert run(deployed, "--verify") == 0
    out = capsys.readouterr().out
    assert "PASS ui: GXG_TELEGRAM_ADMIN_URL points to *.railway.internal" in out
    assert "PASS telegram: TELEGRAM_ADMIN_CALLER_KEYS is the public half of ui.GXG_SECRET__GXG_UI_SVC_KEY" in out
    # a stale public half (the key was rotated in ui only), and the seed leaked to another process: both fail
    ui["GXG_SECRET__GXG_UI_SVC_KEY"] = rd.b64url(Ed25519PrivateKey.generate().private_bytes_raw())
    deployed.by_name("executor")["vars"]["GXG_SECRET__GXG_UI_SVC_KEY"] = seed
    assert run(deployed, "--verify") == 1
    out = capsys.readouterr().out
    assert "FAIL telegram: TELEGRAM_ADMIN_CALLER_KEYS is the public half" in out
    assert "FAIL executor: secret variables equal the declared set (AS-28)" in out and seed not in out


def test_as28_refuses_the_ui_service_key_elsewhere() -> None:
    bad = copy.deepcopy(rd.load_spec())
    bad["services"]["telegram"]["secrets"]["GXG_SECRET__GXG_UI_SVC_KEY"] = {"gen": "ed25519_seed", "as28": "ui_svc_key"}
    assert "telegram: secret GXG_SECRET__GXG_UI_SVC_KEY (ui_svc_key) is not allowed" in " | ".join(
        rd.validate_spec(bad)
    )


def test_kb_service_is_private_read_only_and_gets_the_adapter_keys(deployed: MockRailway) -> None:
    """M6 KB service on Railway: from its own repository, identity keys by reference, password a secret."""
    spec = rd.load_spec()["services"]["kb"]
    assert spec["source"]["repo"] == "dmitry-v-ko/gxg-kb-service" and spec["port"] == 8080
    assert not (spec.get("domain") or spec.get("domains") or spec.get("tcp_proxy"))
    kb = deployed.by_name("kb")
    assert kb["trigger"]["repository"] == "dmitry-v-ko/gxg-kb-service"
    assert kb["vars"]["KB_IDENTITY_KEYS"] == "${{contextforge.GXG_IDENTITY_KEYS}}"
    assert set(rd.secrets_of(spec)) == {"KB_DATABASE_PASSWORD"}  # never inside KB_DATABASE_URL
    for name in ("KB_DATABASE_URL", "KB_CONNECTIONS_JSON", "KB_BUNDLE_KEYS", "GXG_BUNDLE_B64"):
        assert name in spec["owner_env"]


def test_kb_reader_sql_is_read_only_and_has_no_password() -> None:
    sql = (ROOT / "deploy/railway/sql/kb_reader.sql").read_text("utf-8")
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")).upper()
    assert "PASSWORD" not in code and "GRANT USAGE ON SCHEMA KB TO GXG_KB_READER" in code
    for word in ("DROP ", "DELETE", "TRUNCATE", "INSERT", "UPDATE ON", "SERVICE_ROLE", "BYPASSRLS;"):
        assert word not in code
    assert "BYPASSRLS" not in code and code.count("GRANT SELECT") == 2  # Supabase: not nameable, off by default


def test_the_bundle_reaches_kb_with_kit_keys() -> None:
    from scripts.deploy import pilot_bundle

    assert pilot_bundle.CONSUMERS["kb"] == ("KB_BUNDLE_KEYS", pilot_bundle.B64_VAR)
    assert (ROOT / "deploy/railway/manifests/kb/gxg-manifest.yaml").is_file()  # its tools are in the bundle's registry


def test_approval_backup_cron_is_read_only_daily_and_keeps_its_secrets_to_itself() -> None:
    spec = rd.load_spec()
    svc = spec["services"]["approval-backup"]
    assert svc["kind"] == "cron" and svc["schedule"].split()[2:] == ["*", "*", "*"]  # daily
    assert svc["env"]["PGUSER"] == "gxg_backup" and rd.ROLE_OF["approval-backup"] == "gxg_backup"
    assert svc["volume"] == "/var/backups/gxg-approval"
    assert {s["as28"] for s in svc["secrets"].values()} == {"backup_key", "db_password"}
    assert "approval-backup" in rd.AS28["backup_key"] and rd.AS28["backup_key"] == {"approval-backup"}
    assert not [p for p in rd.validate_spec(spec) if "approval-backup" in p]
    assert "check --dir" in svc["start"] and "backup --dir" in svc["start"]  # AC-M14-02 runs after every backup
    roles = (ROOT / "approval/deploy/roles.sql").read_text("utf-8")
    assert "'gxg_backup'" in roles and "GRANT pg_read_all_data TO gxg_backup" in roles
    assert "SUPERUSER" in roles and "NOBYPASSRLS" in roles  # created without privileges, only the read-all membership


def test_restore_drill_refuses_the_production_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("drill", ROOT / "scripts/ops/approval_restore_drill.py")
    assert spec and spec.loader
    drill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drill)
    for k, v in {
        "PGHOST": "db.internal", "PGUSER": "gxg_backup", "PGDATABASE": "gxg_approval", "PGPASSWORD": "x",
        "DRILL_PGHOST": "db.internal", "DRILL_PGUSER": "postgres", "DRILL_PGPASSWORD": "y",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    assert drill.main() == 2
