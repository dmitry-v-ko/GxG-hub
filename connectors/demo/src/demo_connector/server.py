"""MCP server of `demo`: the kit's FastMCP plumbing (gxg_connector_kit.mcp) over Streamable HTTP, /healthz, /readyz.

Made from templates/python-fastmcp (scripts/new-connector); `copier update` keeps this file in step with the template.
Configuration: `gxg_connector_kit.mcp.Settings` with the `DEMO_` prefix (no secrets, only public keys and paths);
DEMO_TEST_CONTROLS=1 (test profile only) serves testctl.py on 127.0.0.1:8090.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import ClassVar

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from starlette.applications import Starlette

from gxg_connector_kit import mcp
from gxg_connector_kit.mcp import ConfigError, NoVerifier, serve

from .backend import Backend
from .tools import Connector, logs

__all__ = ["ConfigError", "NoVerifier", "Settings", "build", "configs", "main", "parse_keys", "serve"]
CONNECTOR_ID = "demo"  # `aud` of identity assertions; id in the platform registry


class Settings(mcp.Settings):
    PREFIX: ClassVar[str] = "DEMO"


def parse_keys(value: str, name: str = "DEMO_BUNDLE_KEYS") -> dict[str, Ed25519PublicKey]:
    return mcp.parse_keys(value, name)


def build(settings: mcp.Settings, backend: Backend | None = None) -> tuple[Starlette, Connector]:
    store, files = mcp.bundle_store(settings, logs.startup)
    connector = Connector(backend or Backend(), store)
    tools = connector.tools(mcp.verifier(settings, CONNECTOR_ID), files.refresh)
    # Read-only connector: backend + valid bundle; no Approval Service (MCC-09).
    return mcp.create_app(CONNECTOR_ID, tools, files.refresh, store.readiness, logs.startup), connector


def configs(settings: mcp.Settings, app: Starlette, connector: Connector) -> list[uvicorn.Config]:
    out = [uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")]  # noqa: S104
    if settings.test_controls:
        from .testctl import create_app as test_app  # noqa: PLC0415 - absent in the production image

        out.append(uvicorn.Config(test_app(connector.backend), host="127.0.0.1", port=8090))
    return out


def main(env: Mapping[str, str] = os.environ) -> int:
    def start() -> list[uvicorn.Config]:
        settings = Settings.from_env(env)
        return configs(settings, *build(settings))

    return mcp.run_main(start, lambda c: serve(c), logs)  # noqa: PLW0108 - looked up at call time: tests replace it
