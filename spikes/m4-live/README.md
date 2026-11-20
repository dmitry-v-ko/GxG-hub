# M4 live spike — ContextForge identity propagation (ADR-0001, option A)

Local-only stand: ContextForge v1.1.1 (pinned digest) + PostgreSQL 17.6 + an echo MCP server that
logs every header and `_meta` it receives. Results: [`reports/conformance/M4-live.json`](../../reports/conformance/M4-live.json).

## Reproduce

```bash
cd spikes/m4-live
./scripts/gen-env.sh                 # random secrets -> .env (gitignored)
: > evidence/echo.jsonl
docker compose up -d                 # wait for: curl -s localhost:4444/health
python3 scripts/setup.py             # 2 teams x 2 agents, 2 echo gateways per team (one aliased), 1 virtual server per team
python3 scripts/attacks.py           # call paths + forgery + bypass + replay (>60 s); writes evidence/results.json
docker compose down
```

`secrets/state.json` (tokens, passwords) is gitignored. The image digest is for linux/arm64; on amd64 set
`CONTEXTFORGE_IMAGE` in `.env` to the amd64 digest of `ghcr.io/ibm/mcp-context-forge:v1.1.1`.

## Layout

| Path | What |
| --- | --- |
| `compose.yaml` | stand; echo is published on `127.0.0.1:9100` on purpose, for the bypass/replay attacks |
| `echo/echo_server.py` | stdlib streamable-HTTP MCP server, JSON responses, logs to `evidence/echo.jsonl` (Authorization/Cookie redacted) |
| `scripts/setup.py` | creates teams, agents (users + team-scoped API tokens), gateways, alias (`custom_name`), virtual servers |
| `scripts/attacks.py` | runs every case, correlates by nonce with the echo log, applies the best option-A verifier (`HMAC(id:email)` + single team) |
| `evidence/` | raw echo log, per-case results, ContextForge denial log lines |

## Option C prototype (identity adapter plugin)

Results: [`reports/conformance/M4-live-C.json`](../../reports/conformance/M4-live-C.json).

```bash
cd spikes/m4-live
./scripts/gen-env.sh && ./scripts/gen-keys.sh   # Ed25519 pair -> secrets/ (gitignored)
./scripts/run_c.sh                              # fresh v1.1.1, fresh v1.1.0 -> upgrade, fail-closed checks
```

| Path | What |
| --- | --- |
| `compose.c.yaml` | overlay: option A off, adapter plugin on (`PLUGINS_FAIL_ON_PLUGIN_ERROR=true`), echo verifies online |
| `adapter/adapter.py` | `tool_pre_invoke` plugin: adds `X-GXG-Identity` (EdDSA JWS: iss, aud, sub, tenant_id, iat, exp 60 s, jti, ahs) |
| `echo/gxg_verify.py` | connector-side verifier (prototype of `IdentityVerifier`) |
| `scripts/attacks_c.py` | paths, forgery, bypass, replay against option C; `evidence/results-c-<label>.json` |
| `compose.c-broken.yaml`, `adapter/plugins-broken.yaml` | adapter that cannot load, for the fail-closed check |
| `scripts/redact_jws.py` | replaces JWS values in committed evidence; raw tokens stay in `evidence/raw/` (gitignored) |
