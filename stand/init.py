"""Throw-away secrets and keys of the dev stand; run once before `docker compose up`: `uv run python stand/init.py`.

Writes (all git-ignored, never commit them):
  .env                         stand secrets for compose.yaml (kept if it exists)
  stand/secrets/adapter_ed25519.pem   identity adapter signing key (ContextForge only); public half in .env
  stand/secrets/bundle.seed    grants bundle signing seed; public half in .env
  stand/secrets/bundle/        grants bundle of connectors/*/fixtures/grants (merged per tenant), signed (GR-11)
  stand/secrets/plugins.yaml   ContextForge plugins; bootstrap.py fills in the team and gateway ids
  stand/secrets/kb/            fake-KB keys (read per tenant, one write key), the wrapper's connections.json (M6)
Synthetic only: the stand never holds a real credential.
"""

from __future__ import annotations

import base64
import json
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
CONNECTORS = ("demo", "kb")  # their fixtures/grants and gxg-manifest.yaml go into the stand bundle


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pub(key: Ed25519PrivateKey) -> str:
    return "ed25519:" + b64(key.public_key().public_bytes_raw())


def password() -> str:
    """Random, and passes ContextForge's policy (classes; no 3 ascending or descending letters or digits in a row)."""
    return "-".join(secrets.token_hex(1) for _ in range(20)) + "-Aa!"  # 2-character chunks: no run of 3


def env_values() -> dict[str, str]:
    return dict(line.split("=", 1) for line in ENV.read_text().splitlines() if "=" in line and line[0] != "#")


def merged_grants(tenant_files: list[Path]) -> dict[str, Any]:
    """One tenant file from the fixtures of several connectors: connections, agents and grants side by side."""
    docs = [yaml.safe_load(f.read_text()) for f in tenant_files]
    principals = {
        kind: list({p["id"]: p for d in docs for p in d["principals"].get(kind, [])}.values())
        for kind in ("agents", "humans")
    }
    groups = {k: v for d in docs for k, v in d.get("groups", {}).items()}
    connections = [c for d in docs for c in d["connections"]]
    names = [g for d in docs for g in d.get("groups", {})] + [c["id"] for c in connections]
    if len(names) != len(set(names)):  # a group or connection of one connector must not shadow another's
        raise SystemExit(f"{tenant_files[0].name}: the same group or connection id in two connectors' fixtures")
    return (
        docs[0]
        | {
            "connections": connections,
            "principals": {k: v for k, v in principals.items() if v},
            "grants": [g for d in docs for g in d["grants"]],
        }
        | ({"groups": groups} if groups else {})
    )


def build_bundle(seed: bytes, out: Path, built_at: str | None = None) -> None:
    """`gxg-policy build` over the connectors' fixtures (+ the example platform registry), signed, into `out`.

    Built in a temporary directory and moved in file by file: `demo` reloads on change (GR-12) and keeps the
    previous bundle if it ever reads a half-written pair. `bundle_version` is the commit (GR-11, GR-14).
    """
    work = SECRETS / "policies"
    shutil.rmtree(work, ignore_errors=True)
    (work / "tenants").mkdir(parents=True)
    (work / "platform").mkdir()
    files = sorted(f for c in CONNECTORS for f in (REPO / "connectors" / c / "fixtures" / "grants").glob("t-*.yaml"))
    for name in sorted({f.name for f in files}):
        doc = merged_grants([f for f in files if f.name == name])
        (work / "tenants" / name).write_text(yaml.safe_dump(doc, sort_keys=False))
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


KB_WORKSPACES = {"t-alpha": "11111111-1111-4111-8111-111111111111", "t-beta": "22222222-2222-4222-8222-222222222222"}


def kb_secrets() -> None:
    """Synthetic fake-KB keys and the wrapper's KB connections (kept if they exist). The wrapper gets only the read
    key of each tenant; the write key exists to show that a read key cannot write (the shape of AC-M6-04)."""
    kb = SECRETS / "kb"
    if (kb / "connections.json").exists():
        return
    kb.mkdir(exist_ok=True)
    kb.chmod(0o755)  # read by the containers' non-root users
    keys, connections = {}, {}
    for tenant, ws in KB_WORKSPACES.items():
        key = secrets.token_urlsafe(32)
        keys[key] = {"workspace_id": ws, "role": "read"}
        (kb / f"{tenant}.key").write_text(key)
        connections[tenant] = {
            "connection_id": "kb-main",
            "url": "http://fake-kb:8080/mcp",
            "workspace_id": ws,
            "key_file": f"/run/kb/{tenant}.key",
        }
    keys[secrets.token_urlsafe(32)] = {"workspace_id": KB_WORKSPACES["t-alpha"], "role": "write"}
    (kb / "fake-keys.json").write_text(json.dumps(keys))
    (kb / "connections.json").write_text(json.dumps(connections))
    for f in kb.iterdir():
        f.chmod(0o644)


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
    missing = {"DEMO_IDENTITY_KEYS", "DEMO_BUNDLE_KEYS", "DEMO_PROVIDER_TOKEN"} - set(env_values())
    if missing:
        sys.exit(f".env exists without {sorted(missing)}: move it away and rerun")
    kb_secrets()
    # GR-13: a bundle older than 24 h makes demo not ready; every run re-signs it (CI does it every 6 h, GR-13a)
    build_bundle(seed, SECRETS / "bundle")
    if not (SECRETS / "plugins.yaml").exists():
        (SECRETS / "plugins.yaml").write_text(plugins_yaml({}, {}))
    (SECRETS / "plugins.yaml").chmod(0o644)
    print("stand/init.py: OK")


if __name__ == "__main__":
    main()
