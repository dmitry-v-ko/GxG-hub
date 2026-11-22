# Integration 2026-11-22: M8, M9, M11 and the specs into `claude/integration`

Starting point: `claude/integration@15213ee` (M0–M5, M7, report of 2026-11-21). No force push, no history rewrite: only merge commits and new commits on top. Full clone (`git rev-parse --is-shallow-repository` = `false`).

## What was merged

| # | Source | Commit | What it brings |
|---|---|---|---|
| 1 | `origin/claude/m8-executor` | `69ff3e5` | M8 (executor, JWKS and key rotation, EFF-08 per requester, demo effects, EX-01), `reports/critic/M8-cycle-{1,2,3}.*`, `reports/mutmut/M8*.txt` |
| 2 | `origin/claude/m9-ui-bot` | `3254fe0` | M9 (Web UI `approval/ui`, Telegram bot `approval/notifier`, own lockfiles, AS-29 networks), `reports/critic/M9-*` |
| 3 | `origin/claude/m11-telegram-read` | `1dc20fb` | M11 (read-only Telegram connector `connectors/telegram`, archive schema from `gxg_agents_v2`, stand gateways), `reports/critic/M11-*` |
| 4 | `origin/claude/admiring-planck-vzys0x` | `44efcc8` | `docs/specs`, `docs/adr`, `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` only (checkout of those paths, not a merge of the branch) |

Integration commits:

- `403bbd4` Merge M8 (no conflicts).
- `64b2ed2` Merge M9 (conflicts and two semantic fixes, below).
- `e3c96f4` Merge M11 (conflicts, backlog rows restored, below).
- `aba22a5` docs from the plan branch.
- this report and `INTEGRATION-READY.md`.

## Conflicts and how they were resolved

### Merge 1 (M8)
No conflicts.

### Merge 2 (M9)

| File | Resolution |
|---|---|
| `approval/README.md` | One intro line naming M7, M8 and M9; the process table keeps both the M8 rows (`executor`, keys, AS-10) and the M9 row (migration path changed to `0004_ui_notifier.py`, see below). |
| `approval/deploy/compose.yaml` | Union: M8 `executor` (networks `internal`, `connectors`; exec-keys directory mount) + M9 `ui`, `ingress`, `notifier`, relays and test profile. The `connectors` network (internal) joins the M9 network list. Executor secrets written in the M9 form `${GXG_SECRETS_DIR:-./secrets}/…`. |
| `approval/tests/test_deploy.py` | AS-28 table: union (`executor` + M9 processes). M9's `NETWORKS` table gets `"executor": {"internal", "connectors"}`; M9's stricter network test (exact network set per service, only `edge`/`egress` not internal) replaces M8's loop. It still proves M8's point: only the executor is on `connectors`. |
| `approval/tests_db/test_propose.py` | Both constant lines (`BOT, SIGNALS` from M8, `HUMAN1, HUMAN2` from M9). |
| `approval/tests_db/test_roles.py` | `FUNCTIONS`: `gxg_ui` + `gxg_link_code`, `gxg_notifier` + `gxg_link_claim` (M9), `gxg_executor` + `gxg_release_reserved` (M8). |
| `pyproject.toml` | `mypy_path`: union (`approval/ui/src`, `approval/notifier/src` + M8's `approval/tests_db`); M9's `authlib.*` override. |
| `scripts/ci/mutmut.sh` | M8's `MUTMUT_PACKAGES` default + M9's `approval/ui`, `approval/notifier` and the root-environment `uv run --project` for them. |
| `scripts/ci/mutate_m7_sql.py` | M8's `MIG2` (0002_executor), `MIG3`, `EXE`, `M8_TESTS`, `M8_MUTATIONS`; M9's DB test list as `TESTS` and its link-code rows with a new name `MIG4` (0004_ui_notifier). |
| `scripts/ci/mutmut-equivalent.txt` | Union of M8 and M9 entries. |
| `docs/backlog.md` | Union of rows (see the M11 merge about the rows M9 had dropped). |

Two changes beyond text conflicts (both from the two branches being correct alone and wrong together):

1. **Alembic revisions.** M8 added `0002_executor.py` (revision `0002`) and `0003_idempotency_per_requester.py` (`0003`, down `0002`); M9 added `0002_ui_notifier.py` with revision `0002` too. Together: two migrations with one revision id — `alembic upgrade head` is ambiguous. The two migrations touch different objects (M8: `gxg_release_reserved`, an outbox index, an intents constraint; M9: `link_codes`, `webauthn_credentials`, `gxg_link_code`, `gxg_link_claim`, `gxg_ui` revokes), so the order does not matter. Fix: M9's file renamed to `0004_ui_notifier.py`, `revision = "0004"`, `down_revision = "0003"` (docstring updated). The SQL itself is unchanged; `mutate_m7_sql.py` and `approval/README.md` point at the new path.
2. **`approval/src/gxg_approval/config.py`**: M9 moved the bundle loader into the kit and dropped `import json` and `import threading`; M8 added `JsonFile`, which uses both. The merged file did not import them (ruff F821). Fix: the two imports restored.

### Merge 3 (M11)

M11 branched from `fb14af1` (before M7, M8 and M9).

| File | Resolution |
|---|---|
| `stand/bootstrap.py`, `stand/tests/test_sc.py`, `stand/README.md` | M8 deleted the REST decoy `demo.message.execute` (EFF-02: no `*.execute` in ContextForge; EX-01 registers a probe only for its test). M11 added the `telegram-<tenant>` gateways and `vs-tg-<tenant>` servers and still expected the decoy in SC-01. Kept both: no execute tool; SC-01 expects `own | telegram` on `/rpc` and SSE, `own` on the demo virtual server. README: M8's EX-01 text + M11's telegram gateway, grants and `test_telegram.py`. |
| `pyproject.toml` | Union: dependencies and sources (`gxg-approval-ui`, `gxg-approval-notifier`, `gxg-connector-telegram`), `known-first-party`, `mypy_path` (+ `connectors/telegram/src`, `connectors/telegram/archive`). |
| `scripts/ci/check-all.sh` | Union: lock checks of `approval/ui`, `approval/notifier`, `connectors/telegram`; all M9 mypy calls; M11's demo/telegram mypy line and stand `MYPYPATH`. The M11 telegram pytest and the `gxg_agents_v2` upstream check came in through the automatic merge. |
| `scripts/ci/mutmut.sh` | `connectors/telegram` added to the default `MUTMUT_PACKAGES` list. |
| `scripts/ci/mutmut-equivalent.txt` | Union (M11 entries under the M7/M8/M9 ones). |
| `uv.lock` | Ours + `uv lock`: the only change is the added `gxg-connector-telegram` package. |
| `docs/backlog.md` | See below. |

**Backlog rows lost in M9.** M9 took `docs/` from the plan branch wholesale, which dropped ten rows of the earlier integration (M5 cycle 1–2: F6, F11, F12, F13; M7 cycles 1–2: six rows). The M9 merge applied that deletion without a conflict. In the M11 merge the rows were restored at their earlier place; the `M5 | final` row is in M11's wording (it records that items (2) and (3) are done in M11), and M11's ten new rows were added. Check: every `|` row of `15213ee`, `m8-executor`, `m9-ui-bot` and `m11-telegram-read` is in the result (the old `M5 | final` row only in its updated M11 form). The plan branch has one more M11 row (`M11-cycle-4.json`, minor about `search_path`) that duplicates M11's own F1–F3 rows of cycle 4; it was not added.

### Docs (step 4)
`docs/specs`, `docs/adr` (incl. the new ADR-0005, ADR-0006), `docs/implementation-plan-v1.md`, `reports/GOAL.md`, `reports/progress.md` taken from `44efcc8`.

```
$ git diff origin/claude/admiring-planck-vzys0x -- docs/specs docs/adr docs/implementation-plan-v1.md | wc -l
0
```

### What was checked so that nothing of M3/M4.1/M5/M7 (or of the new milestones) got lost

- `reports/critic/*`, `reports/conformance/*`, `reports/mutmut/*`, `reports/plans/*`, `reports/M*-READY.md`: every file of each merged branch is byte-equal in HEAD; nothing of `15213ee` under `reports/` changed except `GOAL.md` and `progress.md` (taken from the plan branch).
- Tests: every `test_*` function of every `test_*.py` in `15213ee`, `m8-executor`, `m9-ui-bot`, `m11-telegram-read` exists in HEAD. The one missing file, `approval/tests/test_approval_import.py` (only in M11), is the M0 placeholder that M7 deleted (`43d8a82`); M11 branched before M7.
- The previous integration's fastmcp 4 port of `approval/mcp.py` is untouched by M8/M9/M11.

## Environment notes

- `postgresql-16-pgvector` installed from apt (the M11 connector tests start their own PostgreSQL 16 with pgvector; CI installs the same package).
- `gxg_agents_v2` cloned read-only to `../gxg_agents_v2` (depth 1, HEAD = pinned `f1c4d6d`) for `check_upstream.sh`; nothing written there.
- `dockerd` started by hand (Docker 29.10.0); `kernel.pid_max` set to 4194304 before mutmut (`reports/mutmut/M8.txt`). The session's worker was restarted once during the work; dockerd and `pid_max` were set again before the gate.
- `DOCKER_BUILD_FLAGS='--network host --secret id=ca-bundle,src=/root/.ccr/ca-bundle.crt'`.
