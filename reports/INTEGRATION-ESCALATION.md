# INTEGRATION ESCALATION: M1.1 — demo M8 vs `gxg_connector_kit.effects` (AC-M1.1-04)

Date: 2026-11-23. Branch `claude/integration`, merge `22346df` (M1.1 `bffcbd4` into `40cec84`).

## The question

AC-M1.1-04 says two things: "the M8 tests are green" and "demo is on the template". After the merge these two cannot both hold unless one set of tests changes. The owner has to decide which set changes.

## Facts (checked in this run)

1. **demo keeps its own `effects.py`** (the brief's fallback, what is on the branch now):
   - `connectors/demo/tests/test_effects.py` (M8): 81/81 green **without any change**; `connectors/demo` 146/146.
   - `check-connectors --connector connectors/demo`: 19/19 passed, **EX-01…EX-16 included** (the kit's effect checks pass on demo's own module). The harness needed a demo part: `conformance.py` counts the fake provider's sends and sets its `commit_then_drop` mode, and passes `actor`/`propose`.
   - **Red:** `tests/toolkit/test_template.py::test_demo_is_on_the_template`. `src/demo_connector/server.py` is template-owned (`DEMO_OWNED`), and the template render (`with_effects: false`) has no M8 wiring: `Settings.provider_db`, `Wiring(clock)`, `build(settings, backend, wiring)`, port 8081 from `connector.effects`. M8 tests call all of these. (`Dockerfile` drift was only the header comment; it is the render again now.)
   - **Red as a result:** the broken-copy patches (`tools/gxg-connector-check/tests/broken/demo/*.patch`, `operator/*.patch`, made by `make_patches.py` as exact replacements in demo's `server.py` and `tools.py`) no longer apply: 8 of 19 cases, `test_operator_only_tool`, `test_cli_on_demo_and_usage`.
2. **demo moves onto `gxg_connector_kit.effects`** (`with_effects: true`, the template's `enable_effects`): `server.py` becomes the render. The M8 tests then have to be rewritten against the kit's API: `connector.effects` → `connector.internal`, `Effects._checked` (monkeypatched), `Provider.send(tenant=…, provider_key=…)` → `send(claims, args)`, `Settings.svc_key_file` Path → str, `approval_url` None → "", no `provider_db` and no `Wiring.clock` (`test_jwks_cache_follows_the_injected_clock`), and the text of the kit hook's refusal ("no propose right on this resource" instead of "…on this board"). It also adds behaviour M8 did not have: `caller.serves(tenant)` for the executor. That is more than renaming: it is the kind of change the brief says not to make ("without changing their meaning").

## Recommendation

Option 1 for this integration, with two small changes to M1.1's tests, both under critic review:
- `DEMO_OWNED`: `server.py` stays template-owned only while demo is read-only, or the drift test compares demo against the template's `with_effects: true` render with an allowed diff for stage C.
- regenerate the broken patches against the merged demo (`make_patches.py`, its replacement strings in `server.py`).
The move onto the kit (option 2) stays a backlog row (already added in `docs/backlog.md`, M1.1) as its own task with a critic.

## What does not depend on the answer (done or running in this pass)

Root `pytest` except the files above, demo and M8 tests, `check-connectors` on demo, Docker smoke, stand `stand/run.sh` (fresh / after-restart / after-recreate), mutmut on `packages/gxg-connector-kit` and `tools/gxg-connector-check`. mutmut on `connectors/demo` runs on the current code (option 1); under option 2 it has to be run again.
