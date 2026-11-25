# M9 cycle 1 — plan (ponytail ultra)

Scope: `docs/implementation-plan-v1.md` M9, AC-M9-01…12. Specs: approval-service-v1 (Web UI, Notify-бот, AS-15…20,
AS-27…29), effects-and-approval-v1 (EFF-10…15, EFF-30…33), approval-policy-v1 (AP-08, AP-09), security-v1 (SEC-06,
SEC-07, SEC-08, SEC-12), conformance-matrix-v1 (EX-07, EX-08, EX-15, EX-19). Process: «Ускорение процесса (вторая
редакция)» — owner thread, self-check → `reports/conformance/M9.json` `self_check`, critic subagent, threshold 9.
Base: `origin/claude/integration` `3c2f0b0` (M0…M5, M7). M8 (executor) is not on the base.

## Questions to the need (ultra) and answers

| Part | Needed? | Decision |
| --- | --- | --- |
| htmx (stack table) | no screen needs partial updates | not added; plain forms + one static JS file for WebAuthn |
| SPA / JSON API for ui | no | server-rendered Jinja2 |
| Server-side session store | AS-27 gives `gxg_ui` no session table | signed cookie (Starlette `SessionMiddleware`, itsdangerous); 8 h absolute + 30 min idle checked from session timestamps. shortcut: no server-side revocation of a session, add a table when logout-everywhere is needed |
| `approved → succeeded` in AC-M9-01 | executor is M8 | E2E drives T7/T9/T11 through the real DB functions with the executor/api roles (test driver, not a fake status write) |
| Webhooks in `notifier` (AS-21…23) | M10 | not in M9; network policy has their egress network only |
| Admin, kill switch UI | M10 | no |
| Real OIDC / Telegram | forbidden | `oidc-provider-mock` (test profile only), fake Bot API |

## Where the change reaches

| Place | Change |
| --- | --- |
| `approval/src/gxg_approval/migrations/versions/0001_core.py` (not released) | `gxg_decide`: approver must be in `allowed_approvers` (frozen at T1, NULL → refused for human decisions); `gxg_resolve_manual`: actor must be `oidc:` |
| new `0002_ui_notifier.py` | `link_codes`, `webauthn_credentials`; `gxg_link_code` (ui), `gxg_link_claim` (notifier); narrowing: `gxg_ui` loses `INSERT outbox`, `INSERT/UPDATE approver_links` |
| `approval/src/gxg_approval/intents.py` (T1) | `allowed_approvers` = role `approver` of the tenant ∩ policy approvers (always set for `human`); preview envelope `{preview, canonical_args}` (EFF-33: ui must show args, but reads only `preview_ciphertext`); `notify` outbox payload gets `title`, `target_label`, `requester`, `expires_at`, `approvers` (AS-19; notifier cannot read `intents`) |
| `approval/tests_db/*`, `scripts/ci/mutate_m7_sql.py` | fixtures and expectations for the three T1 changes; SQL mutations of the new checks and of 0002 |
| new `approval/ui/` (own `pyproject.toml`, `uv.lock`, `Dockerfile`) | `gxg_approval_ui`: app, OIDC, session, CSRF, WebAuthn, preview rendering, templates, CLI |
| new `approval/notifier/` (own lockfile, `Dockerfile`) | `gxg_approval_notifier`: outbox → Telegram, long polling, `/start`, `/link`, AP-08 digest; `testing/fake_bot_api.py` (stdlib) |
| `approval/deploy/compose.yaml` | `ui`, `notifier`, ingress, networks per AS-29, test profile (`oidc`, `fake-telegram`) |
| root `pyproject.toml`, `uv.lock` | path sources for ui/notifier (like `connectors/demo`), dev deps `oidc-provider-mock`; workspace `exclude` |
| `scripts/ci/check-all.sh`, `.github/workflows/ci.yml`, `scripts/ci/mutmut.sh` | lock checks, DB tests, E2E, network-policy test, mutmut of ui/notifier modules |

## DoD-6: ready-made solutions

| Component | Taken | Considered | Why |
| --- | --- | --- | --- |
| OIDC client | **authlib** Starlette client (spec stack: «authlib») | python-jose + own code flow; `fastapi-oidc` | authlib does discovery, PKCE, state, nonce, ID-token signature/claims |
| Test OIDC issuer | **oidc-provider-mock 0.4.8** (dev dependency, subprocess) | Keycloak/dex/mock-oauth2-server containers; own authlib server | real discovery + JWKS + code flow, pip-installable, no JVM; only in tests and the compose `test` profile |
| WebAuthn | **py_webauthn (`webauthn`) 2.7** | `fido2` server API; 3.x | Duo's library, verification of registration and assertion incl. challenge/origin/RP/sign count; 3.x needs cryptography ≥ 49 while the root lock has 46 |
| Templates | **Jinja2** with autoescape (spec stack) | f-strings, markupsafe only | autoescape is the XSS control (AS-15) |
| Sessions | **Starlette `SessionMiddleware`** (itsdangerous, already a dependency of starlette) | server-side store | no table in AS-27 for sessions; cookie flags configurable: name `__Host-gxg`, `https_only`, `same_site=strict`, path `/`, HttpOnly always |
| CSRF | synchronizer token in the signed session, `hmac.compare_digest` (~6 lines) | **starlette-csrf**, **asgi-csrf**, fastapi-csrf-protect | the libraries are double-submit cookie: a second cookie the page JS must read (not HttpOnly, breaks AC-M9-06 for all cookies) or a header-only check unusable for plain forms; the session is already signed, so the token in it is the classic synchronizer pattern |
| Invisible-character markers (EFF-32) | stdlib `unicodedata.category` (Cc, Cf, Zl, Zp except `\n`, `\t`) | `bleach`, `confusable_homoglyphs` | bleach sanitizes HTML (we print text only); no library renders `⟦U+XXXX⟧` |
| Telegram client | **httpx** (already in the tree), two methods | python-telegram-bot, aiogram | full frameworks for `getUpdates` + `sendMessage`; callback/webhook machinery we must not have (AS-18, AS-20) |
| Fake Bot API | own, stdlib `http.server` + the same handler as an `httpx.MockTransport` (~80 lines) | `telegram-bot-api` local server (real server, needs real api_id), PTB test utilities | the task asks for a local server by Bot API contract; no maintained fake exists for the two methods with injectable updates |
| AP-08 window | in-memory deque per chat | DB counter | `gxg_notifier` cannot write any table but `audit_events`; Telegram allows one `getUpdates` consumer per token, so one notifier instance. shortcut: window resets on restart (at most 5 extra hints), move to a table if the bot ever runs in more instances |
| Link code store | table `link_codes` + two `SECURITY DEFINER` functions | HMAC code shared by ui and notifier | AS-27 gives the notifier only `SELECT approver_links`: the bot learns `(code, telegram_user_id)` but cannot write it (spec gap, `pending_owner_decision`); a shared HMAC key would be a new cross-process secret (AS-28) |
| Passkey store | table `webauthn_credentials` | none in AS-27 | AS-17 needs registered passkeys; spec gap, `pending_owner_decision` |
| Network policy (AC-M9-12) | Docker Compose `internal: true` networks + one ingress TCP relay (`alpine/socat`) | k8s NetworkPolicy, iptables egress proxy | compose is the stand of the repo; inbound-only needs a published port on a non-internal network, so ui sits on internal networks behind the relay |
| Browser E2E | Playwright (Node, preinstalled) incl. CDP virtual authenticator | Selenium | given by the task |

## Owner questions (defaults, `pending_owner_decision`)

1. Compromised `ui` forging a decision (M7 threat model): default = AS-27 matrix + narrowing above; open paths in
   `reports/conformance/M9.json` and proposal (WebAuthn assertion verified outside `ui`) in `reports/M9-ESCALATION.md`.
2. Approver set frozen at T1 into `allowed_approvers`: an approver added to the bundle after T1 cannot decide that
   intent (≤ approval TTL). Removed approvers are refused by the ui's current-bundle check.
3. Link-code and passkey tables (AS-27 has none); notifier writes `approver_links` only through `gxg_link_claim`.
4. Canonical args inside `preview_ciphertext` (EFF-33 vs. AS-27 column split).

## Not done, on purpose

Spec, AC, rubric, threshold unchanged. No htmx, no logout-everywhere, no webhooks, no unlink of Telegram.
