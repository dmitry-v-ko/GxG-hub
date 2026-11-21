# M2 cycle 1 — plan (ponytail ultra)

Input: `docs/implementation-plan-v1.md` (M2, AC-M2-01…06, DoD), `docs/specs/resource-grants-v1.md`
(GR-04…GR-11a), `docs/specs/approval-policy-v1.md` (AP-01, AP-02, «Lint в CI»),
`docs/specs/approval-service-v1.md` («Реестр платформы»), schemas `resource-grants`, `approval-policy`,
`platform-registry`, `gxg-manifest`. Base: `claude/m0-foundation` (6b3fe48) + GR-11a spec commit.

## Scope (every place the change reaches)

| Part | Files | Rung |
| --- | --- | --- |
| Loading YAML | `tools/gxg-policy/src/gxg_policy/load.py` | installed dep (PyYAML `SafeLoader` subclass): no timestamp conversion (`expires_at` stays a string, as the schema wants), duplicate keys rejected (PyYAML silently keeps the last one — a hidden grant) |
| `validate` | `load.py` | installed dep (`jsonschema` Draft 2020-12 + format checker) against the repo `schemas/`; file kind by directory: `tenants/`, `approval/`, `platform/` |
| `lint` | `lint.py` | minimum code: schema errors + cross-reference checks of GR-04/05/06/09, AP-02, «Lint в CI». Output `file: path: [code] message`, exit 1 on any error; warnings do not fail |
| `diff` | `diff.py` | minimum code: (tenant, subject, action, resource) pairs with groups expanded, expiry, human roles, approval rules by `id`, defaults/quotas, platform services and webhooks → `widening` / `narrowing`; JSON (machine) + Markdown (PR comment) |
| `build` / `verify` | `bundle.py` | JCS: stdlib `json` + key sort by UTF-16 code units (RFC 8785 §3.2.3); no floats, ints in the safe range. Ed25519: new dep `cryptography` (no Ed25519 in stdlib). Format exactly GR-11a |
| CLI | `cli.py`, `pyproject.toml` (`[project.scripts] gxg-policy`) | stdlib `argparse` |
| CI: policies + bundle smoke | `.github/workflows/ci.yml` job `policy` (in `ci-ok`) | validate, lint, build twice with a throw-away key → byte-compare without `built_at` → verify |
| CI: signed bundle after merge | `ci.yml` job `bundle` (only `push` to `main`, not in `ci-ok`) | key from secret `GXG_BUNDLE_SIGNING_KEY`, `kid` from variable `GXG_BUNDLE_KID`, artifact `grants-bundle` |
| CI: AC-M2-04 `grants-widening` | `ci.yml` job `grants-widening` (in `ci-ok`), `scripts/ci/widening_gate.py` | stdlib `urllib`; red while the diff has `widening` and the PR has no `widening-approved` label added by a CODEOWNERS user (CODEOWNERS read from the **base** commit, label events from the API, app-performed events rejected). On a new push with widening the label is removed — approval binds to what the owner saw. Posts the diff report as one PR comment (updated in place) |
| Tests | `tools/gxg-policy/tests/` (`bad/` 15 fixtures, `diff/` 6 pairs + golden reports, bundle, CLI), `tests/ci/test_widening_gate.py` | keys generated in tests |
| Docs | `docs/setup/widening-approval.md`, `docs/setup/bundle-signing.md`, `.github/CODEOWNERS` (+ `/policies/`, `/tools/gxg-policy/`) | text |
| Local CI mirror | `scripts/ci/check-all.sh` | one line per new step |
| Report | `reports/conformance/M2.json` | — |

## Decisions where the spec leaves room (no spec change)

- **Groups in the bundle.** GR-11a: grants «с развёрнутыми группами». `subjects` get group members inline;
  the `groups` map stays, because approval policies reference groups in `requesters`/`approvers`.
- **Platform file name.** Spec names `policies/platform/registry.yaml`; the repo ships `registry.example.yaml`.
  Tooling takes the single `*.yaml` in `platform/` (more than one → lint error).
- **Tenant file name.** Spec: `<tenant_id>.yaml`; repo example is `example.yaml` with `tenant: t-alpha`.
  The tenant comes from the file content; file name is not checked (would fail AC-M2-01 on the examples).
- **Foreign principal (GR-05).** Detected by the tenant segment of `agent:<tenant>:<name>` (the format of
  every example; the spec marks the exact format VERIFY after the ContextForge spike).
- **Tool effect class (AP-02).** From connector manifests (`connectors/*/gxg-manifest.yaml`,
  `schemas/examples/*.gxg-manifest.yaml`, or `--manifest`). Unknown tool in an `auto_approve` rule → warning.
- **Signing key format.** Secret = base64url raw 32-byte Ed25519 seed (no PEM: the secret scanner forbids
  key headers and a seed is all Ed25519 needs). Public key in `verify` uses the registry form `ed25519:<b64url>`.

## Not done, on purpose

- Bundle distribution to services (GR-12/13) — M1 loader and later milestones; CI only uploads an artifact.
- GR-18 ContextForge drift check (SHOULD, needs M4/M5).
- GR-06 expiry warnings use the run time; no extra CLI flag for "now".
- No `keygen` subcommand: tests and the CI smoke generate keys inline; the owner's one-off command is in the doc.
- A PR can still edit `.github/workflows/ci.yml` and remove the gate (a `pull_request` workflow runs the PR's
  own copy). GitHub can't make a check from `pull_request_target` count reliably as required, and required
  workflows need an organization ruleset. Recorded as a limitation in `docs/setup/widening-approval.md`.
