from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import workflow_doctor  # noqa: E402


class WorkflowDoctorArgumentTests(unittest.TestCase):
    def parse_args(self, argv: list[str]):
        parse_doctor_args = getattr(workflow_doctor, "parse_doctor_args", None)
        self.assertTrue(callable(parse_doctor_args), "parse_doctor_args(argv) must be available")
        return parse_doctor_args(argv)

    def test_no_arguments_leave_startup_cleanup_disabled(self) -> None:
        args = self.parse_args([])

        self.assertFalse(args.apply_startup_cleanup)
        self.assertFalse(args.skip_startup_cleanup)

    def test_apply_flag_enables_startup_cleanup(self) -> None:
        args = self.parse_args(["--apply-startup-cleanup"])

        self.assertTrue(args.apply_startup_cleanup)
        self.assertFalse(args.skip_startup_cleanup)

    def test_skip_flag_is_an_explicit_no_op(self) -> None:
        args = self.parse_args(["--skip-startup-cleanup"])

        self.assertFalse(args.apply_startup_cleanup)
        self.assertTrue(args.skip_startup_cleanup)

    def test_apply_and_skip_flags_are_mutually_exclusive(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.parse_args(["--apply-startup-cleanup", "--skip-startup-cleanup"])

        self.assertEqual(raised.exception.code, 2)

    def test_default_main_path_does_not_select_or_apply_cleanup(self) -> None:
        report = {
            "status": "ready",
            "current_stage": "ready",
            "current_stage_judgment": "ready",
            "last_completed_stage": "ready",
            "next_stage": None,
            "next_skill": None,
            "stage_plan": {"current_stage": None},
            "blocked_reasons": [],
            "allowed_next_actions": [],
            "forbidden_next_actions": [],
            "startup_hygiene": {"mode": "report_only_no_delete"},
        }
        selection_result = {
            "mode": "auto_detected",
            "abandoned_product_ids": [],
            "abandoned_paths": [],
            "historical_report_product_ids": [],
            "skipped_protected_product_ids": [],
        }
        validation_result = {
            "script": "scripts/validate_skill_tree.py",
            "exit_code": 0,
            "report_status": "pass",
        }

        with (
            patch.object(sys, "argv", ["workflow_doctor.py"]),
            patch.object(workflow_doctor, "project_root", return_value=ROOT),
            patch.object(workflow_doctor, "run_script", return_value=validation_result),
            patch.object(workflow_doctor.detect_current_state, "build_report", return_value=report),
            patch.object(workflow_doctor.detect_current_state, "write_json"),
            patch.object(workflow_doctor.detect_current_state, "write_markdown"),
            patch.object(
                workflow_doctor.detect_current_state,
                "startup_cleanup_selection",
                return_value=selection_result,
            ) as select_cleanup,
            patch.object(
                workflow_doctor.detect_current_state,
                "startup_cleanup_candidates",
                return_value=[],
            ) as build_candidates,
            patch.object(workflow_doctor, "apply_startup_cleanup", return_value=[]) as apply_cleanup,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            exit_code = workflow_doctor.main()

        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(summary["startup_cleanup"]["applied"])
        self.assertEqual(summary["startup_cleanup"]["candidate_count"], 0)
        self.assertEqual(summary["startup_cleanup"]["moved_to_recycle_bin_count"], 0)
        self.assertEqual(summary["startup_cleanup"]["failed_count"], 0)
        select_cleanup.assert_not_called()
        build_candidates.assert_not_called()
        apply_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
