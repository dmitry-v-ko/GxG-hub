# M11 cycle 4 — ponytail-review

Diff: `7dd0c7c..a484ec2` (cycle 4: everything after the cycle-3 critic report; code head `31772e8`, `a484ec2` adds only `reports/conformance/M11.json`). The code part of the cycle is `da3e25f` and `b5f5b00` (merged in `42879bd`) plus `31772e8`. Milestone diff `fb14af1..a484ec2` was re-read where the cycle touches it (`check_upstream.sh`, `check-all.sh`, stand AC-M11-04 test, telegram duration test). Reviewed against the closed checklist of `reports/critic/M11-cycle-3.json` (`required_improvements` 1–4). Load assumed: one connector replica per tenant gateway; `check-all` run by one person or one scheduled job; gxg_agents_v2 adds a few migrations a week (15 between 2026-10-13 and 2026-11-06).

What this change does: `check_upstream.sh --drift` now lets `grep` read the whole diff of each new upstream migration (no `grep -q`, so no SIGPIPE under `pipefail`) and also flags any migration that mentions `search_path`. `check-all.sh` accepts a `git worktree` clone. The stand test for AC-M11-04 sets the flood wait again before every call (the given delay before `collect`, 3600 s before the reads). The telegram duration test waits until the call is really blocked on the lock. Backlog gets F4–F6, README typo fixed, `M11.json` rebuilt from one full `check-all` on `31772e8`.

Checked by hand:

- Root `pytest`: 908 passed. `connectors/telegram`: 110 passed (the two new drift cases included).
- `check_upstream.sh /home/user/gxg_agents_v2` → equal at `f1c4d6d`, exit 0; `--drift` → no changes `f1c4d6d..origin/HEAD`, exit 0.
- Synthetic upstream repos (vendored migrations as the pin, one new migration): rename of `telegram_archive.messages.text` + 220 KB padding → old script (`4ebe3ec`) exit 0, new script exit 1. So checklist item 1 is closed and the new test case kills the old bug.
- Deleting a vendored migration upstream → exit 1 (correct).
- `gitleaks tree HEAD` and `range 0000…..HEAD` (309 commits): no leaks.
- Stand: 2 quick runs of `stand/tests/test_telegram.py` 21/21; 6 reruns of `-k ac_m11_04` 3/3 each.
- Full `check-all.sh` on `a484ec2` with the clone, no `UPSTREAM_DRIFT=skip`: `check-all: OK`, exit 0, 70 min; stand 3 x 50/50; `mutmut.sh` OK (not killed 101 = listed 101).

## Must fix

None.

## Should fix

1. **The new `search_path` match fires on about half of all unrelated upstream migrations** (`connectors/telegram/archive/check_upstream.sh:L24-26`, from `da3e25f`)
   - **What this is:** To catch `SET search_path = capability_hub; ALTER TABLE connections …`, the script now marks any new migration whose diff contains the word `search_path`.
   - **Problem:** gxg_agents_v2 puts `SET search_path = pg_catalog, …` on almost every `SECURITY DEFINER` function. 65 of its 148 migrations contain `search_path`; only 11 touch `telegram_archive` or `capability_hub.connections`. Of the last 15 real migrations, 7 would be flagged although they do not touch the archive. Reproduced: the real `20261101010000_capability_hub_fractional_usage.sql` added after the pin → "changed the telegram_archive contract", exit 1 (the old script: exit 0). Each such alarm turns `check-all` red for every milestone until someone bumps the pin in `SOURCE.md`, and teaches people to bump it without looking — the opposite of what the ADR-0004 notification is for. It fails closed, so it is not a hole.
   - **Fix:** Drop the bare `search_path` match and write the limit as a `shortcut:` line in `SOURCE.md` (the other option the cycle-3 critic gave), or match only `search_path` lines that name `telegram_archive`/`capability_hub` at statement level. Add one false-positive case (a function with `SET search_path = pg_catalog, public`) expecting exit 0.
   - **If we skip it:** About every second upstream migration makes the Hub gate red for a non-reason.

2. **File names with a space or non-ASCII letters are never checked** (`check_upstream.sh:L21-23`, `for f in $changed`, since cycle 3)
   - **What this is:** The script lists changed files with `git diff --name-only` and loops over them unquoted.
   - **Problem:** `20270101 next.sql` is split into two words that match no file; `20270101_архив.sql` is printed C-quoted by git (`core.quotePath`) and matches nothing. Both, with `ALTER TABLE telegram_archive.messages RENAME COLUMN text TO body;` inside → "no telegram_archive migration changes", exit 0 (reproduced). Upstream names are `<timestamp>_<snake_case>.sql` today, so the chance is low.
   - **Fix (also shorter):** replace the loop with git's own content filter, one command: `hits=$(git -C "$upstream" -c core.quotePath=false diff --name-only -G '<regex>' "$commit" "$ref" -- supabase/migrations)`. Checked on the same synthetic repos: it finds the padded, spaced, non-ASCII and deleted cases. No pipe, so no SIGPIPE question either.
   - **If we skip it:** A migration with an unusual name passes the notification silently.

## Nice to have

3. **Quoted or mixed-case names pass** (`check_upstream.sh:L26`): `alter table "capability_hub"."connections" drop column "metadata";` (the style `supabase db diff` writes) and `ALTER TABLE Capability_Hub.Connections …` → exit 0 (reproduced). PostgreSQL folds unquoted names to lower case, so both are the same table. Upstream writes unquoted lower case today. Fix: `-i` and `capability_hub"?\."?connections` in the regex.

4. **The 1 s flood-wait case still has a 1 s budget for one call** (`stand/tests/test_telegram.py:L441-445`): `defer(…, 1)` then one `collect` through ContextForge must arrive inside the second. It passed in every run here; with the reads re-armed to 3600 s the window covers one call, not six, which is what the cycle-3 critic asked for. Note only.

Lean: -4 lines possible (the `for` loop + `if` + pipe in `check_upstream.sh` become one `git diff -G` line).

Verdict: checklist items 1–4 of cycle 3 are done; no new major or critical. Items 1–3 above are small and go to the backlog.

Not checked: GitHub Actions job `telegram-upstream` and secret `GXG_AGENTS_V2_TOKEN` (Actions off: billing); real Supabase archive; real Telegram (not used by design).
