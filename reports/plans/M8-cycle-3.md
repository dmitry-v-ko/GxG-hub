# M8 cycle 3 — plan (ponytail full)

Input: `reports/critic/M8-cycle-2.json` (score 6.0 on d487b4a, caps: critical, major). Its `required_improvements`
1–5 are the closed checklist. Spec change relayed by the orchestrator (owner's order, docs from
`origin/claude/admiring-planck-vzys0x@59993e4`): the provider idempotency key = `intent_id` (EFF-18 p.5, EFF-23,
EFF-25, demo-connector «Fake provider», AC-M8-01/02/08). It replaces item 1's «key per (tenant, requester, key)»:
`intent_id` is unique per intent, which is unique per (tenant, requester, key) since 0003, and it is constant across
attempts.

## Checklist

| # | Item | Change | Rung |
| --- | --- | --- | --- |
| 1 | provider key collides across requesters (critical) | `execute` passes `claims["iid"]` to `provider.send(provider_key=…)`; the provider journal column and dedupe use `provider_key`; `reconcile` looks up by the request's `intent_id`; test control `GET /_test/provider/calls?provider_key=`; AC tests read the journal by `intent_id`; real-stack test: two requesters of t-alpha, same `idempotency_key`, different text → two intents, two journal rows, two operation ids, both `succeeded` | rename + one argument |
| 2 | `exec-keys/` not ignored | `.gitignore` line; `test_deploy` asserts `git check-ignore approval/deploy/exec-keys/signing-keys.json` | one line + test |
| 3 | 0003 driver row runs the wrong tests | the 0003 row moves to `MUTATIONS` (all of `tests_db`); full driver re-run, 0 not as expected | config |
| 3b | «key shared across tenants» row survives | test: `agent:shared` registered in t-alpha and t-beta, same key → two intents | test |
| 4 | evidence | `reports/conformance/M8.json` for the final commit with `ci_local` (command, exit, per-step result); `reports/mutmut/M8.txt` with per-package counts, survivor ids, full SQL driver output; `.gitleaksignore` for the two base-history fixture findings (c10fd9b, 8128dee, `tests/ci/test_check_secrets.py:23`) | — |
| 5 | minors | backlog rows restored (done in this commit); test: `message-propose.output.json` status enum == `intent-status.schema.json` enum; no-key deferral logs a WARNING `executor: no active signing key` (tested); `Stand.rpc` timeout → `docs/backlog.md` | — |

Mutation testing this cycle: M8 modules only (approval `executor.py`, `keys.py`, `intents.py`; demo `effects.py`,
`provider.py`; kit `exec_token.py`) and the full SQL driver.
