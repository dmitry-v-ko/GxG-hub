"""Throw-away secrets and keys of the dev stand; run once before `docker compose up`: `uv run python stand/init.py`.

Writes (all git-ignored, never commit them):
  .env                         stand secrets for compose.yaml (kept if it exists)
  stand/secrets/adapter_ed25519.pem   identity adapter signing key (ContextForge only); public half in .env
  stand/secrets/bundle.seed    grants bundle signing seed; public half in .env
  stand/secrets/bundle/        grants bundle of connectors/demo/fixtures/grants, signed (GR-11)
  stand/secrets/plugins.yaml   ContextForge plugins; bootstrap.py fills in the team and gateway ids
  stand/secrets/archive-seed.sql  synthetic telegram_archive fixtures (M11, connectors/telegram/archive/seed.py)
Synthetic only: the stand never holds a real credential.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parents[1]
SECRETS = REPO / "stand" / "secrets"
ENV = REPO / ".env"
KID_ADAPTER, KID_BUNDLE = "stand-adapter-1", "stand-bundle-1"
CONNECTORS = ("demo", "telegram")  # their test grants and manifests make the stand bundle


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pub(key: Ed25519PrivateKey) -> str:
    return "ed25519:" + b64(key.public_key().public_bytes_raw())


def password() -> str:
    """Random, and passes ContextForge's policy (classes; no 3 ascending or descending letters or digits in a row)."""
    return "-".join(secrets.token_hex(1) for _ in range(20)) + "-Aa!"  # 2-character chunks: no run of 3


def env_values() -> dict[str, str]:
    return dict(line.split("=", 1) for line in ENV.read_text().splitlines() if "=" in line and line[0] != "#")


def build_bundle(seed: bytes, out: Path, built_at: str | None = None) -> None:
    """`gxg-policy build` over the demo and telegram fixtures (+ the example platform registry), signed, into `out`.

    Built in a temporary directory and moved in file by file: `demo` reloads on change (GR-12) and keeps the
    previous bundle if it ever reads a half-written pair. `bundle_version` is the commit (GR-11, GR-14).
    """
    work = SECRETS / "policies"
    shutil.rmtree(work, ignore_errors=True)
    (work / "tenants").mkdir(parents=True)
    (work / "platform").mkdir()
    for tenant, doc in merged_grants().items():
        (work / "tenants" / f"{tenant}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    shutil.copy(REPO / "policies" / "platform" / "registry.example.yaml", work / "platform")
    version = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True)
    tmp = SECRETS / "bundle.new"
    shutil.rmtree(tmp, ignore_errors=True)
    cmd = [sys.executable, "-m", "gxg_policy.cli", "build", str(work), "--out", str(tmp), "--kid", KID_BUNDLE]
    for c in CONNECTORS:
        cmd += ["--manifest", str(REPO / "connectors" / c / "gxg-manifest.yaml")]
    cmd += ["--bundle-version", version.stdout.strip()] + (["--built-at", built_at] if built_at else [])
    subprocess.run(cmd, check=True, env=os.environ | {"GXG_BUNDLE_SIGNING_KEY": b64(seed)}, stdout=subprocess.DEVNULL)
    out.mkdir(exist_ok=True)
    out.chmod(0o755)  # read by the container's non-root user
    for f in sorted(tmp.iterdir()):
        f.chmod(0o644)
        f.replace(out / f.name)
    tmp.rmdir()


def merged_grants() -> dict[str, dict[str, Any]]:
    """One file per tenant (GR-05) from the test grants of every stand connector: connections, agents and grants
    are concatenated, an agent declared by both connectors once."""
    out: dict[str, dict[str, Any]] = {}
    for c in CONNECTORS:
        for f in sorted((REPO / "connectors" / c / "fixtures" / "grants").glob("t-*.yaml")):
            doc = yaml.safe_load(f.read_text())
            t = out.setdefault(
                doc["tenant"],
                {
                    "tenant": doc["tenant"],
                    "schema_version": 1,
                    "connections": [],
                    "principals": {"agents": []},
                    "grants": [],
                },
            )
            t["connections"] += doc["connections"]
            agents = t["principals"]["agents"]
            agents += [a for a in doc["principals"]["agents"] if a not in agents]
            t["grants"] += doc["grants"]
    return out


def plugins_yaml(tenants: dict[str, str], connectors: dict[str, str]) -> str:
    """ContextForge plugins: unified_pdp first (deny before signing), then the identity adapter."""
    lines = [
        "plugin_dirs: []",
        "plugins:",
        "  - name: UnifiedPDP",
        "    kind: plugins.unified_pdp.unified_pdp.UnifiedPDPPlugin",
        "    hooks: [tool_pre_invoke, resource_pre_fetch]",
        "    mode: sequential",
        "    priority: 10",
        "    config:",
        "      engines:",
        "        - {name: native, enabled: true, priority: 1, settings: {rules_file: /run/gxg/unified_pdp.rules.json}}",
        "      combination_mode: all_must_allow",
        "      default_decision: deny",
        "      cache: {enabled: false}",
        "  - name: GxgIdentity",
        "    kind: plugins.gxg_identity.plugin.GxgIdentityPlugin",
        "    hooks: [tool_pre_invoke]",
        "    mode: sequential",
        "    priority: 1000",
        "    config:",
        "      private_key_file: /run/gxg/adapter_ed25519.pem",
        f"      kid: {KID_ADAPTER}",
        "      tenants: {"
        + ", ".join(f"'{t}': {{tenant: {n}, agent_domain: {n}.agents.gxg.example.com}}" for t, n in tenants.items())
        + "}",
        "      connectors: {" + ", ".join(f"'{g}': {a}" for g, a in connectors.items()) + "}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    SECRETS.mkdir(mode=0o700, exist_ok=True)
    os.umask(0o077)
    key_file, seed_file = SECRETS / "adapter_ed25519.pem", SECRETS / "bundle.seed"
    if not key_file.exists():
        k = Ed25519PrivateKey.generate()
        key_file.write_bytes(k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()))
    key_file.chmod(0o644)  # the ContextForge container runs as a non-root user; the directory stays 0700 on the host
    adapter = ser.load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(adapter, Ed25519PrivateKey):
        sys.exit(f"{key_file} is not an Ed25519 key")
    if not seed_file.exists():
        seed_file.write_text(b64(secrets.token_bytes(32)))
    seed = base64.urlsafe_b64decode(seed_file.read_text() + "=")
    if not ENV.exists():
        r = lambda: secrets.token_urlsafe(32)  # noqa: E731
        ENV.write_text(
            "\n".join(
                [
                    "# dev stand secrets, generated by stand/init.py; never commit",
                    f"POSTGRES_PASSWORD={r()}",
                    f"JWT_SECRET_KEY={r()}",
                    f"AUTH_ENCRYPTION_SECRET={r()}",
                    "PLATFORM_ADMIN_EMAIL=admin@operator.gxg.example.com",
                    f"PLATFORM_ADMIN_PASSWORD={password()}",
                    f"DEFAULT_USER_PASSWORD={password()}",
                    f"BASIC_AUTH_PASSWORD={password()}",
                    f"DEMO_PROVIDER_TOKEN=demo-secret-{secrets.token_hex(12)}",
                    f"DEMO_IDENTITY_KEYS={KID_ADAPTER}={pub(adapter)}",
                    f"DEMO_BUNDLE_KEYS={KID_BUNDLE}={pub(Ed25519PrivateKey.from_private_bytes(seed))}",
                ]
            )
            + "\n"
        )
    added = {"TELEGRAM_ARCHIVE_PASSWORD", "TELEGRAM_ARCHIVE_OWNER_PASSWORD"} - set(env_values())  # M11, older .env
    with ENV.open("a") as f:
        f.writelines(f"{k}={secrets.token_urlsafe(24)}\n" for k in sorted(added))
    missing = {"DEMO_IDENTITY_KEYS", "DEMO_BUNDLE_KEYS", "DEMO_PROVIDER_TOKEN"} - set(env_values())
    if missing:
        sys.exit(f".env exists without {sorted(missing)}: move it away and rerun")
    # GR-13: a bundle older than 24 h makes demo not ready; every run re-signs it (CI does it every 6 h, GR-13a)
    build_bundle(seed, SECRETS / "bundle")
    # M11: synthetic fixtures of the telegram-archive database (read by its init scripts, uid 999)
    sys.path.insert(0, str(REPO / "connectors" / "telegram" / "archive"))
    import seed as archive_seed  # noqa: PLC0415 - path of the connector's test data

    (SECRETS / "archive-seed.sql").write_text(archive_seed.seed_sql(), encoding="utf-8")
    (SECRETS / "archive-seed.sql").chmod(0o644)
    if not (SECRETS / "plugins.yaml").exists():
        (SECRETS / "plugins.yaml").write_text(plugins_yaml({}, {}))
    (SECRETS / "plugins.yaml").chmod(0o644)
    print("stand/init.py: OK")


if __name__ == "__main__":
    main()
