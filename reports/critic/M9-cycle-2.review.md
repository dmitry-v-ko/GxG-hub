# M9 cycle 2 — ponytail-review of `2c9557c..aaa770c`

Reviewer: independent critic, cycle 2. Code under review: `aaa770c` (`4f33c0e` adds only the conformance report).
Load assumed: one `ui` process and one `notifier` process per installation (Telegram allows one `getUpdates`
consumer per token), a few dozen approvers, a few hundred intents a day (approval-service-v1 «Развёртывание»).

## What this change does

It closes the cycle-1 checklist. The notifier no longer lets httpx log the Bot API URL (which holds the bot token);
`rstrip("/")` becomes `removesuffix("/")`; the ui `cli.main` gets 13 tests and joins mutation testing; one unreadable
preview no longer turns the lists into 503; the step-up docstring and README now say what really stops a replay;
the signed-bundle loader moves into `gxg_connector_kit.grants.bundle_store_from_env`; `version` is bounded to a
PostgreSQL `integer`; a two-tenant link test and its SQL mutation are added; the backlog gets F8, F10, F11, F12 and
the notifier outbox item.

Checked by hand, beyond reading: the built `gxg-approval-notifier:m9-test` image made 5244 `getUpdates` calls to a
local fake Bot API with token `999:CRITIC-DEMO-SECRET`, and the token is in 0 log lines (cycle 1: every call).

## Should fix

1. **The ui now crashes with a traceback instead of naming a missing bundle variable**
   (`packages/gxg-connector-kit/src/gxg_connector_kit/grants.py:328-330`, called from
   `approval/ui/src/gxg_approval_ui/cli.py:49`)
   - **What this is:** At start the ui reads three bundle settings: `GXG_BUNDLE_KEYS`, `GXG_BUNDLE_STATE_FILE`,
     `GXG_BUNDLE_DIR`.
   - **Problem:** Before this change the ui read them with its own `env()`, which stops with
     `missing configuration: GXG_BUNDLE_KEYS`. The shared loader reads `environ["..."]` directly. Now an unset
     variable gives `KeyError: 'GXG_BUNDLE_KEYS'` with a stack trace, and an empty one gives a `JSONDecodeError`
     (checked: `bundle_store_from_env({})` → `KeyError`). `test_cli.REQUIRED` leaves these three out, so no test
     noticed. Every other ui variable still gets the clear message.
   - **Fix:** In `cli.main`, call `env()` for the three names before `bundle_store_from_env` (or have the kit
     helper raise `SystemExit(f"missing configuration: {name}")`), and add the three names to `REQUIRED`.
   - **If we skip it:** An operator who forgets one variable gets a stack trace instead of the variable's name.
     Nothing unsafe happens: the process does not start.

## Nice to have

2. **Tests leave threads and pools running for the rest of the session**
   (`packages/gxg-connector-kit/tests/test_grants.py:505-520`, `approval/ui/tests/test_cli.py:120-127`)
   - **What this is:** `bundle_store_from_env` starts a daemon thread that reloads the bundle; `cli.main` opens a
     psycopg pool to `host=/nonexistent`.
   - **Problem:** The reload test starts a thread with `reload_s=0.05` that is never stopped, so it re-reads two files
     every 50 ms until pytest exits. In `test_every_setting_is_required` the pool is opened before
     `GXG_PREVIEW_KEYS_FILE`, `GXG_UI_SESSION_KEY_FILE` and `GXG_OIDC_CLIENT_SECRET_FILE` are read, so those three
     cases leave an open pool that keeps retrying the connection in the background.
   - **Fix:** Give the loader a stop event (or return the thread) and stop it in the test; close
     `started["deps"].pool` only where it exists, or patch `ConnectionPool` in the fixture.
   - **If we skip it:** A slightly slower and noisier test run. No wrong result found.

3. **The ui's own httpx silencing has no test** (`approval/ui/src/gxg_approval_ui/app.py:79-80`)
   - **What this is:** The ui sets `httpx`, `httpx2`, `httpcore` to WARNING "for symmetry" with the notifier.
   - **Problem:** The loop runs at import time, where mutmut cannot mutate it, and no test reads the levels. The ui's
     IdP URLs carry no secret (the code and client secret go in the POST body), so nothing leaks today if it is
     removed.
   - **Fix:** One assertion like the notifier's `test_bot_module_sets_the_httpx_level`, or drop the loop.
   - **If we skip it:** None now; a later secret in an IdP URL would not be caught.

4. **One-line wrapper left behind** (`approval/src/gxg_approval/config.py:48-49`)
   - **What this is:** `config.bundle_store(environ)` now only returns `bundle_store_from_env(environ)`.
   - **Fix:** Call the kit function from `approval/cli.py` and the test directly; delete the wrapper and the
     now-unused `BundleStore` import.
   - **If we skip it:** Three extra lines.

Checked and fine: `removesuffix` in both places and the two removed equivalent entries; `summary()` catches only
`Refused` (missing key and `CryptoError` both raise it, malformed JSON still raises — the api writes that JSON);
`_version` (`int()` errors, 0, −1, 2³¹ → 400; 2³¹−1 → 409; both decision and resolve forms); the F5 test replays
an old cookie with a zero-counter key and gets exactly 409 and one decision row; the F14 test fails under
`c.tenant_ids[1:1]` (its first assert counts the link rows); `shortcut:` comments at `SessionMiddleware` and
`/logout`; all four callers of the old loaders moved; backlog rows present.

Verdict: Ship (1 is a small ops regression, fix with the next change).
Lean: -5 lines possible.
Not checked: the full `scripts/ci/check-all.sh` exit code on `aaa770c` (its last step, `mutmut.sh`, was still running
in the owner's thread when this review was written).
