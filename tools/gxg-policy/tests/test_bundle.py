"""AC-M2-05 (deterministic build) and AC-M2-06 (verify), bundle format GR-11a. Keys are generated per test."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from gxg_policy.bundle import (
    BUNDLE,
    SIG,
    BundleError,
    b64url,
    build,
    check_shape,
    jcs,
    public_key,
    sign,
    unb64url,
    verify,
)
from gxg_policy.lint import lint
from gxg_policy.load import REPO_ROOT

HERE = Path(__file__).parent
REPO = REPO_ROOT
GOOD = HERE / "good"
SHA = "0123456789abcdef0123456789abcdef01234567"


def seed() -> bytes:
    return os.urandom(32)


def run_build(out: Path, key: bytes, built_at: str, hashseed: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GXG_BUNDLE_SIGNING_KEY": b64url(key), "PYTHONHASHSEED": hashseed}
    cmd = [sys.executable, "-m", "gxg_policy.cli", "build", str(REPO / "policies"), "--out", str(out)]
    cmd += ["--bundle-version", SHA, "--built-at", built_at, "--kid", "ci-test"]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)


def without_built_at(data: bytes) -> bytes:
    doc = json.loads(data)
    doc.pop("built_at")
    return jcs(doc)


def test_ac_m2_05_two_builds_are_byte_identical(tmp_path: Path) -> None:
    # Separate processes, different hash seeds, keys and times: only built_at and the signature may differ.
    a = run_build(tmp_path / "a", seed(), "2026-11-20T10:00:00Z", "1")
    b = run_build(tmp_path / "b", seed(), "2026-11-21T11:30:00Z", "2")
    assert a.returncode == 0 and b.returncode == 0, a.stderr + b.stderr
    da, db = (tmp_path / "a" / BUNDLE).read_bytes(), (tmp_path / "b" / BUNDLE).read_bytes()
    assert da != db  # built_at differs
    assert without_built_at(da) == without_built_at(db)
    c = run_build(tmp_path / "c", seed(), "2026-11-20T10:00:00Z", "3")
    assert c.returncode == 0 and (tmp_path / "c" / BUNDLE).read_bytes() == da


def test_bundle_format_gr_11a(tmp_path: Path) -> None:
    key = seed()
    build(
        lint(GOOD / "policies", [GOOD / "manifest.yaml"]),
        tmp_path,
        bundle_version=SHA,
        built_at="2026-11-20T10:00:00Z",
        seed=key,
        kid="k1",
    )
    data = (tmp_path / BUNDLE).read_bytes()
    doc = json.loads(data)
    assert data == jcs(doc)
    assert set(doc) == {"schema_version", "bundle_version", "built_at", "tenants", "platform"}
    assert (doc["schema_version"], doc["bundle_version"], doc["built_at"]) == (1, SHA, "2026-11-20T10:00:00Z")
    t = doc["tenants"]["t-alpha"]
    assert set(t) == {"grants", "approval_policy"}
    assert t["grants"]["grants"][0]["subjects"] == ["agent:t-alpha:hermes"]  # group:researchers expanded
    assert t["grants"]["groups"] == {"researchers": ["agent:t-alpha:hermes"]}
    assert t["approval_policy"]["tenant"] == "t-alpha"
    assert doc["platform"]["services"][0]["id"] == "demo"
    sig = json.loads((tmp_path / SIG).read_bytes())
    assert set(sig) == {"alg", "kid", "sig"} and sig["alg"] == "EdDSA" and sig["kid"] == "k1"
    assert "=" not in sig["sig"] and len(sig["sig"]) == 86  # 64 bytes, base64url without padding


def test_tenant_without_approval_policy_is_null(tmp_path: Path) -> None:
    build(lint(REPO / "policies"), tmp_path, bundle_version=SHA, built_at="2026-11-20T10:00:00Z", seed=seed(), kid="k1")
    doc = json.loads((tmp_path / BUNDLE).read_bytes())
    assert doc["tenants"]["t-alpha"]["approval_policy"] is not None
    pol = lint(GOOD / "policies", [GOOD / "manifest.yaml"])
    pol.approval.clear()
    build(pol, tmp_path, bundle_version=SHA, built_at="2026-11-20T10:00:00Z", seed=seed(), kid="k1")
    assert json.loads((tmp_path / BUNDLE).read_bytes())["tenants"]["t-alpha"]["approval_policy"] is None


def test_build_refuses_policies_with_errors(tmp_path: Path) -> None:
    pol = lint(HERE / "bad/01-dangling-subject/policies")
    with pytest.raises(BundleError, match="policies have errors"):
        build(pol, tmp_path, bundle_version=SHA, built_at="2026-11-20T10:00:00Z", seed=seed(), kid="k1")


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, bytes]:
    key = seed()
    assert run_build(tmp_path, key, "2026-11-20T10:00:00Z", "0").returncode == 0
    return tmp_path, key


def cli_verify(d: Path, key: bytes, kid: str = "ci-test") -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "gxg_policy.cli", "verify", str(d), "--kid", kid, "--public-key", public_key(key)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_ac_m2_06_verify_accepts_built_bundle(built: tuple[Path, bytes]) -> None:
    d, key = built
    r = cli_verify(d, key)
    assert r.returncode == 0, r.stderr
    assert f"bundle_version={SHA}" in r.stdout


def test_ac_m2_06_verify_rejects_any_changed_byte(built: tuple[Path, bytes]) -> None:
    d, key = built
    data = (d / BUNDLE).read_bytes()
    keys = {"ci-test": public_key(key)}
    sig = (d / SIG).read_bytes()
    for i in sorted({0, len(data) // 2, len(data) - 1, *range(1, len(data), 97)}):
        bad = bytearray(data)
        bad[i] ^= 0x01
        with pytest.raises(BundleError):
            verify(bytes(bad), sig, keys)
    bad = bytearray(data)
    bad[len(data) // 2] ^= 0x01
    (d / BUNDLE).write_bytes(bytes(bad))
    r = cli_verify(d, key)
    assert r.returncode == 1 and "bundle rejected" in r.stderr


def test_verify_rejects_wrong_key_kid_alg_and_missing_sig(built: tuple[Path, bytes]) -> None:
    d, key = built
    data, sig = (d / BUNDLE).read_bytes(), (d / SIG).read_bytes()
    assert cli_verify(d, seed()).returncode == 1  # foreign key
    assert cli_verify(d, key, kid="other").returncode == 1  # unknown kid
    keys = {"ci-test": public_key(key)}
    for alg in ("none", "ES256"):
        forged = json.loads(sig)
        forged["alg"] = alg
        with pytest.raises(BundleError):
            verify(data, json.dumps(forged).encode(), keys)
    (d / SIG).unlink()
    assert cli_verify(d, key).returncode == 1


def test_verify_rejects_signed_but_not_canonical(built: tuple[Path, bytes]) -> None:
    d, key = built
    pretty = json.dumps(json.loads((d / BUNDLE).read_bytes()), indent=1).encode()
    with pytest.raises(BundleError, match="JCS"):
        verify(pretty, sign(pretty, key, "ci-test"), {"ci-test": public_key(key)})


@pytest.mark.parametrize(
    "change",
    [{"schema_version": 2}, {"extra": 1}, {"built_at": None}],
    ids=["schema_version-2", "extra-key", "missing-key"],
)
def test_verify_rejects_validly_signed_wrong_shape(built: tuple[Path, bytes], change: dict[str, object]) -> None:
    d, key = built
    doc = json.loads((d / BUNDLE).read_bytes())
    doc.update(change)
    doc = {k: v for k, v in doc.items() if v is not None}
    data = jcs(doc)
    with pytest.raises(BundleError, match="not a v1 bundle"):
        verify(data, sign(data, key, "ci-test"), {"ci-test": public_key(key)})


def test_verify_names_unknown_kid(built: tuple[Path, bytes]) -> None:
    d, key = built
    with pytest.raises(BundleError, match="unknown kid"):
        verify((d / BUNDLE).read_bytes(), (d / SIG).read_bytes(), {"other": public_key(key)})


@pytest.mark.parametrize(
    "built_at",
    ["yesterday", "2026-11-20T10:00:00+00:00", "2026-11-20 10:00:00Z", "2026-13-01T00:00:00Z", "2026-1-1T0:0:0Z"],
)
def test_build_rejects_bad_built_at(tmp_path: Path, built_at: str) -> None:
    r = run_build(tmp_path, seed(), built_at, "0")
    assert r.returncode == 2 and "built-at" in r.stderr
    assert not (tmp_path / BUNDLE).exists()


def test_gr_13a_rebuild_without_flag_only_changes_built_at(tmp_path: Path) -> None:
    # The scheduled CI job runs `build` without --built-at on the same commit: same content, fresh built_at.
    env = {**os.environ, "GXG_BUNDLE_SIGNING_KEY": b64url(seed())}
    out = []
    for name in ("a", "b"):
        cmd = [sys.executable, "-m", "gxg_policy.cli", "build", str(REPO / "policies"), "--out", str(tmp_path / name)]
        r = subprocess.run(
            [*cmd, "--bundle-version", SHA, "--kid", "k"], capture_output=True, text=True, env=env, check=False
        )
        assert r.returncode == 0, r.stderr
        out.append((tmp_path / name / BUNDLE).read_bytes())
    assert without_built_at(out[0]) == without_built_at(out[1])
    for data in out:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", json.loads(data)["built_at"])


def test_build_without_key_is_a_usage_error(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "GXG_BUNDLE_SIGNING_KEY"}
    cmd = [
        sys.executable,
        "-m",
        "gxg_policy.cli",
        "build",
        str(REPO / "policies"),
        "--out",
        str(tmp_path),
        "--kid",
        "k",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 2 and not (tmp_path / BUNDLE).exists()


def test_jcs_matches_canonical_vectors_without_nfc() -> None:
    vectors = json.loads((REPO / "schemas/test-vectors/canonical-hash.json").read_text())
    checked = 0
    for v in vectors["vectors"]:
        if all(unicodedata.is_normalized("NFC", s) for s in _strings(v["input"])):
            assert jcs(v["input"]).decode() == v["canonical"], v["name"]
            checked += 1
    assert checked >= 3
    for v in vectors["invalid_inputs"]:
        with pytest.raises(BundleError):
            jcs(v["input"] if "input" in v else json.loads(v["input_json"]))


def test_jcs_orders_keys_by_utf16_code_units() -> None:
    # U+FB01 sorts after U+1F600 by code point but before it by UTF-16 code units (0xD83D < 0xFB01).
    assert jcs({"ﬁ": 1, "\U0001f600": 2, "a": 3}) == '{"a":3,"\U0001f600":2,"ﬁ":1}'.encode()


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (k, *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


VECTOR = json.loads((REPO / "schemas/test-vectors/bundle-signature.json").read_text())


def test_signature_vector_is_reproduced_by_build_and_accepted_by_verify(tmp_path: Path) -> None:
    # GR-11a: policies + manifest that yield the vector's bundle -> `build` writes exactly its bytes and .sig.
    vb = VECTOR["bundle"]
    root = tmp_path / "policies"
    (root / "tenants").mkdir(parents=True)
    (root / "platform").mkdir()
    (root / "tenants/t-alpha.yaml").write_text(json.dumps(vb["tenants"]["t-alpha"]["grants"]))
    platform = json.loads(json.dumps(vb["platform"]))
    vector_tools = platform["services"][0].pop("tools")
    (root / "platform/registry.yaml").write_text(json.dumps(platform))
    manifest = tmp_path / "gxg-manifest.yaml"
    tools = [{**t, "resource": {"type": "board"}, "input_schema": "x.json", "title": "t"} for t in vector_tools]
    manifest.write_text(
        json.dumps({"connector": "demo", "version": "1.0.0", "contract": "gxg-mcp-connector/v1", "tools": tools})
    )
    seed_ = hashlib.sha256(b"gxg-hub test vector bundle key v1 - NOT A SECRET").digest()
    assert public_key(seed_) == VECTOR["public_key"]
    env = {**os.environ, "GXG_BUNDLE_SIGNING_KEY": b64url(seed_)}
    cmd = [sys.executable, "-m", "gxg_policy.cli", "build", str(root), "--out", str(tmp_path / "out")]
    cmd += ["--bundle-version", vb["bundle_version"], "--built-at", vb["built_at"], "--kid", VECTOR["kid"]]
    r = subprocess.run([*cmd, "--manifest", str(manifest)], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / BUNDLE).read_bytes() == base64.b64decode(VECTOR["bundle_bytes_b64"])
    assert json.loads((tmp_path / "out" / SIG).read_bytes()) == VECTOR["signature_file"]
    assert (tmp_path / "out" / SIG).read_bytes() == jcs(VECTOR["signature_file"])
    assert cli_verify(tmp_path / "out", seed_, VECTOR["kid"]).returncode == 0
    vector_sig = jcs(VECTOR["signature_file"])
    data = base64.b64decode(VECTOR["bundle_bytes_b64"])
    assert verify(data, vector_sig, {VECTOR["kid"]: VECTOR["public_key"]}) == vb


def _bundle(pol_root: Path, manifests: list[Path], tmp_path: Path) -> dict[str, object]:
    build(
        lint(pol_root, manifests), tmp_path, bundle_version=SHA, built_at="2026-11-20T10:00:00Z", seed=seed(), kid="k"
    )
    doc: dict[str, object] = json.loads((tmp_path / BUNDLE).read_bytes())
    return doc


def test_build_takes_connector_tools_from_manifests(tmp_path: Path) -> None:
    # GR-11a / AS-26: only the projected fields, from the manifest whose `connector` is the service id.
    doc = _bundle(GOOD / "policies", [GOOD / "manifest.yaml"], tmp_path)
    services = {s["id"]: s for s in doc["platform"]["services"]}  # type: ignore[index]
    assert services["demo"]["tools"] == [
        {
            "name": "demo.wire.propose",
            "version": "1.0.0",
            "effect_class": "high_risk",
            "action": "propose",
            "executes_via": "demo.wire.execute",
            "preview": {"title": "Перевод", "fields": {"amount": "Сумма"}},
        },
        {
            "name": "demo.message.propose",
            "version": "1.0.0",
            "effect_class": "external_effect",
            "action": "propose",
            "executes_via": "demo.message.execute",
            "dispatch_timeout_s": 30,
            "preview": {"title": "Сообщение", "fields": {"text": "Текст"}},
        },
    ]
    assert all("tools" not in s for s in services.values() if s["kind"] != "connector")


def test_build_without_manifest_gives_connectors_empty_tools(tmp_path: Path) -> None:
    # Hand-written tools in the registry are dropped by build (lint warns); with no manifest the bundle carries [].
    root = tmp_path / "policies"
    shutil.copytree(REPO / "policies", root)
    reg = root / "platform/registry.example.yaml"
    reg.write_text(reg.read_text().replace(
        "    internal_url: https://demo.internal:8081\n",
        "    internal_url: https://demo.internal:8081\n"
        "    tools:\n      - { name: demo.notes.search, version: 1.0.0, effect_class: read, action: read }\n",
    ))  # fmt: skip
    doc = _bundle(root, [], tmp_path / "out")
    connectors = [s for s in doc["platform"]["services"] if s["kind"] == "connector"]  # type: ignore[index]
    assert connectors and all(s["tools"] == [] for s in connectors)
    warnings = [f for f in lint(root, []).findings if f.code == "ignored-tools"]
    assert [(f.file, f.path, f.level) for f in warnings] == [
        ("platform/registry.example.yaml", "services[2].tools", "warning")
    ]


def test_build_rejects_a_bundle_outside_the_schema(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match=r"not a v1 bundle \(grants-bundle.schema.json\): bundle_version"):
        build(
            lint(GOOD / "policies"),
            tmp_path,
            bundle_version="v1",
            built_at="2026-11-20T10:00:00Z",
            seed=seed(),
            kid="k",
        )
    assert not (tmp_path / BUNDLE).exists()


@pytest.mark.parametrize(
    ("text", "code", "path"),
    [
        ("connector: demo\nversion: 1.0.0\ncontract: gxg-mcp-connector/v1\ntools: []\n", "schema", "tools"),
        ("connector: [\n", "manifest", ""),
    ],
)
def test_bad_manifest_is_a_finding_with_file_and_path(tmp_path: Path, text: str, code: str, path: str) -> None:
    m = tmp_path / "gxg-manifest.yaml"
    m.write_text(text)
    errors = lint(GOOD / "policies", [m]).errors
    assert [(e.file, e.path, e.code) for e in errors] == [(str(m), path, code)]


def test_second_manifest_for_one_connector_is_an_error() -> None:
    errors = lint(GOOD / "policies", [GOOD / "manifest.yaml"] * 2).errors
    assert [(e.path, e.code) for e in errors] == [("connector", "duplicate-id")]


# DoD-4 (mutmut): exact error texts, no mutation of the loaded policies, edge encodings.
def test_error_messages(tmp_path: Path) -> None:
    kw: dict[str, Any] = {"bundle_version": SHA, "built_at": "2026-11-20T10:00:00Z", "seed": seed(), "kid": "k"}
    bad = lint(HERE / "lint-all/policies", [HERE / "lint-all/manifest.yaml"])
    with pytest.raises(BundleError) as exc:
        build(bad, tmp_path, **kw)
    assert str(exc.value) == "policies have errors:\n" + "\n".join(map(str, bad.errors)) and len(bad.errors) > 2
    pol = lint(GOOD / "policies", [GOOD / "manifest.yaml"])
    pol.platform = None
    with pytest.raises(BundleError, match=r"^no platform registry under platform/$"):
        build(pol, tmp_path, **kw)
    pol = lint(GOOD / "policies", [GOOD / "manifest.yaml"])
    pol.approval["t-zeta"] = pol.approval["t-alpha"]
    with pytest.raises(BundleError, match=r"^approval policy for unknown tenant 't-zeta'$"):
        build(pol, tmp_path, **kw)
    with pytest.raises(BundleError, match=r"^signing key must be a 32-byte Ed25519 seed \(base64url\)$"):
        sign(b"x", b"short", "k")
    with pytest.raises(BundleError, match=r"^not allowed in a bundle \(float or unsafe int\): 1\.5$"):
        jcs({"a": 1.5})
    with pytest.raises(BundleError, match=r"^unsupported value b'x'$"):
        jcs({"a": b"x"})
    assert jcs({"a": [2**53 - 1, -(2**53 - 1), True, None]}) == b'{"a":[9007199254740991,-9007199254740991,true,null]}'


def test_shape_errors_name_the_first_path(built: tuple[Path, bytes]) -> None:
    d, _ = built
    doc = json.loads((d / BUNDLE).read_bytes())
    doc["tenants"]["t-alpha"]["grants"]["grants"][0]["actions"] = ["fly"]
    doc["bundle_version"] = "x"
    with pytest.raises(BundleError) as exc:
        check_shape(doc)
    assert str(exc.value).startswith("not a v1 bundle (grants-bundle.schema.json): bundle_version: 'x' does not match")
    del doc["bundle_version"]
    with pytest.raises(BundleError, match=r"^not a v1 bundle \(grants-bundle.schema.json\): <root>: 'bundle_version'"):
        check_shape(doc)
    doc["bundle_version"] = SHA
    with pytest.raises(
        BundleError, match=r": tenants/t-alpha/grants/grants/0/actions/0: 'fly' is not valid under any of"
    ):
        check_shape(doc)


def test_verify_error_messages(built: tuple[Path, bytes]) -> None:
    d, key = built
    data, sig = (d / BUNDLE).read_bytes(), (d / SIG).read_bytes()
    keys = {"ci-test": public_key(key)}
    with pytest.raises(BundleError, match=r"^signature file: expected \{alg: EdDSA, kid, sig\}$"):
        verify(data, b'{"alg": "EdDSA"}', keys)
    with pytest.raises(BundleError, match=r"^bad signature$"):
        verify(data, sign(data, seed(), "ci-test"), keys)
    with pytest.raises(BundleError, match=r"^malformed: "):
        verify(data, b"not json", keys)
    pretty = json.dumps(json.loads(data), indent=1).encode()
    with pytest.raises(BundleError, match=r"^bundle is not in JCS form$"):
        verify(pretty, sign(pretty, key, "ci-test"), keys)
    assert verify(data, sig, keys)["bundle_version"] == SHA


def test_build_leaves_the_loaded_policies_unchanged(tmp_path: Path) -> None:
    pol = lint(GOOD / "policies", [GOOD / "manifest.yaml"])
    before = json.dumps([pol.tenants, pol.platform], sort_keys=True)
    build(pol, tmp_path / "a" / "b", bundle_version=SHA, built_at="2026-11-20T10:00:00Z", seed=seed(), kid="k")
    assert json.dumps([pol.tenants, pol.platform], sort_keys=True) == before
    assert (tmp_path / "a" / "b" / BUNDLE).exists()  # nested --out is created


def test_b64url_round_trip_without_padding() -> None:
    for raw in (b"", b"\x00", b"\x00\x00", b"\x00\x00\x17", bytes(range(32))):
        text = b64url(raw)
        assert "=" not in text and unb64url(text) == raw
    assert b64url(b"\x00\x00\x17") == "AAAX"  # a trailing X is data, not padding
