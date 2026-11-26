"""In-process tests of the suite's helpers: test bundle (vs `gxg-policy build`), fake Approval Service, CLI."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_check import bundle, cli
from gxg_connector_check.fakes import FakeApproval
from gxg_connector_check.harness import read_verb_ok
from gxg_connector_kit import mcp, svc_jwt
from gxg_connector_kit.effects import JWKS_PATH
from gxg_connector_kit.grants import BundleStore
from gxg_connector_kit.svc_jwt import parse_registry
from gxg_policy import cli as policy_cli

REPO = next(p for p in Path(__file__).resolve().parents if (p / "connectors" / "demo").is_dir())
DEMO = REPO / "connectors" / "demo"
BUILT_AT = datetime.now(UTC).replace(microsecond=0)  # fresh: GR-13 refuses a bundle older than 24 h
BUILT_AT_Z = BUILT_AT.isoformat().replace("+00:00", "Z")


def test_bundle_is_byte_identical_to_gxg_policy_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shortcut in bundle.py stays true: same bytes as `gxg-policy build` for demo's fixtures."""
    key = Ed25519PrivateKey.generate()
    manifest = yaml.safe_load((DEMO / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    docs = bundle.grants(DEMO)
    keys = [Ed25519PrivateKey.generate().public_key() for _ in range(3)]
    platform = bundle.registry(manifest, ["t-alpha", "t-beta"], executor=keys[0], svc=keys[1], approval=keys[2])
    bundle.write(tmp_path / "ours", docs, platform, key, built_at=BUILT_AT)
    pol = tmp_path / "policies"
    (pol / "tenants").mkdir(parents=True)
    (pol / "platform").mkdir()
    for d in docs:
        (pol / "tenants" / f"{d['tenant']}.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    registry = {**platform, "services": [{k: v for k, v in s.items() if k != "tools"} for s in platform["services"]]}
    (pol / "platform" / "registry.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
    monkeypatch.setenv(policy_cli.KEY_ENV, base64.urlsafe_b64encode(key.private_bytes_raw()).rstrip(b"=").decode())
    args = ["build", str(pol), "--out", str(tmp_path / "theirs"), "--bundle-version", "0" * 40, "--kid", bundle.KID,
            "--manifest", str(DEMO / "gxg-manifest.yaml"), "--built-at", BUILT_AT_Z]  # fmt: skip
    assert policy_cli.main(args) == 0
    for f in (mcp.BUNDLE_FILE, mcp.SIG_FILE):
        assert (tmp_path / "ours" / f).read_bytes() == (tmp_path / "theirs" / f).read_bytes(), f
    store = BundleStore(mcp.parse_keys(bundle.keys_setting(key), "k"), state_file=tmp_path / "s.json")
    store.load(*((tmp_path / "ours" / f).read_bytes() for f in (mcp.BUNDLE_FILE, mcp.SIG_FILE)))
    assert store.check("t-alpha", "agent:t-alpha:reader", "read", "demo:demo-main/board/a-public").allowed


def test_bundle_carries_ext_grants_and_enabled_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M11.3/M11.4: fixture grants with `ext:read`, `ext:read:pii` and `extensions.enabled` (the shape of
    policies/tenants/example-ext-test.yaml) build the same bytes as `gxg-policy build` and resolve in the kit."""
    key = Ed25519PrivateKey.generate()
    manifest = yaml.safe_load((DEMO / "gxg-manifest.yaml").read_text(encoding="utf-8"))
    doc = yaml.safe_load((REPO / "policies" / "tenants" / "example-ext-test.yaml").read_text(encoding="utf-8"))
    for g in doc["grants"]:
        g.pop("expires_at")  # the example's terms run out; this test must not
    keys = [Ed25519PrivateKey.generate().public_key() for _ in range(3)]
    platform = bundle.registry(manifest, [doc["tenant"]], executor=keys[0], svc=keys[1], approval=keys[2])
    bundle.write(tmp_path / "ours", [doc], platform, key, built_at=BUILT_AT)
    pol = tmp_path / "policies"
    (pol / "tenants").mkdir(parents=True)
    (pol / "platform").mkdir()
    (pol / "tenants" / f"{doc['tenant']}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    registry = {**platform, "services": [{k: v for k, v in s.items() if k != "tools"} for s in platform["services"]]}
    (pol / "platform" / "registry.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
    monkeypatch.setenv(policy_cli.KEY_ENV, base64.urlsafe_b64encode(key.private_bytes_raw()).rstrip(b"=").decode())
    args = ["build", str(pol), "--out", str(tmp_path / "theirs"), "--bundle-version", "0" * 40, "--kid", bundle.KID,
            "--manifest", str(DEMO / "gxg-manifest.yaml"), "--built-at", BUILT_AT_Z]  # fmt: skip
    assert policy_cli.main(args) == 0
    for f in (mcp.BUNDLE_FILE, mcp.SIG_FILE):
        assert (tmp_path / "ours" / f).read_bytes() == (tmp_path / "theirs" / f).read_bytes(), f
    store = BundleStore(mcp.parse_keys(bundle.keys_setting(key), "k"), state_file=tmp_path / "s.json")
    store.load(*((tmp_path / "ours" / f).read_bytes() for f in (mcp.BUNDLE_FILE, mcp.SIG_FILE)))
    t, chat = doc["tenant"], doc["grants"][0]["resources"][0]
    reader, collector = (f"agent:{t}:{r}" for r in ("reader", "collector"))
    assert store.ext_check(t, reader, "ext:read:members", chat).allowed
    assert not store.ext_check(t, reader, "ext:read:pii", chat).allowed  # GR-25: never covered by ext:read
    assert store.ext_check(t, collector, "ext:read:pii", chat).allowed
    assert store.check(t, collector, "collect", chat).allowed and not store.check(t, reader, "collect", chat).allowed
    assert store.ext_enabled(t, "telegram", "chats.history") and not store.ext_enabled(t, "telegram", "chats.nope")


def test_bundle_refuses_what_it_cannot_build(tmp_path: Path) -> None:
    (tmp_path / "fixtures" / "grants").mkdir(parents=True)
    with pytest.raises(ValueError, match="no grants in"):
        bundle.grants(tmp_path)
    (tmp_path / "fixtures" / "grants" / "t.yaml").write_text("tenant: t\ngroups: {g: [a]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^test bundle: groups are not supported, list the principals$"):
        bundle.grants(tmp_path)
    with pytest.raises(ValueError, match=r"^test bundle: .* at /tenants/t/grants$"):
        bundle.write(tmp_path / "b", [{"tenant": "t"}], {"schema_version": 1, "services": []},
                     Ed25519PrivateKey.generate(), built_at=BUILT_AT)  # fmt: skip
    assert not (tmp_path / "b").exists()


def test_bundle_write_makes_its_directory(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    pub = key.public_key()
    platform = bundle.registry({"connector": "demo", "tools": []}, ["t-alpha"], executor=pub, svc=pub, approval=pub)
    docs = bundle.grants(DEMO)[:1]
    for _ in range(2):  # a second write over the first is fine
        bundle.write(tmp_path / "a" / "b", docs, platform, key, built_at=BUILT_AT)
    doc = json.loads((tmp_path / "a" / "b" / mcp.BUNDLE_FILE).read_bytes())
    assert doc["built_at"] == BUILT_AT_Z and list(doc["tenants"]) == ["t-alpha"]


def test_jwks_and_keys_setting() -> None:
    k = Ed25519PrivateKey.generate()
    doc = json.loads(bundle.jwks(k.public_key()))
    assert [j["kty"] for j in doc["keys"]] == ["OKP"]
    assert bundle.keys_setting(k) == f"gxg-check={bundle.public(k.public_key())}"
    assert bundle.public(k.public_key()).startswith("ed25519:") and len(bundle.public(k.public_key())) == 51


def fake() -> tuple[FakeApproval, Ed25519PrivateKey]:
    svc = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate().public_key()
    platform = bundle.registry({"connector": "kit", "tools": []}, ["t"], executor=other, svc=svc.public_key(),
                               approval=other)  # fmt: skip
    return FakeApproval(lambda: parse_registry(platform), b'{"keys": []}', "kit"), svc


def post(f: FakeApproval, path: str, body: Any, svc: Ed25519PrivateKey | None) -> httpx2.Response:
    """A fresh service JWT per call: the fake refuses a replayed one, like the Approval Service."""
    headers = {"authorization": f"Bearer {svc_jwt.sign(svc, iss='kit', aud='approval-api')}"} if svc else {}
    with httpx2.Client(transport=f.transport(), base_url="http://a") as h:
        return h.post(path, json=body, headers=headers)


def test_fake_approval(tmp_path: Path) -> None:
    f, svc = fake()
    tok = svc
    body = {"idempotency_key": "k1", "canonical_args_hash": "h1"}
    assert post(f, "/v1/intents", body, None).status_code == 401
    assert post(f, "/v1/intents", body, Ed25519PrivateKey.generate()).status_code == 401
    replayed = svc_jwt.sign(svc, iss="kit", aud="approval-api")
    with httpx2.Client(transport=f.transport(), base_url="http://a") as h:
        codes = [
            h.post("/v1/other", json={}, headers={"authorization": f"Bearer {replayed}"}).status_code for _ in "ab"
        ]
    assert codes == [404, 401]
    first = post(f, "/v1/intents", body, tok)
    assert first.status_code == 201 and first.json()["status"] == "pending_approval"
    again = post(f, "/v1/intents", body, tok)
    assert (again.status_code, again.json()["intent_id"]) == (200, first.json()["intent_id"])
    assert post(f, "/v1/intents", {**body, "canonical_args_hash": "h2"}, tok).status_code == 409
    assert list(f.intents) == [first.json()["intent_id"]] and f.intents[first.json()["intent_id"]] == body
    token = {"execution_token": "t"}
    assert post(f, "/v1/executions/e1/dispatch", {}, tok).status_code == 400 and f.dispatched == []
    assert post(f, "/v1/executions/e1/dispatch", token, tok).status_code == 200
    assert post(f, "/v1/executions/e1/dispatch", token, tok).status_code == 409 and f.dispatched == ["e1"]
    f.dispatch_status = 423
    assert post(f, "/v1/executions/e2/dispatch", token, tok).status_code == 423 and f.dispatched == ["e1"]
    assert post(f, "/v1/other", {}, tok).status_code == 404
    with httpx2.Client(transport=f.transport(), base_url="http://a") as h:
        assert h.get(JWKS_PATH).content == b'{"keys": []}'
        f.down = True
        with pytest.raises(httpx2.ConnectError):
            h.get(JWKS_PATH)


def test_cli_on_demo_and_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "r.json"
    assert cli.main(["--connector", str(DEMO), "--report", str(report)]) == 0
    results = json.loads(report.read_text(encoding="utf-8"))
    # demo has effects since M8: EX-03 applies (a read-only connector: tests/toolkit, sample without effects)
    assert results["SC-02"]["status"] == "passed" and results["EX-03"]["status"] == "passed"
    assert "SC-02    passed" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.main(["--connector", str(tmp_path)])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("name", "allowed"),
    [
        ("kb.documents.search", True),
        ("telegram.participants.collect", True),
        ("telegram.ext.describe", True),
        ("x.ext.call", True),
        ("x.describe", False),
        ("x.call", False),
        ("x.foo.call", False),
        ("x.foo.describe", False),
        ("x.ext.foo", False),
        ("x.y.ext.call", False),
    ],
)
def test_read_verbs_of_mc01(name: str, allowed: bool) -> None:
    """MCC-01: `describe` and `call` are read verbs only as the extension meta-tools `<domain>.ext.<verb>`."""
    assert read_verb_ok(name) is allowed
