# M9 cycle 1 — ponytail-review of the own diff (`3c2f0b0..HEAD`), before the critic

Load assumed (approval-service-v1 «Развёртывание»): one instance of each process, a handful of approvers per
tenant, tens to hundreds of intents a day; one notifier (Telegram allows one `getUpdates` consumer per token).

What this change does: adds the Web UI (`approval/ui`, OIDC sign-in, decisions with CSRF, version and args hash,
WebAuthn step-up, manual reconcile, Telegram link code) and the notify bot (`approval/notifier`, outbox → Telegram,
long polling, AP-08 digest) as separate images with their own lockfiles; narrows what the `gxg_ui` role can do and
makes `gxg_decide` check the approver set frozen at T1; compose networks per AS-29 with a container test.

## Found and fixed in this pass

1. **The refused-claim count scanned the whole audit table** (`0002_ui_notifier.py`, `gxg_link_claim`)
   - What this is: every `/link` counts the sender's refused claims of the last 10 minutes in `audit_events`.
   - Problem: no index on `audit_events`; after a year (RET-02) each claim is a sequential scan of all audit rows.
   - Fix: partial index `audit_events (at) WHERE action = 'telegram_link_refused'` + a plan test.
2. **A preview key that does not open the ciphertext crashed the card** (`app.py`, `envelope`)
   - Problem: `nacl` `CryptoError` → 500 with a stack trace in the log.
   - Fix: → 503 «Сервис временно недоступен» + test.

## Left as decisions (not findings)

- `shortcut:` AP-08 window in memory (one bot instance, restart forgets ≤ 5 hints) — `bot.py`.
- `shortcut:` signed-cookie session, no server-side revocation — plan, README runbook (rotate the session key).
- A send that fails is not retried (EFF-14, at most one notification per intent); a crash between send and commit
  sends that one notification again (at-least-once, still ≤ 1 per intent per successful pass).
- Passkey registration needs only a valid session (no re-authentication): a stolen session cookie (HttpOnly,
  `__Host-`, Strict) could add a passkey. Recorded in the threat model; part of the owner proposal (M9-ESCALATION §1).

Verdict: Ship (after 1 and 2, fixed).
Not checked: the `egress` profile relays against the real IdP/Telegram (forbidden here); GitHub CI runners.
