#!/usr/bin/env python3
"""Build, sign, verify and publish the grants bundle of the pilot (GR-11, GR-13) from `policies/pilot/`.

    RAILWAY_TOKEN=... python scripts/deploy/pilot_bundle.py            # re-issue: build, sign, verify, write variables
    RAILWAY_TOKEN=... python scripts/deploy/pilot_bundle.py --deploy   # ... and redeploy the services that read it
    python scripts/deploy/pilot_bundle.py --build-only --signing-key-env  # build with $GXG_BUNDLE_SIGNING_KEY from the env
    RAILWAY_TOKEN=... python scripts/deploy/pilot_bundle.py --check    # registry keys == live GXG_SVC_PUBLIC_KEY

The signing key lives in one place: the variable GXG_BUNDLE_SIGNING_KEY of the vault service `bundle-signer` (created by
railway_deploy.py, never deployed). This script reads it in memory, hands it to `gxg-policy build` through the child's
environment, and never prints or writes it. It writes the public half (`GXG_BUNDLE_KEYS`) and the signed bundle
(`GXG_BUNDLE_B64`, tar.gz) to the services that verify it. A bundle older than 24 h is rejected (GR-13): run this at
least every 12 h with `--deploy`, because the containers unpack the variable at start.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gxg_connector_kit import _jwt

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR))  # run as a script: scripts/deploy is on the path, the repository root is not
from scripts.deploy import railway_deploy as rd  # noqa: E402

ROOT = ROOT_DIR
POLICIES = ROOT / "policies/pilot"
KID = "pilot-bundle-1"
SIGNER = "bundle-signer"
SIGNING_VAR = "GXG_BUNDLE_SIGNING_KEY"
KEYS_VAR = "GXG_BUNDLE_KEYS"
B64_VAR = "GXG_BUNDLE_B64"
# who verifies the bundle: service -> (variable with the keys, variable with the bundle)
CONSUMERS = {
    "api": (KEYS_VAR, B64_VAR),
    "executor": (KEYS_VAR, B64_VAR),
    "ui": (KEYS_VAR, B64_VAR),
    "notifier": (KEYS_VAR, B64_VAR),
    "telegram": ("TELEGRAM_BUNDLE_KEYS", B64_VAR),
}
# registry id -> service whose GXG_SVC_PUBLIC_KEY is the registry key
REGISTRY_KEYS = {"executor": "executor", "approval-mcp": "mcp"}


def public_key(seed_b64: str) -> str:
    raw = Ed25519PrivateKey.from_private_bytes(_jwt.b64url_decode(seed_b64)).public_key().public_bytes_raw()
    return "ed25519:" + str(rd.b64url(raw))


def build(seed_b64: str, workdir: Path, out: rd.Out) -> Path:
    """`gxg-policy build` then `gxg-policy verify` into workdir/bundle; returns that directory."""
    bundle = workdir / "bundle"
    version = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env = {**os.environ, SIGNING_VAR: seed_b64, "GXG_BUNDLE_KID": KID}
    base = [sys.executable, "-m", "gxg_policy.cli"]
    built = subprocess.run(  # noqa: S603
        [*base, "build", str(POLICIES), "--out", str(bundle), "--bundle-version", version],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    out.say(built.stdout.strip() + built.stderr.strip())
    if built.returncode:
        raise rd.DeployError("gxg-policy build failed")
    checked = subprocess.run(  # noqa: S603
        [*base, "verify", str(bundle), "--kid", KID, "--public-key", public_key(seed_b64)],
        check=False,
        capture_output=True,
        text=True,
    )
    out.say(checked.stdout.strip() + checked.stderr.strip())
    if checked.returncode:
        raise rd.DeployError("gxg-policy verify rejected the bundle")
    return bundle


def pack(bundle: Path) -> str:
    """base64 of a tar.gz holding the two bundle files at its root (entrypoint.sh unpacks it to GXG_BUNDLE_DIR)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("grants-bundle.json", "grants-bundle.json.sig"):
            info = tarfile.TarInfo(name)
            data = (bundle / name).read_bytes()
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


class Pilot:
    def __init__(self, api: rd.Railway, out: rd.Out) -> None:
        self.api, self.out = api, out
        self.spec = rd.load_spec()
        self.dep = rd.Deployer(api, self.spec, out)
        self.dep.discover(create=False)

    def variables(self, service: str) -> dict[str, str]:
        return dict(self.dep.read_variables(service))

    def signing_key(self) -> str:
        key = self.variables(SIGNER).get(SIGNING_VAR)
        if not key:
            raise rd.DeployError(f"{SIGNER}.{SIGNING_VAR} is not set: run railway_deploy.py first")
        self.out.add(key)
        return key

    def check_registry(self) -> bool:
        doc = yaml.safe_load((POLICIES / "platform/registry.yaml").read_text("utf-8"))
        ok = True
        for svc in doc["services"]:
            owner = REGISTRY_KEYS.get(svc["id"])
            live = self.variables(owner).get("GXG_SVC_PUBLIC_KEY") if owner else None
            good = bool(live) and svc["public_keys"] == [live]
            ok &= good
            self.out.say(f"{'PASS' if good else 'FAIL'} registry {svc['id']}: key equals {owner}.GXG_SVC_PUBLIC_KEY")
        return ok

    def publish(self, bundle: Path, seed: str) -> list[str]:
        pub = public_key(seed)
        approval_keys = json.dumps({KID: pub})  # the approval processes: JSON kid -> key (bundle_store_from_env)
        kit_keys = f"{KID}={pub}"  # the connector kit (MCP services): `kid=key[,kid=key]`
        b64 = pack(bundle)
        done = []
        for name, (keys_var, b64_var) in CONSUMERS.items():
            if name not in self.dep.service_ids:
                continue
            self.api.call(
                rd.Q_UPSERT,
                input={
                    "projectId": self.dep.project_id,
                    "environmentId": self.dep.env_id,
                    "serviceId": self.dep.service_ids[name],
                    "variables": {
                        keys_var: kit_keys if keys_var.startswith("TELEGRAM_") else approval_keys,
                        b64_var: b64,
                    },
                    "skipDeploys": True,
                },
            )
            self.out.say(f"set {keys_var}, {b64_var} on {name} ({len(b64)} bytes)")
            done.append(name)
        return done

    def deploy(self, names: list[str]) -> None:
        for name in names:
            self.api.call(
                "mutation Deploy($s: String!, $e: String!) { serviceInstanceDeployV2(serviceId: $s, environmentId: $e) }",
                s=self.dep.service_ids[name],
                e=self.dep.env_id,
            )
            self.out.say(f"deploy {name}")


def main(argv: list[str] | None = None, *, transport: rd.Transport | None = None, out: rd.Out | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="registry public keys equal the live service keys")
    ap.add_argument("--deploy", action="store_true", help="redeploy the services that read the bundle")
    ap.add_argument("--build-only", action="store_true", help="build and verify only, do not write variables")
    ap.add_argument(
        "--signing-key-env", action="store_true", help=f"take the key from ${SIGNING_VAR}, not from Railway"
    )
    args = ap.parse_args(argv)
    out = out or rd.Out()
    try:
        if args.signing_key_env:
            seed = os.environ.get(SIGNING_VAR)
            if not seed:
                raise rd.DeployError(f"${SIGNING_VAR} is not set")
            out.add(seed)
            with tempfile.TemporaryDirectory() as tmp:
                build(seed, Path(tmp), out)
            return 0
        token = os.environ.get("RAILWAY_TOKEN")
        if token:
            out.add(token)
        if transport is None:
            if not token:
                raise rd.DeployError("RAILWAY_TOKEN is not set")
            transport = rd.http_transport(token)
        pilot = Pilot(rd.Railway(transport), out)
        if args.check:
            return 0 if pilot.check_registry() else 1
        if not pilot.check_registry():
            raise rd.DeployError("the registry in policies/pilot does not match the live keys; fix it first")
        seed = pilot.signing_key()
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build(seed, Path(tmp), out)
            if args.build_only:
                return 0
            done = pilot.publish(bundle, seed)
        if args.deploy:
            pilot.deploy(done)
        return 0
    except rd.DeployError as exc:
        out.say(f"error: {exc}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
