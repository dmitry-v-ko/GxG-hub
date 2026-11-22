"""Regenerates tests/broken/demo/*.patch from connectors/demo (run from the repository root after demo changes):

    uv run python tools/gxg-connector-check/tests/broken/make_demo_patches.py

Each case is a few exact replacements in a copy of demo; the patch is their diff. tests/test_broken.py applies them.
"""

import pathlib
import shutil
import subprocess
import tempfile

S = pathlib.Path(tempfile.mkdtemp())
DEMO = pathlib.Path("connectors/demo")
OUT = pathlib.Path("tools/gxg-connector-check/tests/broken/demo")
IGNORE = shutil.ignore_patterns(".venv", "mutants", "__pycache__", ".pytest_cache", "uv.lock", "tests")
T, SV = "src/demo_connector/tools.py", "src/demo_connector/server.py"
B, M = "src/demo_connector/backend.py", "gxg-manifest.yaml"


def case(name: str, edits: list[tuple[str, str, str]]) -> None:
    base, new = S / "a", S / name
    shutil.copytree(DEMO, base, ignore=IGNORE, dirs_exist_ok=True)
    shutil.copytree(DEMO, new, ignore=IGNORE)
    for rel, old, repl in edits:
        text = (new / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, (name, rel, old[:60])
        (new / rel).write_text(text.replace(old, repl), encoding="utf-8")
    cmd = ["git", "diff", "--no-index", "--src-prefix=a/", "--dst-prefix=b/", "a", name]
    diff = subprocess.run(cmd, cwd=S, capture_output=True, text=True, check=False).stdout
    (OUT / f"{name}.patch").write_text(diff.replace("a/a/", "a/").replace(f"b/{name}/", "b/"), encoding="utf-8")


case(
    "01-no-identity",
    [
        (
            SV,
            '''    tools = connector.tools(mcp.verifier(settings, CONNECTOR_ID), files.refresh)''',
            '''    tools = connector.tools(Lenient(mcp.verifier(settings, CONNECTOR_ID)), files.refresh)''',
        ),
        (
            SV,
            '''class Settings(mcp.Settings):''',
            '''class Lenient:
    """BROKEN: a call without an identity header runs as the default reader."""

    def __init__(self, real: Any) -> None:
        self.real = real

    def verify(self, headers: Any, **kw: Any) -> VerifiedIdentity:
        if "x-gxg-test-identity" not in headers and "x-gxg-identity" not in headers:
            now = datetime.now(UTC)
            return VerifiedIdentity("t-alpha", "agent:t-alpha:reader", now, now, "anon", None, "signed_assertion")
        return self.real.verify(headers, **kw)  # type: ignore[no-any-return]


class Settings(mcp.Settings):''',
        ),
        (SV, "from typing import ClassVar\n", "from datetime import UTC, datetime\nfrom typing import Any, ClassVar\n"),
        (
            SV,
            "from gxg_connector_kit.mcp import ConfigError, NoVerifier, serve\n",
            "from gxg_connector_kit.identity import VerifiedIdentity\nfrom gxg_connector_kit.mcp import ConfigError, NoVerifier, serve\n",
        ),
    ],
)

case(
    "02-tenant-from-header",
    [
        (
            SV,
            '''    tools = connector.tools(mcp.verifier(settings, CONNECTOR_ID), files.refresh)''',
            '''    tools = connector.tools(TenantHeader(mcp.verifier(settings, CONNECTOR_ID)), files.refresh)''',
        ),
        (
            SV,
            '''class Settings(mcp.Settings):''',
            '''class TenantHeader:
    """BROKEN: `X-Tenant-Id` from the caller overrides the verified tenant."""

    def __init__(self, real: Any) -> None:
        self.real = real

    def verify(self, headers: Any, **kw: Any) -> Any:
        ident = self.real.verify(headers, **kw)
        tenant = headers.get("x-tenant-id")
        if tenant:
            name = ident.principal.rsplit(":", 1)[1]
            ident = dataclasses.replace(ident, tenant_id=tenant, principal=f"agent:{tenant}:{name}")
        return ident


class Settings(mcp.Settings):''',
        ),
        (SV, "from typing import ClassVar\n", "import dataclasses\nfrom typing import Any, ClassVar\n"),
    ],
)

case(
    "03-resource-before-identity",
    [
        (
            T,
            '''        return mcp.tools(self, MANIFEST, self.grants, verifier, refresh, logs, ROOT)''',
            '''        backend = self.backend

        class Prefetch:
            """BROKEN: looks the named note up (every tenant) before the identity is verified."""

            def verify(self, headers: Any, *, tool: str | None = None, arguments: Any = None) -> Any:
                if arguments and "note_id" in arguments:
                    for tenant in backend.boards:
                        backend.get_note(tenant, arguments["note_id"])
                return verifier.verify(headers, tool=tool, arguments=arguments)

        return mcp.tools(self, MANIFEST, self.grants, Prefetch(), refresh, logs, ROOT)''',
        )
    ],
)

case(
    "04-provider-before-grants",
    [
        (
            T,
            '''        if access.nothing or access.connection is None:''',
            '''        if access.connection is None:  # BROKEN: lists the boards before knowing anything is granted''',
        )
    ],
)

case(
    "05-hidden-is-not-not-found",
    [
        (
            T,
            '''        note = self.backend.get_note(access.ident.tenant_id, args["note_id"])
''',
            '''        note = self.backend.get_note(access.ident.tenant_id, args["note_id"])
        if note is not None and access.connection and not access.check(rid(access.connection, note.board_id)).allowed:
            raise GxgError("permission_denied", "this note is on a board you may not read")  # BROKEN: GR-17
''',
        )
    ],
)

case(
    "06-error-outside-envelope",
    [
        (
            T,
            '''        return mcp.tools(self, MANIFEST, self.grants, verifier, refresh, logs, ROOT)''',
            '''        return [PlainErrors(**dict(t)) for t in mcp.tools(self, MANIFEST, self.grants, verifier, refresh, logs, ROOT)]''',
        ),
        (
            T,
            '''class Connector:''',
            '''class PlainErrors(mcp.GxgTool):
    """BROKEN: errors as plain text without the structured envelope."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        result = await super().run(arguments)
        if isinstance(result.structured_content, dict) and "code" in result.structured_content:
            text = TextContent(type="text", text=f"Error: {result.structured_content['message']}")
            return ToolResult(content=[text], is_error=True)
        return result


class Connector:''',
        ),
        (
            T,
            "from gxg_connector_kit import mcp\n",
            "from fastmcp.tools.base import ToolResult\nfrom mcp.types import TextContent\n\nfrom gxg_connector_kit import mcp\n",
        ),
    ],
)

case(
    "07-unbounded-output",
    [
        (
            M,
            '''    limits: { max_results: 50, timeout_s: 20 }''',
            '''    limits: { timeout_s: 20 }  # BROKEN: no max_results''',
        ),
        ("schemas/notes-search.input.json", '''"maximum": 50,''', '''"maximum": 100000,'''),
    ],
)

case(
    "08-rate-limit-not-retryable",
    [
        (
            B,
            '''            raise GxgError("rate_limited", "backend rate limit", retry_after_s=RETRY_AFTER_S)''',
            '''            raise GxgError("rate_limited", "backend rate limit", retry_after_s=RETRY_AFTER_S, retryable=False)  # BROKEN''',
        )
    ],
)

case(
    "09-secret-in-audit",
    [
        (
            T,
            '''        return content, {}
''',
            '''        return content, {"provider_auth": os.environ.get("DEMO_PROVIDER_TOKEN")}  # BROKEN: a credential in the audit
''',
        ),
        (T, "import json\n", "import json\nimport os\n"),
    ],
)

case(
    "10-always-ready",
    [
        (
            SV,
            '''    return mcp.create_app(CONNECTOR_ID, tools, files.refresh, store.readiness, logs.startup), connector''',
            '''    always = lambda: Readiness(True)  # noqa: E731 - BROKEN: ready without a valid bundle
    return mcp.create_app(CONNECTOR_ID, tools, files.refresh, always, logs.startup), connector''',
        ),
        (
            SV,
            "from gxg_connector_kit import mcp\n",
            "from gxg_connector_kit import mcp\nfrom gxg_connector_kit.readiness import Readiness\n",
        ),
    ],
)
