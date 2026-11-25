# M11 cycle 3 — ponytail-review

Diff: `aac8ac0..4ebe3ec` (cycle 3: commit after `reports/critic/M11-cycle-2.json` up to the branch head). Reviewed against the closed checklist of `reports/critic/M11-cycle-2.json` (`required_improvements` 1–5). Load assumed: one connector replica per tenant gateway, CI/`check-all` run by one person or one scheduled job.

What this change does: search now orders by `chat_ref` last, so equal results in two chats page in a stable order; a new test pages 90 tied results with page sizes 1/3/7. `check_upstream.sh --drift` looks at gxg_agents_v2 migrations after the pinned commit and fails if one mentions `telegram_archive` or `capability_hub.connections`; `check-all.sh` and a new CI job `telegram-upstream` run it. Two lines in `service-integration-v1` record the collect decisions; three backlog lines.

Checked by hand:

- Order is total: `(connection_id, chat_ref)` is `UNIQUE` and `(connection_id, peer_id, message_id)` is the primary key, so `(message_id, chat_ref)` is unique inside one Connection. Mutation "drop `c.chat_ref`" in a `git archive` copy: `test_mc02_search_pages_ties_across_chats_exactly_once` fails (`assert 90 == 80`); with the fix it passes.
- Drift check on a synthetic upstream repo (vendored migrations as the pinned commit, one new migration): small migration renaming `telegram_archive.messages.text` → exit 1 (correct). The same rename followed by padding to 163 KB → exit 0 (wrong, finding 1). Rename at the end of a 163 KB file → exit 1.

## Must fix

1. **The drift check passes when a big migration changes the archive** (`connectors/telegram/archive/check_upstream.sh:L24-28`)
   - **What this is:** For every new upstream migration the script runs `git diff … | grep -qE 'telegram_archive|capability_hub\.connections'` inside `if`, under `set -o pipefail`.
   - **Problem:** `grep -q` stops at the first match. If the diff is bigger than the pipe buffer (64 KiB), `git diff` is still writing, gets SIGPIPE and exits 141. With `pipefail` the whole pipeline is then "false", so the file is not reported. Reproduced: a 163 KB migration whose first line is `ALTER TABLE telegram_archive.messages RENAME COLUMN text TO body;` → "no telegram_archive migration changes", exit 0. The vendored migrations are already 16 and 27 KB; a squash or rebuild migration that recreates the archive is exactly the big kind.
   - **Fix:** Let grep read everything: `grep -E … >/dev/null` instead of `grep -qE`, or `grep -qE … < <(git -C … diff …)`. Add one case to `test_upstream_drift_check_fails_on_a_new_archive_migration`: the rename followed by > 64 KiB of padding must fail.
   - **If we skip it:** The ADR-0004 notification stays green on the change it exists for, and the connector's scope boundary (the service's purge trigger) can change unnoticed.

## Should fix

2. **`reports/conformance/M11.json` is still the cycle-2 file** (`reports/conformance/M11.json`, `commit` = `7d90948`)
   - **What this is:** Checklist item 4 asked to rebuild `M11.json` and `ci_local` from one full `check-all` on the final head.
   - **Problem:** The cycle-3 commit does not touch `reports/conformance/` (the plan lists it as step 4). `commit`, `ci_local` and the test counts describe `7d90948`/`b1976fd`, not `4ebe3ec`.
   - **Fix:** Run `check-all.sh` once on the final head and write that run into `M11.json` (`commit`, `ci_local`, counts).
   - **If we skip it:** The evidence file cannot be reproduced for the commit that closes the milestone (DoD-3).

3. **Unqualified names are not seen by the drift check** (`check_upstream.sh:L25`)
   - **What this is:** The check is a text search for the schema-qualified names.
   - **Problem:** `SET search_path = capability_hub; ALTER TABLE connections DROP COLUMN scope;` → exit 0 (reproduced). Upstream migrations today always qualify names, so this is a rare case.
   - **Fix:** Also match `search_path` lines, or note the limit in `SOURCE.md` as `shortcut:`.
   - **If we skip it:** A migration written in that style slips through; low chance.

6. **The 1-second flood-wait stand test fails at random** (`stand/tests/test_telegram.py:L438-452`, not changed in this cycle)
   - **What this is:** `test_ac_m11_04[1]` sets a 1 s flood wait, then makes six calls through ContextForge and expects every read to say `rate_limited`.
   - **Problem:** When the second passes mid-loop, `search` correctly stops saying `rate_limited` and the test fails. Critic's check-all: fresh stand run 49/50; 2 of 8 reruns of this test failed. So check-all is red.
   - **Fix:** Set the defer again before each read check, or check reads with a delay that cannot run out during the loop.
   - **If we skip it:** check-all on the final head stays red one run in three or four.

7. **mutmut verdicts depend on the checkout path and on load** (`scripts/ci/mutmut-equivalent.txt:85`, demo `server.x_build__mutmut_9`)
   - **What this is:** `mutmut.sh` fails on listed mutants that are now killed and on unlisted timeouts.
   - **Problem:** In a worktree under `/tmp`, `telegram tools.x_load_schema__mutmut_4` is killed (the `..` parent walk finds no file there), and demo `x_build__mutmut_9` times out under parallel stand load. `mutmut.sh` exit 1, not caused by this cycle.
   - **Fix:** backlog: path-independent `load_schema` (package data, F7); list demo `x_build_9` as a timeout, like telegram `x_build_10`.
   - **If we skip it:** DoD-4 fails outside the usual checkout or on a busy machine.

## Nice to have

4. **Worktree clones are refused** (`scripts/ci/check-all.sh:L29`): `[ -d "$upstream/.git" ]` is false for a `git worktree` (`.git` is a file). `git -C "$upstream" rev-parse --git-dir >/dev/null` accepts both.
5. **Typo** (`connectors/telegram/README.md:L49`): "grant истёк , чат" — space before the comma.

Verdict: fix 1 and 6 first, then 2 (one green check-all on the final head).

Not checked: the drift check against the real gxg_agents_v2 (no clone in this session: the repo could not be attached), so `check-all` ran with `UPSTREAM_DRIFT=skip`; GitHub Actions job `telegram-upstream` and the `GXG_AGENTS_V2_TOKEN` secret (Actions off: billing).
