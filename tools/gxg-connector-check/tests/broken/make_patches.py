"""Regenerates tests/broken/{demo,sample}/*.patch from connectors/demo and the template (run from the repository root after demo changes):

    uv run python tools/gxg-connector-check/tests/broken/make_patches.py

Each case is a few exact replacements in a copy of demo; the patch is their diff. tests/test_broken.py applies them.
"""

import pathlib
import shutil
import subprocess
import tempfile

import copier

S = pathlib.Path(tempfile.mkdtemp())
DEMO = pathlib.Path("connectors/demo")
C, ST = "src/demo_connector/conformance.py", "schemas/notes-search.input.json"
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
            '''    ready = mcp.readiness_of(store.readiness, connector.backend.readiness)''',
            '''    ready = lambda: Readiness(True)  # noqa: E731 - BROKEN: ready without a valid bundle''',
        ),
        (
            SV,
            "from gxg_connector_kit import mcp\n",
            "from gxg_connector_kit import mcp\nfrom gxg_connector_kit.readiness import Readiness\n",
        ),
    ],
)

# --- cycle 2: positive controls of the harness, lists, logs, readiness ------------------------------------------
case(
    "11-calls-always-zero", [(C, "calls=lambda: sum(backend.calls.values())", "calls=lambda: 0")]
)  # BROKEN: counts nothing
case("12-failures-only-crash", [(C, "failures=(*BACKEND_FAILURES, \"crash\"),", "failures=(\"crash\",),  # BROKEN")])
case(
    "13-hidden-board-dropped",
    [
        (
            T,
            """        if requested is not None and any(b not in readable for b in requested):
            raise GxgError("not_found", NOT_FOUND)  # GR-17: a named board without a grant""",
            """        if requested is not None:  # BROKEN: a named board without a grant is silently dropped (GR-17)
            requested = [b for b in requested if b in readable]""",
        )
    ],
)
case(
    "14-empty-is-not-found",
    [
        (
            T,
            """        page = visible[offset : offset + limit]""",
            """        if not visible:
            raise GxgError("not_found", NOT_FOUND)  # BROKEN: an empty result is a success (MCC-04)
        page = visible[offset : offset + limit]""",
        )
    ],
)
case(
    "15-cursor-never",
    [
        (
            T,
            """            "next_cursor": self._cursor.make(binding, offset + limit) if more else None,""",
            """            "next_cursor": None,  # BROKEN: a cut page has no cursor""",
        )
    ],
)
case(
    "16-cursor-unbound",
    [
        (
            T,
            """        binding = json.dumps([ident.tenant_id, ident.principal, query, boards, limit]).encode()""",
            """        binding = json.dumps([query, limit]).encode()  # BROKEN: the cursor is not bound to its caller""",
        )
    ],
)
case(
    "17-query-in-audit",
    [
        (
            T,
            """        return content, {"grant_id": sorted(grant_ids), "method": method}""",
            """        return content, {"grant_id": sorted(grant_ids), "method": method, "q": query}  # BROKEN: text in the log""",
        )
    ],
)
case(
    "18-backend-always-ready",
    [
        (
            B,
            """        if self.failure in ("temporarily_unavailable", "timeout_before_start"):
            return Readiness(False, ("backend is not available",))""",
            """        if False:  # BROKEN: readiness ignores the backend (MCC-09)
            return Readiness(False, ("backend is not available",))""",
        )
    ],
)

# --- sample --with-effects (the template rendered here): EX checks -------------------------------------------
SAMPLE = S / "sample-base"
OUT_SAMPLE = pathlib.Path("tools/gxg-connector-check/tests/broken/sample")
copier.run_copy(".", SAMPLE, data={"name": "sample", "with_effects": True, "kit_source": "path", "repo": "../.."},
                defaults=True, quiet=True)  # fmt: skip
ST_TOOLS, ST_BACKEND = "src/sample_connector/tools.py", "src/sample_connector/backend.py"


def sample(name: str, edits: list[tuple[str, str, str]]) -> None:
    new = S / name
    shutil.copytree(SAMPLE, new)
    for rel, old, repl in edits:
        text = (new / rel).read_text(encoding="utf-8")
        assert text.count(old) == 1, (name, rel, old[:60])
        (new / rel).write_text(text.replace(old, repl), encoding="utf-8")
    cmd = ["git", "diff", "--no-index", "--src-prefix=a/", "--dst-prefix=b/", "sample-base", name]
    diff = subprocess.run(cmd, cwd=S, capture_output=True, text=True, check=False).stdout
    diff = diff.replace("a/sample-base/", "a/").replace(f"b/{name}/", "b/")
    (OUT_SAMPLE / f"{name}.patch").write_text(diff, encoding="utf-8")


INTERNAL_END = """            logs=logs,
        ).app()
"""
sample(
    "ex03-any-caller",
    [
        (
            ST_TOOLS,
            INTERNAL_END,
            """            logs=logs,
        )
        internal.callers = AnyCaller()  # type: ignore[assignment]  # BROKEN: no service JWT check (AS-06)
        self.internal = internal.app()
""",
        ),
        (ST_TOOLS, "        self.internal = effects.Internal(", "        internal = effects.Internal("),
        (
            ST_TOOLS,
            "class Connector:",
            """class AnyCaller:
    def verify(self, token: str, **_: Any) -> Any:
        return type("Caller", (), {"serves": lambda self, tenant: True})()


class Connector:""",
        ),
    ],
)
sample(
    "ex03-unverified-token",
    [
        (
            ST_TOOLS,
            INTERNAL_END,
            """            logs=logs,
        )
        internal.tokens.verify = lambda token: jwt.decode(token, options={"verify_signature": False})  # type: ignore[method-assign]  # BROKEN
        self.internal = internal.app()
""",
        ),
        (ST_TOOLS, "        self.internal = effects.Internal(", "        internal = effects.Internal("),
        (ST_TOOLS, "import json\n", "import json\n\nimport jwt\n"),
    ],
)
sample(
    "ex03-serves-everything",
    [
        (
            ST_TOOLS,
            """        own = self.backend.connections.get(tenant)""",
            """        return True  # BROKEN: any tenant, connection and resource (EFF-18 п.3)
        own = self.backend.connections.get(tenant)""",
        )
    ],
)
sample(
    "ex04-no-jti-cache",
    [
        (
            ST_TOOLS,
            INTERNAL_END,
            """            logs=logs,
        )
        internal.tokens._jti.add = lambda *a, **k: True  # type: ignore[method-assign]  # BROKEN: no replay cache
        self.internal = internal.app()
""",
        ),
        (ST_TOOLS, "        self.internal = effects.Internal(", "        internal = effects.Internal("),
    ],
)
sample(
    "ex11-retry-lost-answer",
    [
        (
            ST_BACKEND,
            """        if self.failure == "commit_then_drop":
            raise Dropped
""",
            """        if self.failure == "commit_then_drop":
            self.failure = None
            return await self.send(claims, args)  # BROKEN: retries a lost answer (EFF-24: never automatically)
""",
        )
    ],
)
