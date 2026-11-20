"""AC-M2-05 (deterministic build) and AC-M2-06 (verify), bundle format GR-11a. Keys are generated per test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from gxg_policy.bundle import BUNDLE, SIG, BundleError, b64url, build, jcs, public_key, sign, verify
from gxg_policy.lint import lint

HERE = Path(__file__).parent
REPO = HERE.parents[2]
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
            jcs(v["input"])


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
