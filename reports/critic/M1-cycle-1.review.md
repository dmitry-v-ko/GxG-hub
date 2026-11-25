# M1 cycle 1 — ponytail-review

Diff: `6b3fe48..52874f5` (branch `claude/m1-connector-kit`). Specs taken from `claude/admiring-planck-vzys0x` (AS-06a, GR-13a included).
Assumed load: a few connectors and services, low call rate (no load figures in the specs). Execution tokens are rare (one per approved effect).

**What this change does:** It adds the shared connector library `gxg-connector-kit`: canonical JSON hash, the error envelope, verifiers for execution tokens and service JWTs (PyJWT + Ed25519), the signed grants bundle loader with `GrantResolver`, a test-only identity stub, log redaction and a readiness result. CI gets a per-module branch coverage gate.

How I checked: clean clone at `52874f5`, empty uv cache, Python 3.12.13, `scripts/ci/check-all.sh` → `check-all: OK` (356 passed, 0 skipped; branch coverage 100 % in each of the 6 modules). GitHub Actions run 44793665616 on `52874f5`: all jobs including `ci-ok` = success. 51 single-condition mutations plus 3 extra variants on the `jti` cache (list at the end). RFC 8037 thumbprint vector reproduced. Probes of bundle `built_at` behaviour.

## Must fix

None found. No bug that produces a wrong allow, a forged token acceptance or a leak in the code as written.

## Should fix

1. **The replay cache can be broken without any test noticing** (`packages/gxg-connector-kit/src/gxg_connector_kit/_jwt.py:L169`, tests `test_exec_token.py:L172-177`, `test_svc_jwt.py`)
   - **What this is:** `JtiCache` remembers each seen `jti` until `exp + 5 s`, so a captured token cannot be used a second time while it is still valid. This is the replay guard of EFF-18 п.1 and, for service JWTs (AS-06), the only one.
   - **Problem:** The replay tests send the second copy at the same clock instant. I changed the remembered lifetime from `exp + SKEW_S` to `now + 1` (forget after one second) and to `exp` (no skew): all 253 kit tests still pass. With `now + 1`, the same token replayed 2 s later is accepted although it is valid for 58 more seconds. The code today is right; nothing protects it. The orchestrator listed the `jti` cache among the key conditions to mutate, so this breaks DoD-4.
   - **Fix:** One test per verifier (or one in `_jwt` used by both): accept a token at `T0`, move the clock to `T0 + 30` and to `exp + 5`, verify the same token again → `replay`. About 10 lines.
   - **If we skip it:** A refactor of the cache (e.g. switching to a TTL helper) can silently open a replay window for service JWTs.

2. **A bundle with `built_at` in the future freezes grants and stays "ready"** (`grants.py:L168-170`, `L224`, `L231-239`)
   - **What this is:** The loader rejects any bundle older than the loaded one (GR-13a, anti-rollback) and marks the service not ready when the bundle is older than 24 h (GR-13).
   - **Problem:** `built_at` is never compared with the clock. Probe: load a correctly signed bundle with `built_at = 2036-01-01T00:00:00Z` → accepted, `readiness().ready == True`. Then load a correct bundle built now → rejected as `rollback`. From then on every new bundle (with revocations) is rejected until restart, while `/readyz` stays green, because the age is negative. One CI clock error or a builder bug (local time written with `Z`) is enough. GR-13 (new bundle reaches every service within 5 minutes, stale bundle → not ready) silently stops holding.
   - **Fix:** In `load_bundle`, reject `built_at > now + 5 s` (AS-11 skew) with `BundleError`; one test that a future bundle is rejected and does not block the next correct one. About 4 lines of code plus the test (needs the clock passed to `load_bundle` or the check in `BundleStore.load`).
   - **If we skip it:** Revoked grants keep working on a service that reports itself healthy.

3. **The 5 s skew boundary is not pinned** (`_jwt.py:L163-166`)
   - **What this is:** AS-11 sets the tolerance at exactly 5 s for `iat` and `exp`.
   - **Problem:** Tests check 4 s (accepted) and more than 5 s (rejected). Changing the tolerance to 4 s (`now > exp + SKEW_S - 1`, `iat > now + SKEW_S - 1`) survives the whole suite. With integer timestamps the case "exactly 5 s" is the only one that tells the two apart, and it is not tested. The orchestrator listed "`exp` с допуском" as a key condition.
   - **Fix:** Two cases: `exp = now - 5` accepted, `iat = now + 5` accepted (for exec token and service JWT). Four lines.
   - **If we skip it:** Clock skew handling drifts from AS-11 without a red test; tighter is safe but produces spurious `unauthenticated` on NTP drift.

4. **Mutation claim in the conformance report is not reproducible** (`reports/conformance/M1.json`, DoD-4)
   - **What this is:** The report says 37 of 38 mutations are caught and the only survivor is equivalent.
   - **Problem:** My sweep finds 4 non-equivalent survivors (findings 1 and 3: `jti` lifetime ×2, `exp` and `iat` tolerance). The list of mutations is not in the repository, so the claim cannot be re-run.
   - **Fix:** After 1 and 3, record the mutation list (name, file, replacement, caught) in the report, or commit the small script.
   - **If we skip it:** The next critic cannot tell which conditions are guarded.

## Nice to have

5. **RID regex accepts a trailing newline** (`grants.py:L33-34`, `L272`). `$` in Python also matches before a final `\n`: `check(..., "kb:kb-b/collection/x\n")` is allowed by a mask. RIDs come from backend data (GR-01), so this is not an exploit today. Fix: `fullmatch` and drop `^…$`. One line per regex.
6. **`GxgError.to_dict()` can produce a body that violates `error.schema.json`** (`errors.py:L197-229`). `GxgError("rate_limited", "x", retry_after_s=90000).to_dict()` fails the schema (`maximum: 86400`); same for `retry_after_s=-1` or `details` with 21 keys. Fix: two `ValueError` checks in `__init__`, like the existing `outcome_unknown` one.
7. **No readiness signal for the executor JWKS** (`exec_token.py:L161-187`). MCC-09 wants `/readyz` to fail when JWKS is not reachable. `ExecutorJwks` drops keys on a failed refresh (correct, fail closed) but has no `readiness()`, so a connector built on the kit reports ready while every `execute` gets `unauthenticated`. Fix: `readiness()` returning `Readiness(bool(self._keys) and fresh)`; can wait for M3/M8, which are the first callers.
8. **Lean:** `_jwt.format_public_key` is used only by tests (move it to `tests/kit_testing.py`, −3 lines). `Service.kind` / `internal_url` are parsed but have no reader yet (keep only if M7 needs them soon, −2 lines).

## Checked and fine

- `alg: none`, `HS256`, HMAC signature under an `EdDSA` header, `typ`, missing claims (each of 13), `bool`/float in `iat`/`exp`, `aud` as list: all rejected; mutations caught. The header `alg` check runs before any key lookup; PyJWT is limited to `EdDSA` with an `Ed25519PublicKey`, so key/algorithm confusion is not possible.
- Constant-time comparison: there is no shared secret anywhere (no HMAC); `kid`, `iss`, `aud`, `jti` are public values, signatures are checked by `cryptography`. Nothing needs a constant-time compare.
- base64url with non-zero trailing bits: safe. Two spellings decode to the same bytes, Ed25519 verification in OpenSSL is over those bytes and rejects non-canonical `S`, and no code uses the encoded string as an identity (thumbprints are recomputed from raw bytes). Padding, `+`/`/`, and length ≡ 1 mod 4 are rejected.
- AS-06a: `kid` = RFC 7638 thumbprint; RFC 8037 vector `kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k` reproduced; mismatch rejected (mutation caught).
- GR-11a format, signature over exact bytes, keys from configuration, foreign key / unknown `kid` / wrong `alg` / changed byte → `BundleError` and not ready. GR-13a anti-rollback: `built_at` older than loaded → rejected (mutation caught); equal `built_at` accepted, which matches "старше".
- Fail closed: no bundle or stale → `temporarily_unavailable`; failed JWKS refresh → no key trusted; stub identity refuses to construct without `GXG_PROFILE=test` (mutation caught); no production factory exists.
- GrantResolver: mask limited to one type, foreign connection/domain, foreign tenant, principal not listed, expiry at the boundary, action not granted — all mutations caught. The "unknown group" mutation is equivalent (the unlisted-principal check denies it).
- Canonical: 6 vectors, 2 invalid inputs, 3 × 1000 property examples; NFC, UTF-16 key order, float and range mutations caught.

Verdict: fix 1, 2, 3 and 4 first.
Lean: -5 lines possible.
Not checked: behaviour against a real M2-built bundle (does not exist yet); PyJWT internals beyond the pinned 2.15.1.

## Mutation sweep (52874f5, `pytest packages/gxg-connector-kit/tests`)

Caught (48): alg header, aud, iss, exp tolerance +1, exp check off, iat tolerance +1, ttl, jti check off, jti prune, required str type, int-vs-bool, typ, b64url strictness, svc kid, svc unregistered iss, stub kid, stub profile, stub principal, JWKS kid, JWKS fail-closed, JWKS age, bundle signature, bundle alg, rollback, staleness in readiness, staleness in `current`, UTC `built_at`, mask type, foreign connection (2 variants), expiry off, expiry `<=`, principal listed, action, `allowed` prefix, `allowed` connection, tenant slice, other tenant's file, redact case, redact JWT, redact lists, canonical sort, NFC, float, int range, `outcome_unknown`, `all_ready`; jti lifetime = `iat + 1`.

Survived (non-equivalent, 5): exp tolerance −1, iat tolerance −1, jti lifetime `exp − 30`, jti lifetime `exp`, jti lifetime `now + 1`.

Survived (equivalent, 1): unknown group kept as literal subject (denied by the principal check).
