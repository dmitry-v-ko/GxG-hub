# M9 final — ponytail-review

Reviewed: `3c2f0b0..40bc8a5` (branch `claude/m9-ui-bot`). The task named `244000b`; after it the branch got
`40bc8a5`, which changes code (two new tests in `approval/tests_db/test_propose.py` that kill `mutmut.sh`
survivors in `intents.py`), so `40bc8a5` is the reviewed commit. Final confirming critic (plan «Ускорение
процесса», item 1). Assumed load: one organisation, a handful of tenants, tens of approvers, one `ui` and one
`notifier` instance (compose layout, `notifier` is a single `getUpdates` consumer by design).

What this change does: it adds two new processes to the Approval Service. `ui` is a server-rendered web app:
OIDC login, lists and the intent card with an escaped preview, approve/reject through the database function
`gxg_decide`, a WebAuthn (passkey) step-up for `high_risk`, manual resolution of `outcome_unknown`, Telegram link
codes. `notifier` is a Telegram bot: it sends one hint per intent (title, target label, requester, deadline,
link; never fields or changes), a digest after five in ten minutes, and accepts only `/start` and `/link`. The
database side freezes the approver set at T1, narrows the `gxg_ui` role, and adds link-code and passkey tables.

What I checked myself, not from reports: read `app.py`, `stepup.py`, `render.py`, `cli.py`, `config.py`, all
templates and `webauthn.js`; `bot.py`, notifier `cli.py`, both Dockerfiles; migration `0002` and the changed
`gxg_decide`/`gxg_resolve_manual`; the `intents.py` T1 changes; the compose networks; the kit loader. Reran
`scripts/ci/check-all.sh` (see the JSON), ran the built notifier image against my own fake Bot API with
failures, and ran uvicorn with the ui's call shape to see its access log.

## Should fix

1. **The ui writes the OIDC authorization code into its log** (`approval/ui/src/gxg_approval_ui/cli.py:57`)
   - **What this is:** After login the IdP sends the browser to `/auth/callback?code=…&state=…`, and the ui
     forwards it to `/auth/complete?code=…&state=…`.
   - **Problem:** `uvicorn.run` keeps its default access log, which prints the path with the query string at INFO.
     Reproduced with the same call shape: `INFO: 127.0.0.1:58814 - "GET /auth/callback?code=AUTHCODE-SECRET-123&state=STATE-xyz HTTP/1.1" 200 OK`.
     Every login puts a fresh authorization code twice into the ui log. SEC-17 says no secret goes to logs.
     The code alone cannot be redeemed (PKCE verifier in the user's cookie, client secret in the ui), so this is
     defence in depth, not an open door.
   - **Fix:** `uvicorn.run(..., access_log=False)` (the app logs nothing per request), or a log filter that cuts the
     query. One argument plus one assertion in `test_cli.py`.
   - **If we skip it:** Log readers (and log shippers) get one-time login codes; if a future change drops PKCE or
     leaks the client secret, the log becomes a session factory.

2. **Preview text stays in plaintext in `outbox` forever** (`approval/src/gxg_approval/intents.py:313-322`)
   - **What this is:** At T1 the api now writes `title`, `target_label`, `requester` and the approver list into the
     `notify` outbox row, so that the notifier (which has no preview key, AS-28) can build the AS-19 message.
   - **Problem:** Nothing ever clears or deletes these rows: the notifier only sets `delivered_at`, and no purge
     touches `outbox`. `target.label` is the recipient (for example a person's or a chat's name). After the RET-01
     purge (30 days) wipes `preview_ciphertext`, the label is still readable in `outbox`; after a tenant's data key
     is destroyed (RET-03, crypto-shredding) it is still readable too.
   - **Fix:** In M10's purge (AC-M10-03) also null the payload of delivered `notify` rows (or delete them), and add
     one assertion to that test. No M9 code change needed if M10 owns it; record it in the backlog.
   - **If we skip it:** Retention promises in RET-01/RET-03 are quietly false for recipient names.

3. **The pending list offers intents the person may not decide** (`approval/ui/src/gxg_approval_ui/app.py:92, 380`)
   - **What this is:** «Ожидают решения» lists every `pending_approval` intent of the tenants where the person is an
     approver, and the card shows the Approve/Reject buttons when the bundle role is `approver`.
   - **Problem:** Since M9 the approver set of an intent is frozen at T1 (`allowed_approvers`, policy `approvers`
     narrows it). An approver outside that set sees the intent in the list, presses Approve and gets
     «approver is not allowed to decide this intent» (403 from `gxg_decide`). Safe, but the list is wrong for them
     and the notifier, which already uses the frozen set, disagrees with the ui.
   - **Fix:** Add `AND %s = ANY(allowed_approvers)` to the pending query and to `can_decide` (the column is already
     readable by `gxg_ui`). Two lines and one test.
   - **If we skip it:** Confused approvers and support questions when policies name approvers.

4. **The executor's report does not describe the reviewed commit** (`reports/conformance/M9.json`)
   - **What this is:** DoD-1 (owner decision 2026-11-21): green `check-all.sh` on the reviewed commit, run by the
     executor and again by the critic, with the output in `ci_local`.
   - **Problem:** `commit` and `ci_local.commit` are `aaa770c`, and `ci_local.result` still says «mutmut.sh still
     running». The commit after it (`40bc8a5`) exists exactly because that `mutmut.sh` run found survivors in
     `intents.py` — so at `244000b` DoD-4 was not met, and the cycle-2 critic's condition («score holds if
     mutmut.sh exits 0») was not satisfied there. The report never says so.
   - **Fix:** A reports-only commit: `commit`/`ci_local` for `40bc8a5` with the result of `check-all.sh`.
   - **If we skip it:** The evidence trail claims a run that did not finish and hides that it failed.

## Nice to have

5. **`getUpdates: getUpdates: …` in the notifier log** (`approval/notifier/src/gxg_approval_notifier/cli.py:40`).
   `BotError` already starts with the method name; the format adds it again. Drop the prefix.

6. **AC-M10-08 sits under the «Этап D» heading** (`docs/implementation-plan-v1.md:476`). The row was appended after
   the M10 table's `---`, so it renders as a stray table line under M11's stage. Move it into the M10 table.

7. **`check-all.sh` fails on a shallow clone** (`scripts/ci/gitleaks.sh range`, pre-existing). A fresh session
   clone is shallow; gitleaks then reports the parentless boundary commits (5 «generic-api-key» findings in
   `tests/ci/test_check_secrets.py` at `190786d`, `204d97d`, `ba2fa1b`, `2d46c1b`, `90dde17`). After
   `git fetch --unshallow` the same scan is clean. Fail early with «unshallow first» when
   `git rev-parse --is-shallow-repository` is true. Not M9 code.

Known and already in the backlog (not repeated as findings): passkey registration with a session only (M9-F8 →
AS-30, M10), unmarked invisible characters in Telegram text (F10), single-host OIDC relay (F11), no notifier health
signal (F12), cycle-2 N1–N4.

Verdict: Ship. Findings 1–4 are minor and go to the backlog; 4 is a reports-only fix the owner of the milestone
should make before the PR.
Lean: -0 lines possible (nothing to delete; 1 and 3 are additions of one line each).
Not checked: real Telegram and a real IdP through the `egress` profile (needs internet; the AC uses the `test`
profile); Tailscale-only inbound (M14).
