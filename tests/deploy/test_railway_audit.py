"""AC-M14-11 `--audit`: canned Railway API answers (no mock state), read-only, every environment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from scripts.deploy import railway_deploy as rd

SPEC = {
    "project": "p",
    "environment": "pilot",
    "tenant": "t",
    "repo": {"name": "r"},
    "services": {
        "api": {"kind": "app", "secrets": {"DSN": {"gen": "db_dsn:gxg_api", "as28": "db_dsn"}}},
        "web": {"kind": "app"},
    },
}
SERVICES = {"edges": [{"node": {"id": "s-api", "name": "api"}}, {"node": {"id": "s-web", "name": "web"}}]}
PROJECTS = {
    "projects": {
        "edges": [
            {
                "node": {
                    "id": "p1",
                    "name": "p",
                    "services": SERVICES,
                    "environments": {
                        "edges": [
                            {"node": {"id": "e-pilot", "name": "pilot"}},
                            {"node": {"id": "e-prod", "name": "prod"}},
                        ]
                    },
                }
            }
        ]
    }
}


class Fixture:
    """Answers by operation name; `override[(op, key)]` replaces one answer. Records every operation it saw."""

    def __init__(self, override: Mapping[tuple[str, Any], Any] | None = None) -> None:
        self.override: Mapping[tuple[str, Any], Any] = override or {}
        self.seen: list[str] = []

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        op = re.search(r"(?:query|mutation)\s+(\w+)", query).group(1)  # type: ignore[union-attr]
        self.seen.append(op)
        key = variables.get("serviceId") or variables.get("environmentId")
        if (op, key) in self.override:
            return {"data": self.override[(op, key)]}
        clean: dict[str, Any] = {
            "Projects": PROJECTS,
            "Variables": {"variables": {"DSN": "x"} if variables.get("serviceId") == "s-api" else {}},
            "Domains": {"domains": {"serviceDomains": [], "customDomains": []}},
            "TcpProxies": {"tcpProxies": []},
        }
        if op == "Variables" and not variables.get("serviceId"):
            clean["Variables"] = {"variables": {}}
        return {"data": clean[op]}


def audit(fx: Fixture, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    d = rd.Deployer(rd.Railway(fx), SPEC, rd.Out())
    code = 0 if d.audit() else 1
    return code, capsys.readouterr().out


def test_clean_project_passes_in_every_environment_and_only_reads(capsys: pytest.CaptureFixture[str]) -> None:
    fx = Fixture()
    code, out = audit(fx, capsys)
    assert code == 0, out
    assert "PASS prod/api: secret names equal AS-28" in out and "PASS pilot/web: no TCP proxy" in out
    assert not any(op.endswith(("Create", "Delete", "Upsert", "Update", "Connect", "Redeploy")) for op in fx.seen)


@pytest.mark.parametrize(
    ("op", "answer", "text"),
    [
        (
            "Domains",
            {"domains": {"serviceDomains": [{"id": "d", "domain": "a.up.railway.app"}], "customDomains": []}},
            "no Railway public domain",
        ),
        (
            "Domains",
            {"domains": {"serviceDomains": [], "customDomains": [{"id": "d", "domain": "hub.example"}]}},
            "no custom domain",
        ),
        ("TcpProxies", {"tcpProxies": [{"id": "t", "proxyPort": 1}]}, "no TCP proxy"),
    ],
)
def test_exposure_fails_in_every_environment(
    op: str, answer: dict[str, Any], text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = audit(Fixture({(op, "s-web"): answer}), capsys)
    assert code == 1
    assert f"FAIL prod/web: {text}" in out and f"FAIL pilot/web: {text}" in out


def test_exposure_is_found_per_environment(capsys: pytest.CaptureFixture[str]) -> None:
    class OnlyProd(Fixture):
        def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
            if "query TcpProxies" in query and variables["environmentId"] == "e-prod":
                return {"data": {"tcpProxies": [{"id": "t", "proxyPort": 1}]}}
            return super().__call__(query, variables)

    code, out = audit(OnlyProd(), capsys)
    assert code == 1 and "FAIL prod/api: no TCP proxy" in out and "PASS pilot/api: no TCP proxy" in out


def test_any_shared_variable_fails(capsys: pytest.CaptureFixture[str]) -> None:
    for var in ("OPENROUTER_API_KEY", "SOME_SHARED"):  # no named exception (owner decision 2026-11-24)
        bad, out = audit(Fixture({("Variables", "e-prod"): {"variables": {var: "v-shared-123"}}}), capsys)
        assert bad == 1 and "FAIL prod (project): no shared variables" in out and "v-shared-123" not in out


def test_secret_names_must_equal_the_declared_set(capsys: pytest.CaptureFixture[str]) -> None:
    extra = {
        ("Variables", "s-web"): {"variables": {"GXG_SECRET__LEAK": "v", "DSN": "v"}}
    }  # a secret of another service
    code, out = audit(Fixture(extra), capsys)
    assert code == 1 and "FAIL pilot/web: secret names equal AS-28: extra=['DSN', 'GXG_SECRET__LEAK']" in out
    code, out = audit(Fixture({("Variables", "s-api"): {"variables": {}}}), capsys)
    assert code == 1 and "missing=['DSN']" in out


def test_a_service_outside_the_spec_fails(capsys: pytest.CaptureFixture[str]) -> None:
    projects = {
        "projects": {
            "edges": [
                {
                    "node": {
                        **PROJECTS["projects"]["edges"][0]["node"],
                        "services": {"edges": [*SERVICES["edges"], {"node": {"id": "s-x", "name": "stray"}}]},
                    }
                }
            ]
        }
    }
    code, out = audit(Fixture({("Projects", None): projects}), capsys)
    assert code == 1 and "FAIL pilot/stray: service is declared in the spec" in out


def test_values_never_reach_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = audit(Fixture({("Variables", "s-api"): {"variables": {"DSN": "postgres://u:SECRETPW@h/db"}}}), capsys)
    assert code == 0 and "SECRETPW" not in out
