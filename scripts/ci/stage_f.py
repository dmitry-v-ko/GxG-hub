#!/usr/bin/env python3
"""AC-M14-06: reports/conformance/stage-F.json — every id of the Conformance Matrix (SC, EX, MC, SI, OPS) with the
tests that prove it and its status in this run. The ids are read from docs/specs/conformance-matrix-v1.md; an id
without a mapping here (or a mapping for an id that is not in the matrix) fails the script. Reuses stage_c.py: its EX
mapping, its JUnit reader, its MC-05 evidence.

    uv run python scripts/ci/stage_f.py --stand stand/secrets/junit/{fresh,after-restart,after-recreate}.xml

Runs the mapped non-stand tests once (`pytest --junitxml`; Docker or GXG_TEST_PG_SOCKET_DIR for the DB tests) and
reads the stand JUnit files of `stand/run.sh` for the `stand/tests/...` ones. Statuses:

- `passed`: every mapped test ran and passed (a parametrized test: every case; every stand JUnit file given);
- `failed`: a mapped test failed, or a non-stand test did not run;
- `not_run`: a stand test has no result (no `--stand` JUnit), or the id is open on purpose (no test in this
  repository, see OPEN);
- `manual`: needs a live or human step; `related` lists tests that cover a part, they do not make it `passed`.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "docs/specs/conformance-matrix-v1.md"


def _load_stage_c() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stage_c", ROOT / "scripts/ci/stage_c.py")
    if spec is None or spec.loader is None:
        raise ImportError("scripts/ci/stage_c.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load_stage_c()
A, UI, DEMO = C.A, C.UI, C.DEMO
S, ST, SM = "stand/tests/test_sc.py", "stand/tests/test_telegram.py", "stand/tests/test_mc.py"
DEP = "tests/deploy"

# SC and MC: the stand tests (through ContextForge, `stand/run.sh`, fresh / after-restart / after-recreate) as in
# reports/conformance/M5.json and M11.json, plus the stand tests of the Telegram connector.
SC: dict[str, list[str]] = {
    "SC-01": [
        f"{S}::test_sc01_each_agent_lists_only_its_team_tools",
        f"{S}::test_sc01_other_team_tool_is_refused_by_name_on_every_path",
        f"{S}::test_sc01_hidden_tool_and_default_deny_on_every_path",
        f"{S}::test_sc01_alias_and_original_name",
        f"{S}::test_sc01_global_mcp_refuses_agents",
        f"{ST}::test_sc01_other_team_telegram_tools_are_refused_by_name",
    ],
    "SC-02": [
        f"{S}::test_sc02_anonymous_expired_and_foreign_tokens",
        f"{S}::test_sc02_agents_cannot_log_in_with_a_password",
    ],
    "SC-03": [
        f"{S}::test_sc03_identity_in_arguments_headers_and_meta_changes_nothing",
        f"{ST}::test_sc03_identity_in_arguments_headers_and_meta_changes_nothing",
    ],
    "SC-04": [
        f"{S}::test_sc04_bad_identity_is_refused_by_the_connector_before_the_backend",
        f"{ST}::test_sc04_bad_identity_is_refused_by_the_connector_before_the_service",
    ],
    "SC-05": [
        f"{S}::test_sc05_other_tenant_and_hidden_note_are_byte_identical_to_missing",
        f"{ST}::test_sc05_and_ac_m11_03_unauthorized_chat_is_not_found_and_never_reaches_the_service",
    ],
    "SC-06": [
        f"{S}::test_sc06_agent_host_cannot_connect_to_demo",
        f"{S}::test_sc06_inside_the_network_without_identity_is_unauthenticated",
        f"{ST}::test_sc06_agent_network_cannot_reach_telegram_and_inside_without_identity_is_unauthenticated",
    ],
    "SC-07": [
        f"{S}::test_sc07_ssrf_registration_urls_are_refused",
        f"{S}::test_sc07_redirects_are_not_followed",
        f"{DEP}/test_ssrf_config.py::test_protection_is_on_and_the_switches_that_open_it_are_off",
        f"{DEP}/test_ssrf_config.py::test_no_blocked_address_is_inside_the_allowed_networks",
        f"{DEP}/test_ssrf_config.py::test_the_allowed_networks_are_only_railways_private_ranges",
    ],
    "SC-08": [
        f"{S}::test_sc08_prompt_injection_is_data",
        f"{ST}::test_sc08_injection_is_data_through_contextforge",
    ],
    "SC-09": [
        f"{S}::test_sc09_no_secrets_tokens_or_texts_in_replies_and_logs",
        f"{ST}::test_sc09_and_ac_m11_05_1000_calls_leave_no_texts_peer_ids_or_secrets_in_logs",
    ],
    "SC-10": [
        f"{S}::test_sc10_partial_grants_without_hidden_counts",
        f"{S}::test_sc10_expired_grant_gives_nothing",
        f"{S}::test_sc10_invalid_rolled_back_or_stale_bundle_means_not_ready_and_refused",
        f"{ST}::test_sc10_search_returns_only_granted_chats_without_hidden_counts",
        f"{ST}::test_sc10_nothing_granted_and_bad_bundle_are_refused_before_the_service",
    ],
}
MC: dict[str, list[str]] = {
    "MC-01": [
        f"{SM}::test_mc01_negotiate_discover_invoke_with_the_official_sdk",
        f"{SM}::test_mc01_rpc_lists_the_same_schemas",
        f"{ST}::test_mc01_contextforge_lists_the_connectors_schemas",
    ],
    "MC-02": [
        f"{SM}::test_mc02_search_schema_pagination_and_limits",
        f"{SM}::test_mc02_valid_empty_result_does_not_fall_back",
        f"{SM}::test_mc02_index_failure_falls_back_to_scan",
        f"{ST}::test_mc02_search_schema_pagination_cursor_and_limits",
        f"{ST}::test_mc02_thread_and_collect_page_to_the_end",
    ],
    "MC-03": [
        f"{SM}::test_mc03_backend_errors_keep_their_codes_and_retryable_through_contextforge",
        f"{SM}::test_mc03_timeout_after_the_request_was_sent",
        f"{SM}::test_mc03_connector_down_is_an_error_not_a_result",
        f"{ST}::test_mc03_archive_down_is_temporarily_unavailable_through_contextforge",
        f"{ST}::test_mc03_timeout_after_the_request_was_sent",
        f"{ST}::test_mc03_connector_down_is_an_error_not_a_result",
    ],
    "MC-04": [
        f"{SM}::test_mc04_revoked_agent_token_stops_working_on_every_path",
        f"{SM}::test_mc04_adapter_key_rotation_and_revocation_at_the_connector",
    ],
}
SI_EVIDENCE = "reports/conformance/M12-si.md"  # SI-01…03: tests of gxg_agents_v2 (M12), re-run for stage F
MC05_EVIDENCE = "reports/conformance/M10-mc05.md"  # as in stage C: the re-run on the next minors, `status: passed`

OPS: dict[str, list[str]] = {
    # restart of the services with intents in flight: executor death between reserve and dispatch / after dispatch,
    # the sweeper after a DB error, and the restore onto a clean database with no second provider call
    "OPS-01": [
        f"{A}/test_executor.py::test_ac07_connector_killed_after_dispatch",
        f"{A}/test_executor.py::test_ac09_executor_dies_between_reserve_and_dispatch",
        f"{A}/test_executor.py::test_ac07_t14_when_the_executor_dies_too",
        f"{A}/test_sweeper.py::test_one_pass_handles_all_four_deadlines",
        f"{A}/test_sweeper.py::test_reconnects_after_a_database_error",
        f"{A}/test_backup.py::test_restore_keeps_intent_states_and_the_executor_calls_the_provider_no_second_time",
    ],
    "OPS-03": [
        f"{A}/test_backup.py::test_restore_keeps_intent_states_and_the_executor_calls_the_provider_no_second_time",
        f"{A}/test_backup.py::test_restore_refuses_a_database_that_is_not_clean",
        f"{DEP}/test_approval_backup.py::test_roundtrip_over_several_chunks_and_nothing_readable_in_the_file",
        f"{DEP}/test_approval_backup.py::test_damage_truncation_reordering_and_a_wrong_key_are_refused",
    ],
    "OPS-04": [
        f"{A}/test_backup.py::test_restore_keeps_intent_states_and_the_executor_calls_the_provider_no_second_time",
        f"{A}/test_purge.py::test_terminal_31_days_ago_is_purged_metadata_stays",
        f"{A}/test_purge.py::test_sweeper_purges_hourly",
        f"{DEP}/test_approval_backup.py::test_prune_goes_by_the_name_not_the_mtime_and_spares_other_files",
        f"{DEP}/test_approval_backup.py::test_check_fails_on_an_old_backup_and_passes_without",
    ],
    "OPS-05": [
        f"{A}/test_roles.py::test_table_and_column_privileges_match_the_matrix",
        f"{A}/test_roles.py::test_statements_as_each_role",
    ],
    "OPS-07": [
        f"{S}::test_ops07_tailscale_headers_are_not_an_identity",
        f"{A}/test_tailnet_identity.py::test_forged_tailscale_headers_never_authenticate",
        f"{A}/test_tailnet_identity.py::test_a_valid_token_still_works_so_the_refusals_above_are_about_the_credential",
        "approval/tests/test_admin_web.py::test_tailscale_headers_are_not_an_operator",
    ],
}

# Ids that stay open on purpose: no test in this repository proves them, or they need a live / human step. `status`
# is never `passed`; `related` are the tests that cover a part (listed in the report, they decide nothing).
OPEN: dict[str, dict[str, Any]] = {
    "OPS-02": {
        "status": "not_run",
        "reason": "immediate effect is tested (policy: next propose; revocation: stand MC-04; key rotation: EX-24) "
        "and the audit of a policy switch (GR-14: propose audit carries policy_version) and of a key rotate/remove "
        "(SEC-09: key_rotated/key_removed with the JWKS hash before/after) is tested; not covered: an agent-token "
        "revocation is ContextForge's own record (no Hub audit_events row), and the weakening alert is a red CI "
        "gate on the PR, not a Prometheus rule; no alert on a rotation/revocation. A drill is needed",
        "related": [
            f"{A}/test_propose.py::test_a_new_bundle_applies_to_the_very_next_propose_and_names_its_version",
            f"{A}/test_propose.py::test_policy_deny_stores_nothing",
            "tests/ci/test_grants_widening_job.py::test_branch_with_widening_is_red_without_a_pr",
            "tests/ci/test_widening_gate.py::test_run_fails_without_label_and_updates_own_comment",
            "tools/gxg-policy/tests/test_diff.py::test_direct_rule_edits",
            f"{A}/test_executor.py::test_ac11_propose_grant_revoked_between_approve_and_execute",
            f"{A}/test_executor.py::test_ac14_rotation_without_downtime_and_removal_of_a_compromised_kid",
            "approval/tests/test_keys.py::test_rotate_publishes_first_and_activates_15_minutes_later",
            "approval/tests/test_keys.py::test_rotate_audits_kid_and_the_jwks_hash_before_and_after",
            "approval/tests/test_keys.py::test_remove_audits_kid_and_the_jwks_hash_before_and_after",
            "approval/tests/test_keys.py::test_nothing_is_written_when_the_audit_row_cannot_be_written",
            f"{A}/test_executor.py::test_keys_cli_rotate_remove_and_release",
            f"{A}/test_executor.py::test_keys_remove_of_a_compromised_key_works_with_the_db_down",
            f"{A}/test_propose.py::test_human_approval_by_default_for_external_effect",
            f"{SM}::test_mc04_revoked_agent_token_stops_working_on_every_path",
            "approval/deploy/tests/test_alert_rules.py::test_rule_unit_tests_pass",
        ],
    },
    "OPS-06": {
        "status": "manual",
        "reason": "the policy is live (reports/M14-tailnet-acl-live.md: saved via the Tailscale API, its `tests` "
        "ran on save: tag:agent reaches only tag:gateway:443, Hub nodes reach nothing on each other); left: probes "
        "from inside a Hub node (needs an SSH key on Railway) and the operator's own device opening the UI and :8443",
        "related": [
            "approval/deploy/tests/test_network_policy.py::test_ui_is_reached_from_outside_over_https_only",
            "tests/network/test_check_acl.py::test_committed_policy_ok",
            f"{DEP}/test_railway_audit.py::test_exposure_fails_in_every_environment",
        ],
    },
}


def matrix_ids(text: str) -> list[str]:
    """Ids of the matrix tables, in order: rows that start with `| XX-NN |`."""
    return re.findall(r"^\|\s*([A-Z]+-\d+)\s*\|", text, flags=re.MULTILINE)


def mapping() -> dict[str, dict[str, Any]]:
    """id → {tests | evidence | status+reason}: the whole map, one entry per id."""
    out: dict[str, dict[str, Any]] = {}
    for table in (SC, C.EX, MC, OPS):
        for i, tests in table.items():
            out[i] = {"tests": tests}
    out["MC-05"] = {"evidence": MC05_EVIDENCE}
    for i in ("SI-01", "SI-02", "SI-03"):
        out[i] = {"evidence": SI_EVIDENCE}
    out.update(OPEN)
    return out


def all_tests(entries: dict[str, dict[str, Any]]) -> list[str]:
    return sorted({t for e in entries.values() for t in e.get("tests", []) + e.get("related", [])})


def _functions(file: Path) -> set[str]:
    return {
        n.name for n in ast.parse(file.read_text("utf-8")).body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def problems(ids: list[str], entries: dict[str, dict[str, Any]]) -> list[str]:
    """What is wrong with the map: ids missing from it or from the matrix, duplicates, tests that do not exist."""
    bad = [f"{i}: in the matrix, no mapping" for i in ids if i not in entries]
    bad += [f"{i}: mapped, not in the matrix" for i in entries if i not in ids]
    bad += [f"{i}: twice in the matrix" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for i, e in entries.items():
        if sum(k in e for k in ("tests", "evidence", "status")) != 1:
            bad.append(f"{i}: needs exactly one of tests / evidence / status")
        if "status" in e and (e["status"] not in ("manual", "not_run") or not e.get("reason")):
            bad.append(f"{i}: an open id is manual or not_run and has a reason")
        if "evidence" in e and not (ROOT / e["evidence"]).exists():
            bad.append(f"{i}: evidence file {e['evidence']} is missing")
    for t in all_tests(entries):
        path, _, func = t.partition("::")
        file = ROOT / path
        if not file.is_file():
            bad.append(f"{t}: no such file")
        elif func and func not in _functions(file):
            bad.append(f"{t}: no such top-level test function")
    return bad


def _count(found: dict[str, list[bool]], t: str) -> dict[str, Any]:
    return {"test": t, "cases": len(found.get(t, [])), "passed": sum(found.get(t, []))}


def judge(tests: list[str], found: dict[str, list[bool]]) -> tuple[str, list[dict[str, Any]]]:
    """passed / failed / not_run for a list of tests and the per-test counts."""
    rows = [_count(found, t) for t in tests]
    if any(r["passed"] != r["cases"] for r in rows):
        return "failed", rows
    missing = [r["test"] for r in rows if r["cases"] == 0]
    if not missing:
        return "passed", rows
    # a stand test with no JUnit was not run; a missing non-stand test was selected and did not come back
    return ("not_run" if all(t.startswith("stand/") for t in missing) else "failed"), rows


def build(entries: dict[str, dict[str, Any]], found: dict[str, list[bool]], root: Path = ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, e in entries.items():
        row: dict[str, Any] = {"id": i}
        if "tests" in e:
            row["status"], row["tests"] = judge(e["tests"], found)
        elif "evidence" in e:
            ok = "status: passed" in (root / e["evidence"]).read_text("utf-8")
            row.update(status="passed" if ok else "failed", evidence=e["evidence"])
        else:
            row.update(status=e["status"], reason=e["reason"])
            if e.get("related"):
                row["related"] = [_count(found, t) for t in e["related"]]
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stand", nargs="*", default=[], type=Path, help="JUnit files of stand/run.sh")
    ap.add_argument("--out", type=Path, default=ROOT / "reports/conformance/stage-F.json")
    args = ap.parse_args()
    entries = mapping()
    bad = problems(matrix_ids(MATRIX.read_text("utf-8")), entries)
    if bad:
        print("stage F: the map does not match the matrix:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2
    nodes = [t for t in all_tests(entries) if not t.startswith("stand/")]
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "f.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
               f"--junitxml={junit}", *nodes]  # fmt: skip
        run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        found = C.results(junit, *args.stand) if junit.exists() else {}
    git = ["git", "rev-parse", "--short", "HEAD"]
    commit = subprocess.run(git, cwd=ROOT, capture_output=True, text=True, check=False).stdout
    ids = sorted(build(entries, found), key=lambda r: (r["id"].split("-")[0], int(r["id"].split("-")[1])))
    counts: dict[str, int] = {}
    for r in ids:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    report: dict[str, Any] = {
        "stage": "F", "commit": commit.strip(), "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "uv run python scripts/ci/stage_f.py " + " ".join(f"--stand {p}" for p in args.stand),
        "pytest_exit": run.returncode, "counts": counts, "ids": ids,
        "all_passed": all(r["status"] == "passed" for r in ids),
    }  # fmt: skip
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"stage F: {counts.get('passed', 0)}/{len(ids)} passed, {counts} -> {args.out}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
