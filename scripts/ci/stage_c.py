#!/usr/bin/env python3
"""AC-M10-07: reports/conformance/stage-C.json — EX-01…EX-25 and MC-05 with the tests that prove each and their
status in this run. pytest writes the results (JUnit XML); this script only maps ids to tests and reads them.

    uv run python scripts/ci/stage_c.py [--stand JUNIT ...] [--mc05 REPORT_MD]

Runs the mapped Python tests once (`pytest --junitxml`, Docker needed for the DB tests). `--stand` adds JUnit files
of `stand/run.sh` (EX-01 through ContextForge). An id is `passed` only if every mapped test ran and passed (a
parametrized test: every case). MC-05 is `passed` with the report of the re-run on the next minor versions.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
A, UI, NO, DEMO = "approval/tests_db", "approval/ui/tests_db", "approval/notifier/tests_db", "connectors/demo/tests"
# EX id → tests (path::function; a parametrized function counts with all its cases). Conformance Matrix stage C.
EX: dict[str, list[str]] = {
    "EX-01": [
        f"{A}/test_executor.py::test_propose_is_listed_and_execute_is_not",
        "stand/tests/test_ex.py::test_ex01_no_agent_lists_execute_on_any_path",
        "stand/tests/test_ex.py::test_ex01_calls_by_name_are_refused_on_every_path",
        "stand/tests/test_ex.py::test_ex01_unified_pdp_denies_a_registered_execute_tool",
    ],
    "EX-02": [f"{DEMO}/test_effects.py::test_propose_sends_canonical_args_hash_preview_and_requester"],
    "EX-03": [f"{A}/test_executor.py::test_ac03_invalid_execution_tokens_are_401_without_dispatch"],
    "EX-04": [f"{A}/test_executor.py::test_ac04_replay_after_success_is_409_and_the_provider_is_called_once"],
    "EX-05": [f"{A}/test_executor.py::test_ac05_changed_args_are_422"],
    "EX-06": [f"{A}/test_propose.py::test_idempotent_repeat"],
    "EX-07": [
        f"{UI}/test_decisions.py::test_csrf_missing_or_foreign",
        f"{UI}/test_decisions.py::test_stale_version_or_changed_hash",
        f"{UI}/test_decisions.py::test_machine_credentials_cannot_decide",
        f"{UI}/test_decisions.py::test_requester_cannot_approve_own_intent",
        f"{UI}/test_decisions.py::test_approver_of_another_tenant",
        f"{A}/test_decider.py::test_named_human_must_own_the_passkey",
    ],
    "EX-08": [
        f"{UI}/test_decisions.py::test_high_risk_needs_step_up",
        f"{UI}/test_decisions.py::test_quorum_of_two_needs_two_people",
        f"{A}/test_decider.py::test_replay_and_stale_version",
    ],
    "EX-09": [f"{A}/test_state_machine.py::test_every_status_event_pair"],
    "EX-10": [f"{A}/test_executor.py::test_ac02_50_intents_10_executors_no_dedupe_one_call_each"],
    "EX-11": [
        f"{A}/test_executor.py::test_ac06_commit_then_drop_is_unknown_then_reconciled_succeeded",
        f"{A}/test_executor.py::test_ac07_connector_killed_after_dispatch",
    ],
    "EX-12": [f"{A}/test_executor.py::test_ac08_timeout_before_start_twice_then_ok"],
    "EX-13": [
        f"{A}/test_executor.py::test_ac10_kill_switch_after_reserve_before_dispatch",
        f"{A}/test_admin.py::test_global_switch_blocks_the_next_dispatch_of_every_tenant",
        f"{A}/test_admin.py::test_tool_scope",
        f"{A}/test_state_machine.py::test_t7_t9_kill_switch_and_limits",
    ],
    "EX-14": [f"{A}/test_executor.py::test_ac11_propose_grant_revoked_between_approve_and_execute"],
    "EX-15": [f"{NO}/test_bot.py::test_no_callback_buttons_and_commands_do_not_decide"],
    "EX-16": [
        f"{A}/test_executor.py::test_ac12_approval_service_unavailable",
        f"{DEMO}/test_effects.py::test_propose_when_the_approval_service_is_down",
    ],
    "EX-17": [f"{A}/test_propose.py::test_31st_propose_within_an_hour_is_rate_limited"],
    "EX-18": [
        "approval/tests/test_policy.py::test_reference_scenarios",
        "approval/tests/test_policy.py::test_limit_boundary",
    ],
    "EX-19": [f"{UI}/test_e2e.py::test_approver_signs_in_and_approves", "approval/ui/tests/test_render.py"],
    "EX-20": [
        f"{NO}/test_webhooks.py::test_receiver_refuses_a_bad_signature_an_old_t_and_a_replay",
        f"{NO}/test_webhooks.py::test_unavailable_subscriber_backoff_then_dead_and_alert",
        "packages/gxg-connector-kit/tests/test_webhooks.py::test_refusals",
        "approval/deploy/tests/test_alert_rules.py::test_rule_unit_tests_pass",
    ],
    "EX-21": [f"{A}/test_propose.py::test_unknown_connector_or_foreign_key"],
    "EX-22": [
        "packages/gxg-connector-kit/tests/test_canonical.py::test_vectors_bytes_and_sha256",
        "packages/gxg-connector-kit/tests/test_canonical.py::test_rejected_values",
    ],
    "EX-23": [
        f"{A}/test_executor.py::test_ac09_executor_dies_between_reserve_and_dispatch",
        f"{A}/test_executor.py::test_ac07_t14_when_the_executor_dies_too",
        f"{A}/test_state_machine.py::test_t12_retries_until_max_attempts_then_fails",
    ],
    "EX-24": [f"{A}/test_executor.py::test_ac14_rotation_without_downtime_and_removal_of_a_compromised_kid"],
    "EX-25": [
        f"{A}/test_executor.py::test_ex25_quota_429_then_ok_spends_no_attempt",
        f"{A}/test_executor.py::test_ex25_always_429_expires_at_the_deadline_with_no_provider_call",
        f"{A}/test_executor.py::test_ex25_no_task_past_the_deadline",
        f"{A}/test_executor.py::test_ex25_malformed_429_is_t12a",
        f"{A}/test_executor.py::test_ex25_429_after_dispatch_is_outcome_unknown",
        f"{A}/test_executor.py::test_ex25_late_429_after_t10_is_ignored",
        f"{A}/test_executor.py::test_ex25_defer_cas_race_is_ignored",
        f"{A}/test_executor.py::test_ex25_reserve_needs_a_due_task",
        f"{DEMO}/test_effects.py::test_execute_quota_answers_429_before_dispatch",
    ],
}


def results(*junits: Path) -> dict[str, list[bool]]:
    """`path::function` (and `path` for a whole file) → passed per case."""
    out: dict[str, list[bool]] = {}
    for junit in junits:
        for case in ET.parse(junit).iter("testcase"):  # noqa: S314 (pytest's own JUnit files of this run)
            cls, name = case.get("classname", ""), case.get("name", "").split("[", 1)[0]
            parts = cls.split(".")
            path = "/".join(parts) + ".py"
            ok = not any(case.find(t) is not None for t in ("failure", "error", "skipped"))
            for key in (f"{path}::{name}", path):
                out.setdefault(key, []).append(ok)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stand", nargs="*", default=[], type=Path)
    ap.add_argument("--mc05", type=Path, default=ROOT / "reports/conformance/M10-mc05.md")
    ap.add_argument("--out", type=Path, default=ROOT / "reports/conformance/stage-C.json")
    args = ap.parse_args()
    nodes = sorted({t for tests in EX.values() for t in tests if not t.startswith("stand/")})
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "c.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
               f"--junitxml={junit}", *nodes]  # fmt: skip
        run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        found = results(junit, *args.stand)
    git = ["git", "rev-parse", "--short", "HEAD"]
    commit = subprocess.run(git, cwd=ROOT, capture_output=True, text=True, check=False).stdout
    report: dict[str, object] = {
        "stage": "C", "commit": commit.strip(), "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": "uv run python scripts/ci/stage_c.py " + " ".join(f"--stand {p}" for p in args.stand),
        "pytest_exit": run.returncode, "ids": [],
    }  # fmt: skip
    ids: list[dict[str, object]] = []
    for ex, tests in EX.items():
        cases = {t: found.get(t, []) for t in tests}
        ok = all(cases.values()) and all(all(v) for v in cases.values())
        ids.append({"id": ex, "status": "passed" if ok else "failed",
                    "tests": [{"test": t, "cases": len(v), "passed": sum(v)} for t, v in cases.items()]})  # fmt: skip
    mc05 = args.mc05.exists() and "status: passed" in args.mc05.read_text("utf-8")
    ids.append({"id": "MC-05", "status": "passed" if mc05 else "failed", "evidence": str(args.mc05.relative_to(ROOT))})
    report["ids"] = ids
    report["all_passed"] = all(i["status"] == "passed" for i in ids)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"stage C: {sum(i['status'] == 'passed' for i in ids)}/{len(ids)} passed -> {args.out}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
