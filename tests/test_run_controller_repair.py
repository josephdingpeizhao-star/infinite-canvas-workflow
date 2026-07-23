from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import run_controller  # noqa: E402


def ready_route(stage: str = "ready") -> dict:
    return {
        "current_stage": stage,
        "next_required_skill": None,
        "blocked_reasons": [],
        "available_artifacts": ["qc_reports"],
        "outputs": {"renders": {"file_count": 14}, "repaired": {"file_count": 0}},
    }


def valid_gate(**overrides) -> dict:
    value = {
        "report_found": True,
        "report_valid": True,
        "target_count": 18,
        "actionable_target_count": 17,
        "render_enabled": True,
        "api_key_configured": True,
    }
    value.update(overrides)
    return value


class RepairRunControllerGateTest(unittest.TestCase):
    def test_ready_valid_nonempty_enabled_command_is_authorized(self) -> None:
        self.assertEqual(
            "repair",
            run_controller.resolve_repair_command(
                ("run", "repair"),
                ready_route(),
                valid_gate(),
            ),
        )

    def test_non_ready_route_is_rejected(self) -> None:
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.resolve_repair_command(
                ("run", "repair"),
                ready_route("needs_qc_reports"),
                valid_gate(),
            )

    def test_missing_or_invalid_report_is_rejected(self) -> None:
        for facts in (
            valid_gate(report_found=False, report_valid=False),
            valid_gate(report_valid=False),
        ):
            with self.subTest(facts=facts):
                with self.assertRaises(run_controller.RunValidationError):
                    run_controller.resolve_repair_command(("run", "repair"), ready_route(), facts)

    def test_empty_or_review_only_targets_are_rejected(self) -> None:
        for facts in (
            valid_gate(target_count=0, actionable_target_count=0),
            valid_gate(target_count=1, actionable_target_count=0),
        ):
            with self.subTest(facts=facts):
                with self.assertRaises(run_controller.RunValidationError):
                    run_controller.resolve_repair_command(("run", "repair"), ready_route(), facts)

    def test_render_switch_or_api_key_missing_is_rejected(self) -> None:
        for facts in (
            valid_gate(render_enabled=False),
            valid_gate(api_key_configured=False),
        ):
            with self.subTest(facts=facts):
                with self.assertRaises(run_controller.RunValidationError):
                    run_controller.resolve_repair_command(("run", "repair"), ready_route(), facts)

    def test_retry_next_and_other_commands_are_rejected(self) -> None:
        for command in (("retry", "repair"), ("run", "next"), ("run", "renders")):
            with self.subTest(command=command):
                with self.assertRaises(run_controller.RunValidationError):
                    run_controller.resolve_repair_command(command, ready_route(), valid_gate())

    def test_legacy_parser_resolver_and_verbs_contract_is_unchanged(self) -> None:
        self.assertEqual(("run", "retry"), run_controller.RUN_VERBS)
        self.assertEqual(("run", "repair"), run_controller.parse_run_content("run: repair"))
        self.assertEqual([], run_controller.runnable_steps(ready_route(), {"found": True, "status": "pass"}))
        with self.assertRaises(run_controller.RunValidationError):
            run_controller.resolve_command(
                ("run", "repair"),
                ready_route(),
                {"found": True, "status": "pass", "render_blocked": False},
            )


if __name__ == "__main__":
    unittest.main()
