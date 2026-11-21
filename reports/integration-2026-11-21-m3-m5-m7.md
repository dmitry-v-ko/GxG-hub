# Integration 2026-11-21: M3, M4.1, M5, M7 and the specs into `claude/integration`

Starting point: `claude/integration@6a75c62` (M0, M0.1, M1, M2, M4). No force push, no history rewrite: only merge commits and new commits on top.

## What was merged

| # | Source | Commit | What it brings |
|---|---|---|---|
| 1 | `origin/claude/m5-readonly-poc` | `fb14af1` (newer than `7c5a53e`) | M3 (demo connector, 9.5), M4.1 (ADR-0002 Accepted), M5 (read-only PoC on ContextForge, stand, 9.5); final critic report `reports/critic/M5-final.{json,review.md}` (in `fb14af1`) |
| 2 | `origin/claude/m7-approval-core` | `e25cad9` (newer than `0fce3f3`) | M7 (Approval Service core, 9.0); final critic report `reports/critic/M7-final.{json,review.md}` |
| 3 | `origin/claude/admiring-planck-vzys0x` | `717dabd` | `docs/specs`, `docs/adr`, `reports/GOAL.md`, `reports/progress.md` only (checkout of those paths, not a merge of the branch) |

Integration commits:

- `8a2b652` Merge M5 (no conflicts).
- `34c04ee` Merge M7 (conflicts below; includes the `fastmcp` range in `approval/pyproject.toml`).
- `51cda58` docs from the plan branch + backlog union.
- `fc87e27` minimal port of `approval/src/gxg_approval/mcp.py` and one assertion in `approval/tests_db/test_mcp.py` to fastmcp 4.1.8.
- this report, the stand run record, `INTEGRATION-READY.md`.

## Conflicts and how they were resolved

### Merge 1 (M5)
No conflicts. In the merge, `connectors/demo/tests/test_demo_import.py` was deleted: that is M3's own change (`4690309`), not the integrator's.

### Merge 2 (M7)

| File | Resolution |
|---|---|
| `.github/workflows/ci.yml` | `mutmut` job: comment covers M1, M2, M3, M5 and M7; `timeout-minutes: 240` (M7: approval ~1700 mutants + SQL check; the M5 value 45 is smaller than the new load). `ci-ok.needs` = the union: `lint, types, tests, approval-db, schema-guard, secrets, mutmut, policy, demo-image, m5-stand, grants-widening`. The M7 `approval-db` job and the M7 mypy step came in through the automatic merge. |
| `pyproject.toml` | Union of both sides' `per-file-ignores` (`stand/*.py` from M5; `approval/.../cli.py`, `approval/.../migrations/**` from M7). `tests_db` ignore, `httpx`/`docker` in dev, `mypy_path` with the kit tests, `do_not_mutate` with `mutate_m7_sql.py`, the `cpex.*` override came in automatically. Workspace members unchanged: `packages/gxg-connector-kit`, `approval`, `tools/gxg-policy`; `connectors/demo` by path (its own lockfile). |
| `scripts/ci/mutmut.sh` | Union: the M7 PostgreSQL container (Unix socket, `--network none`) and the SQL check `mutate_m7_sql.py` + the M5 loop over `connectors/demo`, `gateway/contextforge`, `packages/gxg-connector-kit`. Final loop: `tools/gxg-policy connectors/demo gateway/contextforge packages/gxg-connector-kit approval .`. The comment merges both descriptions. |
| `scripts/ci/mutmut-equivalent.txt` | Union: all M3/M5/M1 entries + all M7 entries (under the heading `# M7 (approval)`). Nothing removed. |
| `docs/backlog.md` | Union of rows without duplicates: M5 cycle rows (F11–F13, F6) and M7 cycle rows. |
| `uv.lock` | Regenerated with `uv lock` after the `fastmcp` fix (below). |

### Docs (step 3)
M7 brought back the pre-M4 "VERIFY" text in `docs/specs/security-v1.md`, `target-platform-v1.md`, `resource-grants-v1.md` (the automatic merge took it without a conflict). All of `docs/specs` and `docs/adr` were taken from `717dabd`. `docs/setup/*` (M2 documents, missing on the plan branch) stay. In `docs/backlog.md` the rows of M5 and M7 cycles were kept and the two final-critic rows of the plan branch were added (`M5 | final | …M5-final.json`, `M7 | …M7-final.json`).

Checks:

```
$ git show HEAD:docs/specs/security-v1.md | grep -c X-GxG-Identity
2
$ git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr | wc -l
0
```

## Minimal code changes (beyond conflict resolution)

1. **`approval/pyproject.toml`: `fastmcp>=2.12,<3` → `fastmcp>=2.12,<5`.** Without it `uv lock` has no solution: the root project depends on `gxg-connector-demo`, which pins `fastmcp==4.1.8` (M3), and on `gxg-approval` with `fastmcp<3` (M7). One root workspace has one version of `fastmcp`. Lowering the demo is not possible (M3/M5 conformance is on 4.1.8, the same version as the demo image).
2. **`approval/src/gxg_approval/mcp.py`: port to fastmcp 4 / MCP SDK 2.3 (3 lines, behaviour unchanged):**
   - `from fastmcp.tools.tool import ToolResult` → `from fastmcp.tools.base import ToolResult` (module moved);
   - `CallToolResult(..., structuredContent=..., isError=True)` → `structured_content=..., is_error=True` (MCP SDK 2 field names; on the wire it is still `structuredContent`/`isError`);
   - `# type: ignore[call-arg]` on the line `ErrorResult(...) if error else ToolResult(...)` in `_result`: in fastmcp 4 `ToolResult` is a pydantic `BaseModel`, and mypy uses the `dataclass_transform` `__init__` for the subclass. The runtime constructor is the same.
   - `create_server` was not touched, so the mutant names of the M7 equivalent list (`x_create_server__mutmut_*`) still point at the same code.
3. **`approval/tests_db/test_mcp.py::test_tools_are_listed`:** `c.initialize_result.serverInfo.name` → `c.server_info.name`. In fastmcp 4 `Client.initialize_result` is `None` outside the legacy handshake; `server_info` is the replacement the library itself recommends. The check stays the same (server name `gxg-approval`).

Before the port: mypy 4 errors in `mcp.py`; `approval/tests_db` 306/307 (this test). After: mypy clean, 307/307.

## Check results (locally, HEAD `fc87e27`, the code of this report's commit)

Docker: `dockerd` started by hand, Docker 29.10.0. Image builds in the sandbox go through a TLS proxy, so `DOCKER_BUILD_FLAGS="--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt"` (the way described in `stand/README.md`); the CA goes only into a build secret, not into a layer.

| Check | Command | Result |
|---|---|---|
| lock | `uv lock --check`; `(cd connectors/demo && uv lock --check)`; `uv sync --locked` | OK (116 / 81 packages) |
| ruff | `uv run ruff check .`; `uv run ruff format --check .` | All checks passed; 118 files formatted |
| mypy | the 5 mypy calls from `scripts/ci/check-all.sh` (kit+approval+scripts strict; conftest+tests/ci; approval/tests_db; policy+demo+contract; stand+gateway) | Success in all 5 (54, 8, 8, 23, 11 files) |
| tests of all packages + `tests/contract` | `uv run pytest` | **868 passed** |
| approval on real PostgreSQL 16.6 | `docker pull postgres:16.6-alpine && uv run pytest approval/tests_db` | **307 passed** |
| schema lock | `uv run python scripts/ci/schema_lock.py` | up to date |
| kit + coverage | `uv run pytest packages/gxg-connector-kit/tests --cov…`; `check_coverage.py` | 366 passed; branches 100% in every module |
| demo (own lockfile) | `(cd connectors/demo && uv sync --locked && uv run pytest -q)` | 64 passed |
| schema guard | `uv run python scripts/ci/prove_schema_guard.py` | 66/66 mutations make the contract tests fail |
| gitleaks | `scripts/ci/gitleaks.sh tree HEAD`; `… range 0000…..HEAD` | no leaks found |
| secrets | `uv run pytest tests/secrets` | 21 passed |
| policy | `gxg-policy validate policies/`; `lint`; `bash scripts/ci/bundle_smoke.sh` | 0 errors (lint: 2 warnings, as before); bundle OK |
| demo image | `connectors/demo/docker_smoke.sh` (with `DOCKER_BUILD_FLAGS`) | docker_smoke: OK |
| **M5 stand** | `stand/run.sh` (with `DOCKER_BUILD_FLAGS`) | **fresh 29/0, after-restart 29/0, after-recreate 29/0**; run record: `reports/integration/2026-11-21-m3-m5-m7-stand.json` |
| mutmut | `scripts/ci/mutmut.sh` | see `INTEGRATION-READY.md` (the run takes ~2 h, started after the other checks) |

Notes on the runs, without hiding anything:

- The first run of `check-all.sh` went through my own line-by-line wrapper, which split the multi-line mypy command (rc=126 is the wrapper's artifact). The command was then run as a whole: green.
- The first `approval/tests_db` run: 303 errors `No such image: postgres:16.6-alpine` (the image had not been pulled; in CI the `approval-db` job runs `docker pull` as a separate step). After `docker pull`: see the table.
- The first stand run: `429 Too Many Requests` from Docker Hub when pulling `postgres:18`; the second: fresh 26/3 (in MC-02, MC-04, SC-04 the probe's `docker run python:3.12-slim` exited with 125 while the image was not there yet), after-restart and after-recreate 29/0. The third run, with the images already present: 29/0 in all three phases (table). Nothing in the code was changed between the second and third runs.
- `reports/conformance/M5.json` (the milestone record of M5) was not overwritten: `stand/run.sh` writes there, so after the run the file was restored from git and the integration run was saved separately.
- Warnings: `FastMCPDeprecationWarning` about `structuredContent` in `stand/tests/test_mc.py` (M5 code, not an error); `pytest_socket` warning in `test_api_read.py` (was in M7 too).

## GitHub CI
Not checked: GitHub runners for this repository may not be available (as with M5/M7 heads). All results above are local. The `grants-widening` job is red on a push without a PR by M2 design (any widening outside a PR is red).

## Not done / for the owner
- `approval` now runs on fastmcp 4.1.8 / MCP SDK 2.3, not on 2.14.7, which the M7 critic saw. The M7 tests on PostgreSQL (incl. `test_mcp.py`, AC-M7-09) are green; it is worth having the M8 owner look at `mcp.py` again.
- The M5 stand's `DeprecationWarning` (`structuredContent` in `stand/tests/test_mc.py`) is an M5 backlog candidate, not changed (M5 code).
