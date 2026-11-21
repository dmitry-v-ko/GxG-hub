# INTEGRATION READY: M3, M4.1, M5, M7 + specs

- Branch: `claude/integration`; code under test: `fc87e27` (report: `6a6d4e9`, this file is the next commit).
- Merged: `claude/m5-readonly-poc@fb14af1` (M3, M4.1, M5 + `reports/critic/M5-final.*`), `claude/m7-approval-core@e25cad9` (M7 + `reports/critic/M7-final.*`), `docs/specs`, `docs/adr`, `reports/GOAL.md`, `reports/progress.md` from `claude/admiring-planck-vzys0x@717dabd`.
- `git show HEAD:docs/specs/security-v1.md | grep -c X-GxG-Identity` = 2; `git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr` is empty.
- Minimal code change: approval on fastmcp 4.1.8 (range `<5`, 3 lines in `mcp.py`, 1 assertion in `tests_db/test_mcp.py`); reason and details in `reports/integration-2026-11-21-m3-m5-m7.md`.

## Checks (local, Docker 29.10.0 started by hand)

| Check | Result |
|---|---|
| `uv lock --check` (root, demo), `uv sync --locked` | OK |
| ruff check / format | OK |
| mypy (all 5 calls from `check-all.sh`) | OK |
| `uv run pytest` (all packages + `tests/contract`) | 868 passed |
| `uv run pytest approval/tests_db` (PostgreSQL 16.6) | 307 passed |
| kit + coverage 100% branches; demo 64; schema guard 66/66; tests/secrets 21; policy validate/lint/bundle | OK |
| gitleaks tree HEAD and full range | no leaks found |
| `connectors/demo/docker_smoke.sh` | OK |
| `stand/run.sh` (M5) | fresh 29/0, after-restart 29/0, after-recreate 29/0 |
| `scripts/ci/mutmut.sh` | running (~2 h); result will be added to this file in a separate commit |

GitHub CI was not checked (runners may be unavailable); everything is local. `grants-widening` is red on a push without a PR by M2 design.
