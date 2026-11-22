# M8 cycle 1 — ponytail-review of `3c2f0b0..481ef5f` (independent critic)

What this change does: it adds the `executor` worker of the Approval Service (it takes approved intents from the
outbox, reserves them, signs a one-minute execution token, calls the connector's internal `execute`, records the
result, and asks the connector to `reconcile` when the result is unknown), a CLI to rotate and remove the executor's
signing keys, and an `api` that re-reads the public key file (JWKS) on change. In `demo` it adds the
`demo.message.propose` tool, the internal port 8081 with `execute`/`reconcile`, and a fake provider with a SQLite
journal. On the M5 stand it removes the permanent decoy `*.execute` tool and proves EX-01 with a temporary probe.

Load assumed (approval/README.md «Развёртывание», plan M8): a handful of tenants, tens of intents per minute, a few
executor instances on one PostgreSQL; the compose file `approval/deploy/compose.yaml` is the reference deployment.

Read beyond the diff: `0001_core.py` (T7, T9, T12a, `_gxg_move`, `_gxg_execution`), `intents.dispatch`, `api.py`
dispatch route, kit `exec_token`/`_jwt`, `tools.DemoTool.run`, `server.BundleFiles`, `config.bundle_store`,
`effects_stack.py`, the specs named in the task. Experiments (outside the repository, files removed afterwards) are
quoted where they prove a finding.

## Must fix

1. **A repeated `propose` breaks the agent's MCP client once the intent has moved on**
   (`connectors/demo/schemas/message-propose.output.json:9`, `connectors/demo/src/demo_connector/effects.py:153-157`)
   - **What this is:** EFF-08 / EX-06: a repeat of `propose` with the same `idempotency_key` and the same args returns
     the existing intent (`api` answers 200 with its current status). The tool's output schema is published in
     `tools/list`, and the MCP client checks every result against it.
   - **Problem:** the schema allows only `pending_approval` and `approved`. Signals repeats `propose` with
     `signals:<candidate>:v<n>`; after the intent was executed, `api` returns `status: succeeded` (or `executing`,
     `failed`, `rejected`, …) and the connector passes it on. The client then raises instead of returning the intent.
     Experiment (demo test fixtures, `MockApi` answering 200 with each status): `succeeded` and `executing` →
     `RuntimeError: Invalid structured content returned by tool demo.message.propose: 'executing' is not one of
     ['pending_approval', 'approved']`; `pending_approval` → fine. No test repeats `propose` after a status change.
   - **Fix:** take the status enum from `schemas/intent-status.schema.json` (all 11 statuses) in
     `message-propose.output.json`; add one end-to-end test: propose → succeeded → the same propose again → 200
     with the same `intent_id` and `status: succeeded` through a real MCP client.
   - **If we skip it:** every idempotent retry of a business service (the main reason EFF-08 exists) fails with a
     client exception after the first execution.

2. **Without an active signing key every approved intent burns its attempts in a second and ends `failed`**
   (`approval/src/gxg_approval/executor.py:119-142, 170-190`; `approval/README.md:44`)
   - **What this is:** the executor reserves the intent (T7) first and only then asks `SigningKeys.current()` for a key.
     With no key whose `not_before` has passed, `current()` raises `temporarily_unavailable`; `execute()` catches it
     as `no_response`, the execution is still `reserved`, so it becomes T12a (`refused_before_dispatch`, attempt
     counted), the intent goes back to `approved`, a new outbox row is due at once, and so on.
   - **Problem:** this is exactly the state after the documented emergency step for the usual one-key setup:
     `keys rotate` (new key active only in 15 min) + `keys remove --kid <old> --compromised`; also after
     `keys remove` of the old key before the new key's `not_before` (AS-10 «старый остаётся 24 ч» is not enforced).
     Experiment on the real stack (`stack` fixture of `test_executor.py`, signing list with `not_before` in 2999,
     10 × `run_once`): `failed`, executions `[(1, failed_before_effect, no_response), (2, …), (3, …)]`, attempts 3.
     The README row «Компрометация» promises the opposite: «без него исполнения ждут `rotate` + 15 мин».
     Approvals given by humans are lost; the recorded reason (`no_response`) hides the cause.
   - **Fix:** check `keys.current()` before `gxg_reserve` in `take()`; with no active key, leave the outbox row
     undelivered (the same path as the kill-switch deferral) and do not reserve. Optionally refuse
     `keys remove` (without `--compromised`) of the only active key. One test: no active key → intent stays
     `approved`, `attempts` 0, no executions; key becomes active → executed.
   - **If we skip it:** an incident response (key compromise) silently fails every approved intent of every tenant.

3. **Key rotation does not work in the reference deployment; the compromise path of the CLI has no test**
   (`approval/deploy/compose.yaml:80-82`, `approval/src/gxg_approval/keys.py:25-29`, `approval/src/gxg_approval/cli.py:93-109`)
   - **What this is:** AC-M8-14 / AS-10 «ротация без простоя»: the executor re-reads `GXG_EXEC_SIGNING_KEYS_FILE`
     (`config.JsonFile`) and `api` re-reads the JWKS file. In compose the executor's key file is a Docker secret
     (`exec_signing_keys` → `/run/secrets/exec_signing_keys`), i.e. a single-file bind mount; `keys rotate` writes
     with `os.replace` (new inode).
   - **Problem:** a single-file bind mount keeps showing the old inode after a rename on the host. Experiment:
     `docker run -v $D/keys:/run/secrets/keys:ro …`, then `os.replace` of `$D/keys` on the host → host `v2`,
     container still `v1`. So the executor never sees the new key until a restart; after the old key is removed
     from the JWKS 24 h later, every dispatch is refused (and with finding 2 every intent fails). The procedure
     also cannot run in one place: `keys rotate|remove` needs both files, but the JWKS lives in `api`'s
     `./config` and the key file only in the executor's secret; `--compromised` needs the executor's DB DSN and
     the `internal` network, which the host cannot reach. The test of AC-M8-14 runs in one process and replaces
     the lists in memory, and `cli._keys` (the command the README tells the operator to run) and `cli._executor`
     are not tested at all; `cli._keys` repeats the release SQL instead of using `Executor.release_reserved`.
   - **Fix:** mount the executor key file from a directory (`./exec-keys:/run/gxg/exec-keys:ro`, file inside), the
     same way `api` gets `./config`; say in the README which command runs where (rotate/remove on the host over the
     two directories, `--compromised` release via `docker compose exec executor gxg-approval keys release` or the
     like); a test of `cli.main(["keys", "remove", "--kid", K, "--compromised"])` against the DB, and a
     `test_deploy.py` check that the executor key file is not a single-file mount.
   - **If we skip it:** the first real rotation (SEC-10: at least every 90 days) stops execution.

## Should fix

4. **The private key file is world-readable for a moment** (`approval/src/gxg_approval/keys.py:26-28`)
   - **What this is:** `_write` creates `exec-keys.json.tmp` with `write_bytes` (default umask, usually 0644) and
     only then `chmod 0600`.
   - **Problem:** between the two calls any local user who can list the directory can read the new seed (e.g. an
     inotify watcher on the host's `secrets/` directory with 0755).
   - **Fix:** `fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)` and write through it. Two lines.
   - **If we skip it:** a leaked execution-token key on a shared operator host.

5. **The executor keeps the connector's old URL forever** (`approval/src/gxg_approval/cli.py:75-78`)
   - **What this is:** one `httpx.Client(base_url=service.internal_url)` per service id, cached for the life of the
     process, while the registry (and its `internal_url`) comes from the bundle that reloads every few minutes.
   - **Problem:** after a bundle moves `demo` to a new internal URL, the executor still posts to the old one; a
     refused connection is `no_response` on a `reserved` execution → T12a, three attempts → `failed`.
   - **Fix:** key the cache by `(service.id, service.internal_url)`. One line.
   - **If we skip it:** a connector move needs an executor restart, and intents fail until someone notices.

6. **An SQL mutant is labelled equivalent but is not** (`scripts/ci/mutate_m7_sql.py`, M8 row «CONTINUE WHEN …»;
   `migrations/versions/0002_executor.py:24`)
   - **What this is:** `gxg_release_reserved` loops over the `reserved` executions it selected, then locks each one;
     `CONTINUE WHEN (x.e).state <> 'reserved'` skips one that was dispatched between the select and the lock.
   - **Problem:** without the line, such an execution is updated to `released` and then `_gxg_check` refuses
     `reserve_lease_expired` from `dispatching` with GX409, so the whole call aborts and *no* reservation is
     released. That is a different result (an error instead of a skip), so the mutant is not equivalent; it
     survives because no test pins the race. A deterministic test is easy: hold the intent row lock in one
     connection, move the execution to `dispatching`, start the release in a thread, commit. Safety holds
     (no double effect), so this is a test gap, not a bug.
   - **Fix:** that test, and the row without the `equivalent:` prefix.
   - **If we skip it:** the guard can be deleted later with green tests.

7. **The provider gets the body's args, not the canonical ones** (`connectors/demo/src/demo_connector/effects.py:222-229`)
   - **What this is:** `_checked` compares the hash of `canonical.normalize(args)` with `ahs`; `execute` then sends
     the raw `args["text"]` to the provider.
   - **Problem:** a text in NFD form (e.g. `"é"`) passes the hash check (it normalizes to the approved NFC
     text) and is sent as NFD: the provider's `text_hash` differs from the approved text. Today only a broken or
     compromised executor sends that, so it is defence in depth.
   - **Fix:** keep the normalized args from the check and send those. One line.

8. **Probes of the new worker are missing** (`approval/src/gxg_approval/cli.py:62-90`)
   - **What this is:** approval-service-v1 «Развёртывание»: `/healthz` and `/readyz` at every process; `readyz` checks
     the DB, a valid bundle and the signing key for the executor.
   - **Problem:** the executor has no probe; a dead DB connection or an expired bundle is invisible to the
     orchestrator. Already in the M7 backlog for `sweeper`/`mcp` (M10/M14); now it covers one more process.
   - **Fix:** extend the backlog item to `executor` (with «ключ подписи» from finding 2).

9. **Repeated helpers and error handling** (`effects.py:41,50,86-87` vs `tools.py:44,53,64-65`; `effects.py:173-207`
   vs `tools.py:77-110`)
   - **What this is:** `DOMAIN`, `RTYPE`, `NOT_FOUND` and `rid()` are copied in `effects.py`; `_endpoint` repeats the
     internal-error branch of `DemoTool.run` (traceback frames, `internal_error` envelope, audit line in `finally`).
   - **Fix:** `tools.py` imports them from `effects.py` (it already imports `Effects`); one small function for the
     internal-error branch used by both.
   - Lean: about 20 lines.

## Nice to have

10. **`reports/mutmut/M8.txt` is a summary, not the mutmut output.** It has no counts for `executor.py`/`keys.py`
    (killed/survived/timeout) and states the full SQL driver run was not repeated. My own runs are in the JSON report.
11. **DoD-6 table misses three self-written parts:** `config.JsonFile` (re-read on mtime; alternatives `watchfiles`,
    `watchdog` are not needed for one stat call), `SigningKeys` (newest key by `not_before`), and the dispatched-jti
    memory `Effects._dispatched` (the kit has `_jwt.JtiCache`, which does not answer «was this jti dispatched» and is
    not a fit). All three are justified custom code; only the record is missing.
12. **EX-01 on the stand runs `demo` without effect tools.** The stand checks that ContextForge publishes no
    `*execute*` and that `unified_pdp` denies a registered probe; that `demo` with `DEMO_APPROVAL_URL` never
    publishes `execute` is checked at the connector (`test_propose_is_listed_and_execute_is_not`). Together this
    covers AC-M8-13; `propose` through ContextForge (identity binding of its args) is not exercised anywhere yet.

Verdict: fix 1, 2 and 3 first.
Lean: -20 lines possible.
Not checked: GitHub CI (runners not assigned; every CI step re-run locally), the full mutmut run of the demo
connector (only my targeted runs, see the JSON report).
