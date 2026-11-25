"""The approval image is built with `uv sync --no-dev --package gxg-approval`: every third-party module the process
code imports must be a declared dependency (dev-only packages hide the gap in the root environment)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = {"jwt": "pyjwt", "nacl": "pynacl", "psycopg_pool": "psycopg-pool", "prometheus_client": "prometheus-client"}
TRANSITIVE = {
    "mcp",
    "starlette",
    "pydantic",
    "sqlalchemy",
    "cryptography",
}  # pulled in by fastapi, fastmcp, alembic, the kit


def test_approval_imports_are_declared_dependencies() -> None:
    declared = {
        re.split(r"[\[<>=! ;]", d, maxsplit=1)[0].lower().replace("_", "-")
        for d in tomllib.loads((ROOT / "approval/pyproject.toml").read_text())["project"]["dependencies"]
    }
    missing: set[str] = set()
    for path in (ROOT / "approval/src/gxg_approval").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in sys.stdlib_module_names or top in {"gxg_approval", "gxg_connector_kit"} | TRANSITIVE:
                    continue
                if DIST.get(top, top).replace("_", "-") not in declared:
                    missing.add(top)
    assert not missing, f"imported but not declared in approval/pyproject.toml: {sorted(missing)}"
