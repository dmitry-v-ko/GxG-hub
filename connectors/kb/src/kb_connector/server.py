"""MCP server of the KB wrapper: FastMCP over Streamable HTTP plus /healthz and /readyz (MCC-09).

Configuration (environment; no secrets, only public keys and paths):
  GXG_PROFILE          `test` enables the test identity issuer; anything else is production
  KB_BUNDLE_DIR        directory with grants-bundle.json and grants-bundle.json.sig (GR-11a)
  KB_BUNDLE_KEYS       `kid=ed25519:<base64url>[,kid=...]`: bundle verification keys (never from the bundle)
  KB_STATE_FILE        newest loaded `built_at`, survives restarts (GR-13a)
  KB_IDENTITY_KEYS     `kid=ed25519:<base64url>[,kid=...]`: ContextForge identity adapter keys (ADR-0001 C);
                       without them every call is refused
  KB_TEST_ISSUER_KEY   test profile only: public key of the test identity issuer
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from gxg_connector_kit import TokenError, parse_public_key
from gxg_connector_kit.cf_identity import ContextForgeVerifier
from gxg_connector_kit.grants import BundleError, BundleStore
from gxg_connector_kit.identity import IdentityVerifier, StubVerifier, VerifiedIdentity

from .backend import Backend
from .tools import Connector, errors

CONNECTOR_ID = "kb"  # `aud` of identity assertions; id in the platform registry
log = logging.getLogger("kb_connector")
BUNDLE_FILE, SIG_FILE = "grants-bundle.json", "grants-bundle.json.sig"  # GR-11a


class ConfigError(Exception):
    """The service must not start."""


@dataclass(frozen=True)
class Settings:
    profile: str
    bundle_dir: Path
    bundle_keys: str
    state_file: Path
    test_issuer_key: str | None
    identity_keys: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        s = cls(
            profile=env.get("GXG_PROFILE", "production"),
            bundle_dir=Path(env.get("KB_BUNDLE_DIR", "/bundle")),
            bundle_keys=env.get("KB_BUNDLE_KEYS", ""),
            state_file=Path(env.get("KB_STATE_FILE", "/var/lib/kb/bundle-state.json")),
            test_issuer_key=env.get("KB_TEST_ISSUER_KEY") or None,
            identity_keys=env.get("KB_IDENTITY_KEYS", ""),
        )
        if s.profile != "test" and s.test_issuer_key is not None:
            raise ConfigError("the test issuer is allowed only with GXG_PROFILE=test")
        if s.test_issuer_key is not None and s.identity_keys:
            raise ConfigError("KB_TEST_ISSUER_KEY and KB_IDENTITY_KEYS are exclusive")
        return s


# shortcut: NoVerifier, parse_keys and BundleFiles are copies of demo_connector.server (second connector);
# move them into gxg-connector-kit with KbTool (tools.py).
class NoVerifier:
    """No identity adapter keys configured: refuses every call (fail closed)."""

    def verify(self, headers: Mapping[str, str], **_: Any) -> VerifiedIdentity:
        raise TokenError("no identity verifier configured")


def parse_keys(value: str, name: str) -> dict[str, Ed25519PublicKey]:
    keys = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        kid, sep, key = item.partition("=")
        if not sep or not kid:
            raise ConfigError(f"{name}: expected kid=ed25519:<base64url>")
        try:
            keys[kid] = parse_public_key(key)
        except ValueError as exc:
            raise ConfigError(f"{name}: {exc}") from None
    return keys


class BundleFiles:
    """Loads the bundle at start and again whenever its files change (GR-12). A bad file keeps the previous bundle."""

    def __init__(self, store: BundleStore, directory: Path) -> None:
        self.store = store
        self.directory = directory
        self._seen: tuple[int, int] | None = None

    def refresh(self) -> None:
        try:
            stamp = ((self.directory / BUNDLE_FILE).stat().st_mtime_ns, (self.directory / SIG_FILE).stat().st_mtime_ns)
        except OSError:
            return
        if stamp == self._seen:
            return
        self._seen = stamp
        try:
            bundle = self.store.load(
                (self.directory / BUNDLE_FILE).read_bytes(), (self.directory / SIG_FILE).read_bytes()
            )
            log.info(json.dumps({"event": "bundle_loaded", "bundle_version": bundle.version}))
        except (BundleError, OSError) as exc:
            log.warning(json.dumps({"event": "bundle_rejected", "reason": str(exc)}))


def create_app(
    connector: Connector, verifier: IdentityVerifier, refresh: Callable[[], None], store: BundleStore
) -> Starlette:
    mcp = FastMCP("kb")
    for tool in connector.tools(verifier, refresh):
        mcp.add_tool(tool)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        # Read-only connector: KB (in-process fake-KB) + valid bundle; no Approval Service (MCC-09).
        refresh()
        ready = store.readiness()
        if not ready.ready:
            log.warning(json.dumps({"event": "not_ready", "reasons": list(ready.reasons)}))
        return JSONResponse(
            {"status": "ready" if ready.ready else "not_ready"}, status_code=200 if ready.ready else 503
        )

    return mcp.http_app(stateless_http=True, json_response=True)


def build(settings: Settings, backend: Backend | None = None) -> tuple[Starlette, Connector]:
    backend = backend or Backend()
    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    store = BundleStore(parse_keys(settings.bundle_keys, "KB_BUNDLE_KEYS"), state_file=settings.state_file)
    files = BundleFiles(store, settings.bundle_dir)
    files.refresh()
    verifier: IdentityVerifier = NoVerifier()
    if settings.test_issuer_key is not None:
        verifier = StubVerifier(CONNECTOR_ID, parse_public_key(settings.test_issuer_key))
    elif settings.identity_keys:
        verifier = ContextForgeVerifier(CONNECTOR_ID, parse_keys(settings.identity_keys, "KB_IDENTITY_KEYS"))
    connector = Connector(backend, store)
    return create_app(connector, verifier, files.refresh, store), connector


def main(env: Mapping[str, str] = os.environ) -> int:
    # JSON log lines (audit, events) to stdout; stack frames of internal errors to stderr only.
    out, err = logging.StreamHandler(sys.stdout), logging.StreamHandler()  # stderr
    out.addFilter(lambda r: r.name != errors.name)
    err.addFilter(lambda r: r.name == errors.name)
    for h in (out, err):
        h.setFormatter(logging.Formatter())  # the message only
    logging.basicConfig(level=logging.INFO, handlers=[out, err])
    try:
        app, _ = build(Settings.from_env(env))
    except (ConfigError, RuntimeError, ValueError, OSError) as exc:
        log.error(json.dumps({"event": "startup_refused", "reason": str(exc)}))
        return 2
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")  # noqa: S104
    return 0
