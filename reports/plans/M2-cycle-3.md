# M2 cycle 3 — plan (ponytail full)

Input: critic `reports/critic/M2-cycle-2.json` (score 5.0, two critical) and `.review.md`; base
`origin/claude/integration` merged (stage A audit: GR-11a `tools`, AS-26, AS-06 `public_keys`,
`grants-bundle.schema.json`, `bundle-signature.json`, non-empty `limits` for `auto_approve`).
Spec, plan and acceptance criteria are not changed. I agree with every critic item; none is disputed.

## Scope

| Item | Files | Rung |
| --- | --- | --- |
| Base change (critical 1) | `scripts/ci/grants_widening.sh`, `ci.yml` | minimum code: every non-`main` run diffs `merge-base(refs/remotes/origin/main, H)..H`, H = PR head SHA (`pull_request`) or `HEAD`; the event base is not used. CODEOWNERS from `refs/remotes/origin/main`. `edited` added to `pull_request` types |
| F2 rest (critical 2) | `scripts/ci/widening_gate.py` | ~5 lines: arrival = earliest `pull_request` run for the head SHA created **after** the latest `head_ref_force_pushed`; runs whose `head_repository.id` ≠ PR head repo id are ignored |
| Tag shadowing (major 3) | `grants_widening.sh` | full ref `refs/remotes/origin/main` (same fix as 1) |
| Tests for 1–3 | `tests/ci/test_grants_widening_job.py`, `tests/ci/test_widening_gate.py` | PR with non-main base → red; tag `origin/main` → red; A/B/A/B fast-forward → red; fork run ignored |
| DoD-4 mutmut (major 4) | `pyproject.toml`, `tools/gxg-policy/pyproject.toml` (`[tool.mutmut]`), `scripts/ci/mutmut.sh`, tests, `reports/mutations/M2-cycle-3-mutmut.txt` | installed tool (mutmut 3.8.1, dev group). Two configs because mutmut 3 maps file path → module name only for `./`, `src/`: `gxg_policy` runs from `tools/gxg-policy`, the gate from the root. One command runs both and fails on any survivor not listed as equivalent |
| Custom mutations only for bash/YAML | `scripts/ci/mutate_m2.py` → keeps only `grants_widening.sh` and `ci.yml` entries | see «DoD-6» |
| Docs (minor 5) | `docs/setup/widening-approval.md` | text: the false claim becomes true after 1–3; checklist: base change, A/B/A/B, tag + dispatch, both orders of same-name `ci-ok` |
| GR-11a `tools` | `gxg_policy/bundle.py`, `lint.py` | minimum code: `build` sets `tools` of every `kind: connector` service from `connectors/<id>/gxg-manifest.yaml` (`connector` = service `id`), projected to `name, version, effect_class, action, executes_via, dispatch_timeout_s, preview`; no manifest → `[]` |
| Manifests read once | `lint.py` | manifests are validated by `gxg-manifest.schema.json` (finding with file and path, not a crash); the same set feeds AP-02 classes in `lint` and `tools` in `build`. Default set = `connectors/*/gxg-manifest.yaml` (what the bundle carries); `schemas/examples/` no longer counts, so AP-02 checks exactly the classes AS-26 enforces |
| Bundle schema | `bundle.py` | installed dependency `jsonschema` + `referencing` (already a jsonschema dependency): `build` and `verify` validate the document by `grants-bundle.schema.json` |
| `public_keys` | fixtures (36 `registry.yaml`) | data only: `public_key: X` → `public_keys: [X]` |
| Signature vector | `tools/gxg-policy/tests/test_bundle.py` | test: policies + manifest that produce the vector's bundle → `build` bytes == `bundle_bytes_b64`, `.sig` == `signature_file`; `verify` accepts it with the vector key |
| `limits: {}` | `tests/bad/20-auto-approve-empty-limits` | fixture only (schema already rejects it) |
| Conformance | `reports/conformance/M2.json` | `proven_by_tests` and `owner_pending` split per case |

## DoD-6: ready solutions considered for code I wrote

| Component | Considered | Decision |
| --- | --- | --- |
| Diff base for PRs (`grants_widening.sh`) | GitHub `base.sha`; `pull_request.base.ref` check; `actions/checkout` merge commit | plain `git merge-base` with the full remote ref: one rule for every event, no trust in the event base |
| Head arrival / staleness (`widening_gate.py`) | GitHub environments with required reviewers (paid plan for private repos, not the label AC-M2-04 prescribes); Code Owner review (excluded, AC-M0-03); `head_ref_force_pushed.commit_id` (not documented as the new head) | keep the custom ~10-line rule on run `created_at` + force-push events |
| Mutation testing, Python | **mutmut** (DoD-4) | used for `gxg_policy/{diff,lint,bundle,load}.py` and `widening_gate.py` |
| Mutation testing, `grants_widening.sh`, `ci.yml` | mutmut (Python only); `mutation-testing` tools for bash: none maintained on PyPI/npm/apt that mutate shell; YAML: none for GitHub workflow semantics | keep `mutate_m2.py`, reduced to those two files; reason written here |
| Manifest → `tools` | `jsonschema` for shape (used); no tool knows GR-11a projection | 10 lines, dict comprehension |
| Bundle schema validation | `jsonschema` + `referencing.Registry` (installed) | used, no custom checks beyond the existing exact-bytes JCS check |
| JCS | `rfc8785` (PyPI) | unchanged from cycle 1 (stdlib; the new vector reproduces byte for byte, test) |
| Ed25519 | `cryptography` (used) | unchanged |
| Test-vector seed | `hashlib.sha256` (stdlib) | used |
| `mutmut.sh` | `mutmut run` alone (one config per cwd) | a 15-line wrapper only to run the two configs and fail on survivors; mutmut exits 0 with survivors |

## Not done, on purpose

- The secrets job also uses `pull_request.base.sha`; it is being replaced by gitleaks in M0.1. Not touched here.
- Hand-written `tools` in `policies/platform/registry.example.yaml` stay (integration data); `build` overwrites them from manifests, `lint` warns that they are ignored.
- Which of two same-name `ci-ok` checks branch protection counts: needs a real PR (owner checklist).
- A PR can still change the gate itself (`ci.yml`, gate scripts, `tools/gxg-policy/src/**`, lockfiles): known limit, unchanged.
