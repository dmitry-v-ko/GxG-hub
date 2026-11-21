# M9 cycle 2 — plan (ponytail full)

Input: `reports/critic/M9-cycle-1.json` (score 6.0; caps critical, DoD, major). Its `required_improvements` are the
closed checklist of this cycle; its `threat_model` is the attack-path list. Base: `2c9557c`.

| # | Checklist item | Change | Rung |
| --- | --- | --- | --- |
| F1 | bot token in httpx INFO logs | `cli.setup_logging()` in notifier and ui: `httpx`, `httpx2`, `httpcore` loggers at WARNING; test that a `Bot.call` under that setup leaves no record with the token (and that without it httpx does log the URL, so the test can fail); README runbook: rotate the token if older logs were shipped | stdlib `logging` |
| F2 | `rstrip("/")` mutants not equivalent | `removesuffix("/")` in `app.py` and `bot.py`; both lines out of `mutmut-equivalent.txt` | stdlib |
| F3 | `ui cli.main` untested, not mutated | tests with `uvicorn.run` and `create_app` patched: non-loopback without TLS, http public URL / issuer → `SystemExit`; loopback without TLS and with TLS → the arguments `uvicorn.run` gets; every variable reaches `Deps`; `cli.py` in `only_mutate`; `mutmut.sh` comment exact | monkeypatch |
| F4 | one bad preview → every list 503 | `summary()` shows «превью недоступно» for that row; card keeps 503; test with one unopenable row among good ones | — |
| F5 | step-up single-use claim | docstring and README: one vote per human is the DB's, a replay inside 5 min gives no new decision; test with a zero-counter authenticator: replay → exactly 409, one decision | — |
| F6 | bundle loader duplicated | `gxg_connector_kit.grants.bundle_store_from_env()` (load + 60 s reload thread), used by `approval/config.py` and `ui/cli.py`; kit tests (100 % branch gate, kit mutmut) | existing kit module |
| F7 | session shortcut not marked | `shortcut:` comment at `SessionMiddleware` and `/logout` | — |
| F9 | `version` ≥ 2³¹ → 500 | `0 < version < 2**31` else 400 (decision and resolve); test | — |
| F14 | link for several tenants untested | notifier test: code for t-alpha and t-beta → two links, notifications of both tenants reach the chat; SQL mutation «link only the first tenant» in `mutate_m7_sql.py` | — |
| F13 | `ci_local` running | full `scripts/ci/check-all.sh` on the cycle-2 code commit, result in `reports/conformance/M9.json` | — |

Backlog (`docs/backlog.md`, M9): F8 passkey registration without role / re-auth (AS-30, M10), F10 markers in Telegram
text, F11 single-host IdP relay, F12 notifier liveness vs AS-29, notifier can mark any outbox row delivered.

Not done: spec, AC, rubric, threshold unchanged.
