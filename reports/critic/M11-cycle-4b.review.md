# M11 cycle 4 — ponytail-review (second independent critic; the first is reports/critic/M11-cycle-4.review.md, d355517)

Diff: `7dd0c7c..a484ec2` (from the commit that added `reports/critic/M11-cycle-3.json` to the head; the merge `42879bd` brings in `b5f5b00`, `da3e25f`, then `31772e8`, `a484ec2`). Code at the head = code at `31772e8`: `a484ec2` changes only `reports/conformance/M11.json`.

Load assumed: CI and one developer running `check-all`; the drift check runs once per CI run over a handful of upstream migrations.

What this change does: the upstream schema drift check (`check_upstream.sh --drift`) now lets `grep` read the whole diff, so a big migration no longer hides behind SIGPIPE under `pipefail`, and it also flags `search_path`. The stand test for AC-M11-04 re-arms the flood-wait deferral before every call. `check-all` accepts a `git worktree` clone, the README typo is fixed, three backlog lines are added, and `M11.json` is regenerated from one full `check-all` on `31772e8`.

## Checked

- `check_upstream.sh:24-28`: `grep -E … >/dev/null` reads to EOF, so `git diff` is never cut off; `set -euo pipefail` is still on. The `if` keeps a failing `git diff` from aborting, but `changed=$(git diff --name-only …)` before it already fails the script on a bad ref, so a broken pipe cannot turn into a silent "clean".
- New test cases: match + `"-- padding\n" * 20000` (≈ 220 KB, more than the 64 KiB pipe buffer) and `SET search_path` + unqualified name. On the cycle-3 script (`4ebe3ec`) both fail (2 failed, 3 passed); on the head all 5 pass.
- `stand/tests/test_telegram.py:438-452`: `defer(seconds)` before each `collect`, `defer(3600)` before each read; `finally: undefer()`. Reads only check the flag, so the 1 s window no longer has to last through six calls.
- `test_duration_is_in_milliseconds`: it waits on `pg_stat_activity.wait_event_type = 'Lock'` for at most 30 s, then sleeps 0.3 s. It does not fail on a timeout, so the 300 ms bound still holds after the wait.
- `check-all.sh:29`: `git -C … rev-parse --git-dir` accepts a worktree and a normal clone. It also accepts a path inside any other repository (e.g. a subdirectory of GxG-hub), but then `check_upstream.sh` compares hashes at the pinned commit and fails, so nothing goes green by mistake.

## Findings

### Nice to have

1. **The 1 s case still assumes one call takes under a second** (`stand/tests/test_telegram.py:443-445`)
   - **What this is:** For `seconds=1` the deferral ends 1 s after `defer()` commits. `collect` must reach the archive before that.
   - **Problem:** If one call through ContextForge takes more than 1 s (very heavy machine load), `collect` correctly stops being `rate_limited` and the test fails. The window is now one call instead of six, and the docstring states the assumption.
   - **Fix:** None needed now. If it ever flakes, use 2 s as the shortest case or check `retry_after_s` only for 7 and 3600.
   - **If we skip it:** A rare red stand run under extreme load.

2. **Stale count in `M11.json` evidence** (`reports/conformance/M11.json`, `acceptance[AC-M11-01].evidence`)
   - **What this is:** The AC-M11-01 text says "in-process twins … (105 passed)".
   - **Problem:** The same file's `ci_local` and `self_check` say 110 (critic run: 110 passed). The text was carried over from an earlier cycle.
   - **Fix:** Write "110 passed" when the file is next regenerated.
   - **If we skip it:** A reader sees two different numbers.

Verdict: Ship.

Not checked: the drift check against the real gxg_agents_v2 clone (no access in this session; `UPSTREAM_DRIFT=skip`), GitHub Actions job `telegram-upstream`.

## Critic runs (head `a484ec2`, code = `31772e8`)

- `connectors/telegram`: `uv run pytest` → 110 passed (PostgreSQL 16 + pgvector archive).
- Drift test: 5 passed; on the cycle-3 `check_upstream.sh` (`4ebe3ec`) the padding and `search_path` cases fail (2 failed).
- `UPSTREAM_DRIFT=skip KEEP=1 scripts/ci/check-all.sh`, full history (309 commits): lockfiles, ruff, mypy, root 908, kit coverage 360, demo 64, telegram 110, prove_schema_guard, gitleaks tree + range no leaks, tests/secrets 21, smoke; stand fresh 49/50, after-restart 50/50, after-recreate 50/50. The one failure is M5 `test_mc02_search_schema_pagination_and_limits`: `docker run` exit 125 while the fresh daemon was still pulling `python:3.12-slim` (dockerd: "layer … locked"). It is an environment failure in a test this diff does not touch; 3 reruns passed. A first try on a shallow clone (57 commits) failed gitleaks range on a boundary commit; after `git fetch --unshallow` there were no leaks.
- `test_ac_m11_04` on the kept stand: 8/8 runs green, then 10/10 green under mutmut load (load average 5), plus 3/3 in the stand suites. Cycle 3: 2 of 8 failed.
- `scripts/ci/mutmut.sh`: telegram 1279 mutants, 1257 killed; not killed 102, listed 101. The extra one is `tools.run__mutmut_3`, a timeout while the stand loop ran in parallel; run alone it is killed. This is backlog F6 (verdicts depend on load), not new.
- `M11.json`: `commit` 31772e8, `commit_dirty` false, `ci_local` one run with the clone, EXIT 0. Its counts (908 / 360 / 64 / 110 / 3 × 50, gitleaks 308 commits = 309 at the head minus `a484ec2`, mutmut 1279/1258) match this run.
