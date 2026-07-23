from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from final_prompt_integrity_fixtures import read_json, write_json  # noqa: E402
from qc_repair import prepare_repair_plan  # noqa: E402
from qc_repair_fixtures import (  # noqa: E402
    build_qc_repair_fixture,
    read_fixture_report,
    rewrite_both_reports,
)
from render_task_assembler import NEGATIVE_PROMPT_SEPARATOR, assemble_render_tasks  # noqa: E402


class QcRepairPlanTest(unittest.TestCase):
    def test_valid_report_aggregates_eighteen_targets_into_eight_index_ordered_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))

            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            self.assertTrue(prepared.report_found)
            self.assertTrue(prepared.report_valid)
            self.assertEqual(18, prepared.target_count)
            self.assertEqual(17, prepared.actionable_target_count)
            self.assertEqual(
                ("main_01", "main_02", "main_05", "detail_01", "detail_02", "detail_04", "detail_05", "detail_06"),
                tuple(order.config_id for order in prepared.plan.work_orders),
            )

    def test_main_02_keeps_six_targets_in_one_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            order = next(item for item in prepared.plan.work_orders if item.config_id == "main_02")

            self.assertEqual(6, len(order.targets))
            self.assertEqual(6, len(order.actionable_targets))
            self.assertEqual((), order.review_targets)

    def test_detail_06_keeps_review_target_out_of_actionable_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            order = next(item for item in prepared.plan.work_orders if item.config_id == "detail_06")
            review_goal = order.review_targets[0].repair_goal

            self.assertEqual(2, len(order.targets))
            self.assertEqual(1, len(order.actionable_targets))
            self.assertEqual(1, len(order.review_targets))
            self.assertNotIn(review_goal, order.task.prompt)
            self.assertIn(order.actionable_targets[0].repair_goal, order.task.prompt)

    def test_prompt_preserves_original_then_addendum_then_negative_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            order = prepared.plan.work_orders[0]
            document = read_json(fixture.bundle.prompt_path(order.config_id))

            self.assertEqual(document["final_prompt"], order.original_final_prompt)
            self.assertTrue(order.task.prompt.startswith(document["final_prompt"] + order.repair_addendum))
            self.assertTrue(order.task.prompt.endswith(NEGATIVE_PROMPT_SEPARATOR + document["negative_prompt"]))
            self.assertIn("不新增生成方向", order.repair_addendum)
            self.assertIn("绑定角度", order.repair_addendum)
            self.assertIn("画布比例", order.repair_addendum)

    def test_return_stage_changes_do_not_change_provider_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            before = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )
            report = read_fixture_report(fixture)
            for index, target in enumerate(report["repair_targets"]):
                target["return_stage"] = f"not-a-route-{index}"
            rewrite_both_reports(fixture, report)

            after = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            self.assertEqual(
                tuple(order.task.prompt for order in before.plan.work_orders),
                tuple(order.task.prompt for order in after.plan.work_orders),
            )

    def test_missing_report_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            fixture.workspace_report.unlink()
            fixture.repo_report.unlink()

            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            self.assertFalse(prepared.report_found)
            self.assertFalse(prepared.report_valid)
            self.assertIsNone(prepared.plan)
            self.assertEqual("qc_report_missing", prepared.error_code)

    def test_empty_repair_targets_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            report = read_fixture_report(fixture)
            report["repair_targets"] = []
            report["issues"] = []
            rewrite_both_reports(fixture, report)

            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            self.assertTrue(prepared.report_found)
            self.assertTrue(prepared.report_valid)
            self.assertEqual(0, prepared.target_count)
            self.assertIsNone(prepared.plan)
            self.assertEqual("repair_targets_empty", prepared.error_code)

    def test_mismatched_report_copies_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            report = read_json(fixture.repo_report)
            report["notes"] = "different"
            write_json(fixture.repo_report, report)

            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            self.assertTrue(prepared.report_found)
            self.assertFalse(prepared.report_valid)
            self.assertIsNone(prepared.plan)
            self.assertEqual("qc_report_mismatch", prepared.error_code)

    def test_output_paths_are_repaired_and_reference_images_match_render_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = build_qc_repair_fixture(Path(tmp))
            original = assemble_render_tasks(fixture.bundle.manifest, fixture.bundle.index_path)
            expected_references = {
                task.output_path.stem: task.reference_images for task in original.tasks
            }
            prepared = prepare_repair_plan(
                fixture.bundle.manifest,
                fixture.bundle.manifest_path,
                repo_reports_dir=fixture.repo_reports_dir,
            )

            for order in prepared.plan.work_orders:
                self.assertEqual(fixture.repaired_dir, order.task.output_path.parent)
                self.assertEqual(f"{order.config_id}.png", order.task.output_path.name)
                self.assertNotEqual(fixture.bundle.renders_dir, order.task.output_path.parent)
                self.assertEqual(expected_references[order.config_id], order.task.reference_images)


if __name__ == "__main__":
    unittest.main()
