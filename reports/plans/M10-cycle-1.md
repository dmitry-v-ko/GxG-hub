# M10 cycle 1 — plan (ponytail ultra)

Branch `claude/m10-ops` from `origin/claude/integration@40cec84` (M3, M4.1, M5, M7, M8, M9, M11 merged).
Scope: plan «M10» (AC-M10-01…08), backlog M9 (SEC-17 access log, outbox purge), M8 `pending_owner_decision`
(`max_attempts` in the policy schema).

## Places touched

| Part | Where | Ladder step and why |
| --- | --- | --- |
| Migration `0005_ops` | `approval/src/gxg_approval/migrations/versions/0005_ops.py` | minimal code. One migration: decider role rights, `gxg_decide` with assertion, webhook events trigger, `webhook_deliveries`, `gxg_purge`, `gxg_metrics`, admin switch function, passkey invites. PostgreSQL does atomicity (AS-01) — nothing else would |
| Role `gxg_decider` | `approval/deploy/roles.sql`, `approval/tests_db/conftest.py` | one line in the role list (AS-27, AS-30) |
| decider (AS-30) | `approval/src/gxg_approval/decider.py`, `cli.py` | **existing dependency** `webauthn` (py_webauthn 2.7, already in the root lock through ui) for both ceremonies; FastAPI like `api`. Own code: the challenge rows and the call order only |
| ui → decider | `approval/ui/src/gxg_approval_ui/stepup.py` (shrinks to an HTTP client), `app.py`, `static/webauthn.js`, `templates/` | ui stops verifying WebAuthn and stops writing `webauthn_credentials`; `webauthn` leaves the ui lockfile |
| Webhooks (AS-21…23, EX-20) | `packages/gxg-connector-kit/src/gxg_connector_kit/webhooks.py` (sign/verify, used by notifier and by receivers such as Signals M12); bundle `webhooks` in `grants.py`; `approval/notifier/src/gxg_approval_notifier/webhooks.py` | stdlib `hmac`/`hashlib`. Ready libraries considered: `standardwebhooks` (another header format, `webhook-id`/`webhook-signature`, base64 — not AS-22), `svix` (hosted service). AS-22 fixes its own header, 15 lines of stdlib |
| Purge (RET-01, OPS-04 partly; backlog M9 F-2) | `gxg_purge` in 0005, `sweeper.py` | one definer function, called by the existing sweeper loop |
| Metrics (AS «Наблюдаемость») | `gxg_metrics()` in 0005, `approval/src/gxg_approval/metrics.py`, `/metrics` on `api` | **existing dependency** `prometheus_client` (custom collector + existing Counters) |
| Alert rules | `approval/deploy/prometheus/rules.yaml`, `rules.test.yaml` | **known tool** `promtool check rules` / `promtool test rules` (image `prom/prometheus`, pinned by digest) — no own rule checker |
| Admin API + CLI (AS-24, EX-13) | `approval/src/gxg_approval/admin.py`, `cli.py` (`switch set|clear`, `passkeys invite`; `keys rotate` exists since M8) | operator OIDC token verified with **existing dependency** PyJWT (`PyJWKClient`) |
| SEC-17 access log | `approval/ui/src/gxg_approval_ui/cli.py` | logging filter on `uvicorn.access` (stdlib `logging.Filter`); dropping the whole access log loses ops data |
| `max_attempts` | `schemas/approval-policy.schema.json` (+ kit copy, schema lock), `policy.py`, `intents.py` | optional field in `defaults` and rules, default 3, minimum over matched rules (as `execution_window`) |
| Deploy | `approval/deploy/compose.yaml` (`decider`, `admin`, networks), deploy tests | compose, as M9 |
| Runbooks | `docs/runbooks/approval-ops.md` | text |
| MC-05 (AC-M10-06) | `reports/conformance/M10-mc05.md` | ContextForge: registry tag list is the proof (no v1.2.x). FastMCP: re-run on 4.2.x in a scratch copy, pins unchanged |
| Stage C (AC-M10-07) | `reports/conformance/stage-C.json` | EX-01…24, MC-05 → test ids and runs |

## Design decisions (conservative defaults, `pending_owner_decision` where the spec is silent)

1. **AS-30 challenge.** `challenge = SHA-256(intent_id ‖ args_hash ‖ version ‖ decision) ‖ nonce(16 B)`. The nonce is a
   decider row (5 min, single use). Without it AS-16 freshness cannot be checked for a deterministic challenge and an
   assertion withheld by a compromised ui could be replayed later. `pending_owner_decision`.
2. **Who is the approver.** The decider takes the human from the credential row (`webauthn_credentials.human_principal`)
   and requires it to equal the human the ui names. ui → decider: internal network only (no service JWT: the ui has no
   kind in AS-06, and authority comes from the assertion, not from the caller). Recorded.
3. **Passkey enrollment outside ui.** One-time invite (`gxg-approval passkeys invite --human …` → admin API → hash in
   `passkey_invites`, 24 h). The human enters it on the passkey page; ui forwards it to the decider, which consumes
   it and stores the credential with `enrolled_by`. Credentials without `enrolled_by` (M9, written by ui) are refused.
   `pending_owner_decision` (the other AS-30 option, IdP re-login with `max_age`, needs the decider to reach the IdP).
4. **Every decision signed.** `gxg_decide` takes the assertion, stores it in `decisions.webauthn_assertion`,
   `step_up = true`. `EXECUTE gxg_decide` only for `gxg_decider`. T18 (manual reconcile) stays in ui (AS-30 names
   approve/reject).
5. **Webhook events.** A trigger on `intents` writes an outbox `webhook` event on every status change (T1 included)
   with `event_id`, `occurred_at`; `_gxg_outbox('webhook', …)` calls of 0001 become no-ops, so there is one source.
6. **Delivery.** notifier fans an outbox `webhook` row out to `webhook_deliveries` (one row per subscriber of the
   tenant in the current bundle) and delivers per subscriber strictly in id order (head of line): the oldest pending
   row first, so a subscriber back after 1 h gets every event in `version` order. Backoff 5 s · 2ⁿ up to 1 h; a row
   older than 24 h that fails → `dead`, next row. Secrets: `secret://<path>` → file `<GXG_WEBHOOK_SECRETS_DIR>/<path>`.
7. **Purge.** `gxg_purge` (sweeper): payload NULL 30 days after `terminal_at` (tenant override: `pending_owner_decision`,
   one global value now); delivered outbox rows deleted after 1 day (title/requester of `notify`); delivered/dead
   deliveries after 7 days; used/expired challenges and invites after 1 day. `outcome_unknown` is not terminal, so
   never purged.
8. **Metrics.** DB-derived metrics from `gxg_metrics()` (definer, aggregates only, labels tenant/tool/mode/outcome/
   scope), process counters (`gxg_quota_rejections_total`, `gxg_reserve_conflicts_total`), `gxg_bundle_age_seconds`;
   `/metrics` on `api` (+ executor's own port). `gxg_token_verify_failures_total{reason}` on the connector side
   (kit `exec_token` verify, demo `/metrics`).
9. **Operators.** `platform_admin` = list `GXG_ADMIN_OPERATORS` (public config of the admin process; the bundle has no
   operator role). `switch clear` needs an operator token with `auth_time` ≤ 5 min (step-up). `pending_owner_decision`.
10. **Alerts** go to Prometheus/Alertmanager (rules file); «канал оператора» is the Alertmanager receiver of the
    deployment. Every admin action is in `audit_events` and in `gxg_admin_actions_total{action}`.

## Not done, and why

- RET-02 (1-year metadata deletion), RET-03/04 (crypto-shredding, backups): not in AC-M10-03 («частично»); M14.
- Per-tenant retention: no field in the policy schema; one global 30 days (decision 7).
- `/healthz` of sweeper/mcp/executor (backlog M7/M8): M14 unless touched.
- ui → decider service JWT: decision 2.
