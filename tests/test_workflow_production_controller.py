from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import workflow_production_controller as controller  # noqa: E402


class RequestedOutputsTest(unittest.TestCase):
    def test_empty_manifest_is_patched_through_existing_editor_gate_with_all_four_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "cup.batch_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "product_id": "cup",
                        "requested_outputs": [],
                        "inputs": {},
                        "drafts": {},
                        "artifacts": {},
                        "outputs": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = controller.apply_production_requested_outputs(manifest_path)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            ["main", "detail", "final_prompts", "qc_reports"],
            saved["requested_outputs"],
        )
        self.assertTrue(result["changed"])

    def test_nonempty_divergent_targets_stop_without_rewriting_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "cup.batch_manifest.json"
            original = {"product_id": "cup", "requested_outputs": ["main"]}
            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(controller.ProductionGateError):
                controller.apply_production_requested_outputs(manifest_path)
            self.assertEqual(original, json.loads(manifest_path.read_text(encoding="utf-8")))


class RunControllerDelegationTest(unittest.TestCase):
    def test_every_step_is_parsed_and_resolved_by_run_controller(self) -> None:
        route = {"current_stage": "needs_product_identity_archive"}
        integrity = {"found": False}
        with (
            mock.patch.object(controller.run_controller, "parse_run_content", return_value=("run", "next")) as parse,
            mock.patch.object(controller.run_controller, "resolve_command", return_value="identity") as resolve,
        ):
            self.assertEqual("identity", controller.resolve_gated_step("run: next", route, integrity))
        parse.assert_called_once_with("run: next")
        resolve.assert_called_once_with(("run", "next"), route, integrity)

    def test_partial_render_uses_existing_retry_semantics_and_complete_batch_runs_qc(self) -> None:
        route = {"current_stage": "needs_qc_reports"}
        self.assertEqual("retry: renders", controller.next_gated_command(route, accepted_render_count=1))
        self.assertEqual("run: qc", controller.next_gated_command(route, accepted_render_count=14))

    def test_pre_qc_route_uses_run_next_for_integrity_and_initial_render(self) -> None:
        route = {"current_stage": "needs_generated_images_before_qc"}
        self.assertEqual("run: next", controller.next_gated_command(route, accepted_render_count=0))

    def test_ready_route_is_terminal(self) -> None:
        self.assertIsNone(controller.next_gated_command({"current_stage": "ready"}, accepted_render_count=14))

    def test_qc_has_human_progress_message(self) -> None:
        self.assertEqual("正在逐张质检 14 张成图…", controller.human_step_message("qc", produced_count=14))


class SelectionTest(unittest.TestCase):
    def test_one_completed_information_card_selects_real_mode_and_batch_id(self) -> None:
        state = {
            "nodes": [
                {"id": "machine", "type": "workflow"},
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": "杯子_20260719", "imageCount": 2},
                        }
                    },
                },
                {
                    "id": "original",
                    "type": "image",
                    "metadata": {"content": "blob:original", "storageKey": "image:original"},
                },
            ],
            "connections": [
                {"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"},
                {"id": "image-machine", "fromNodeId": "original", "toNodeId": "machine"},
            ],
        }
        selection = controller.resolve_production_selection("machine", state)
        self.assertEqual("杯子_20260719", selection.batch_id)
        self.assertEqual("card", selection.card_id)

    def test_connected_unregistered_card_never_falls_back_to_demo(self) -> None:
        state = {
            "nodes": [
                {"id": "machine", "type": "workflow"},
                {"id": "card", "type": "batch-info", "metadata": {"batchIntake": {"status": "draft"}}},
            ],
            "connections": [{"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"}],
        }
        with self.assertRaises(controller.ProductionGateError):
            controller.resolve_production_selection("machine", state)

    def test_registered_card_without_connected_material_stops_real_mode(self) -> None:
        state = {
            "nodes": [
                {"id": "machine", "type": "workflow"},
                {
                    "id": "card",
                    "type": "batch-info",
                    "metadata": {
                        "batchIntake": {
                            "status": "completed",
                            "receipt": {"batchId": "杯子_20260719", "imageCount": 2},
                        }
                    },
                },
            ],
            "connections": [{"id": "card-machine", "fromNodeId": "card", "toNodeId": "machine"}],
        }
        with self.assertRaisesRegex(controller.ProductionGateError, "至少 1 张批次素材"):
            controller.resolve_production_selection("machine", state)


if __name__ == "__main__":
    unittest.main()
