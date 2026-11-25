"""M14 prep: the planned cron service `bundle-reissue` (services.yaml `planned`): valid description, and one run of
its command, `pilot_bundle.py --deploy`, against a mock Railway re-issues a fresh, verifiable bundle."""

from __future__ import annotations

import base64
import copy
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt
from scripts.deploy import pilot_bundle as pb
from scripts.deploy import railway_deploy as rd
from tests.deploy.mock_railway import MockRailway

SPEC = rd.load_spec()
PLANNED = SPEC["planned"]["bundle-reissue"]


def test_the_planned_service_is_described_but_not_part_of_the_deployment(capsys: pytest.CaptureFixture[str]) -> None:
    assert "bundle-reissue" not in SPEC["services"]  # apply, --verify and --audit never see it
    assert rd.validate_planned(SPEC) == []
    assert PLANNED["start"] == "python scripts/deploy/pilot_bundle.py --deploy"
    assert "GXG_BUNDLE_SIGNING_KEY" not in PLANNED["secrets"]  # the key stays in bundle-signer
    assert rd.main(["--dry-run"]) == 0
    assert "bundle-reissue [cron, PLANNED, not deployed] cron 0 */6 * * *" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"schedule": "0 */13 * * *"}, "every N hours, N <= 12"),
        ({"schedule": "0 3 * * *"}, "every N hours, N <= 12"),
        ({"domain": "x.up.railway.app"}, "public domain"),
        ({"secrets": {"GXG_BUNDLE_SIGNING_KEY": {"gen": "owner", "as28": "bundle_signing"}}}, "stays in bundle-signer"),
        ({"secrets": {"RAILWAY_TOKEN": {"gen": "owner", "as28": "db_dsn"}}}, "not in the AS-28 table"),
    ],
)
def test_the_planned_service_is_refused_when_it_breaks_a_rule(change: dict[str, Any], problem: str) -> None:
    spec = copy.deepcopy(SPEC)
    spec["planned"]["bundle-reissue"].update(change)
    assert any(problem in p for p in rd.validate_planned(spec))


def test_a_run_of_the_job_reissues_a_fresh_bundle_for_every_consumer(capsys: pytest.CaptureFixture[str]) -> None:
    mock = MockRailway()
    assert rd.main([], transport=mock, out=rd.Out()) == 0
    registry = yaml.safe_load((pb.POLICIES / "platform/registry.yaml").read_text("utf-8"))
    for svc in registry["services"]:  # the live keys are the ones the registry names (check_registry)
        owner, var = pb.REGISTRY_KEYS[svc["id"]]
        mock.by_name(owner)["vars"][var] = svc["public_keys"][0]
    seed = mock.by_name("bundle-signer")["vars"]["GXG_BUNDLE_SIGNING_KEY"]
    capsys.readouterr()
    before = datetime.now(UTC) - timedelta(seconds=5)

    assert pb.main(["--deploy"], transport=mock, out=rd.Out()) == 0  # the cron command

    seen = capsys.readouterr()
    assert seed not in seen.out + seen.err  # the key is used in memory only
    assert sorted(mock.deploys) == sorted(mock.sid(n) for n in pb.CONSUMERS)  # every reader is redeployed
    public = pb.public_key(seed)
    for name, (keys_var, _) in pb.CONSUMERS.items():
        variables = mock.by_name(name)["vars"]
        assert pb.KID in variables[keys_var]
        assert public in variables[keys_var]
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(variables["GXG_BUNDLE_B64"])), mode="r:gz") as tar:
            bundle = tar.extractfile("grants-bundle.json")
            sig = tar.extractfile("grants-bundle.json.sig")
            assert bundle and sig
            data, signature = bundle.read(), json.loads(sig.read())
        built = datetime.strptime(json.loads(data)["built_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert before <= built <= datetime.now(UTC) + timedelta(seconds=5)  # fresh: GR-13 allows 24 h
        key = Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(seed)).public_key()
        key.verify(_jwt.b64url_decode(signature["sig"]), data)  # signed by the key kept in bundle-signer


def test_switched_on_it_becomes_a_cron_service_with_its_schedule() -> None:
    spec = copy.deepcopy(SPEC)
    spec["services"]["bundle-reissue"] = spec["planned"].pop("bundle-reissue")
    mock = MockRailway()
    rd.Deployer(rd.Railway(mock), spec, rd.Out()).apply()
    instance = mock.by_name("bundle-reissue")["instance"]
    assert instance["cronSchedule"] == "0 */6 * * *" and instance["restartPolicyType"] == "NEVER"
    assert instance["startCommand"] == "python scripts/deploy/pilot_bundle.py --deploy"
