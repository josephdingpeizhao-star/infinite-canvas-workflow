from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_demo_executor import write_placeholder_png  # noqa: E402
import workflow_production_projection as projection  # noqa: E402


class ProductionProjectionTest(unittest.TestCase):
    def test_real_projection_contains_no_data_uri_or_file_path_and_uses_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_01.png"
            write_placeholder_png(path, width=1254, height=1254, kind="main", ordinal=1)
            artifact = projection.artifact_from_path("杯子_20260719", path)
            machine = {"id": "machine", "type": "workflow", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            ops, node = projection.build_output_projection_ops(machine, [machine], artifact, "http://127.0.0.1:17373")

        self.assertEqual("wfprod-output:杯子_20260719:main_01", node["id"])
        metadata = node["metadata"]
        self.assertEqual("loading", metadata["status"])
        self.assertNotIn("data:", str(metadata))
        self.assertNotIn(str(path.parent), str(metadata))
        self.assertTrue(metadata["workflowProductionOutput"]["downloadUrl"].startswith("http://127.0.0.1:17373/"))
        self.assertTrue(any(op["type"] == "connect_nodes" for op in ops))

    def test_existing_persisted_node_is_not_replaced_or_disconnected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_01.png"
            write_placeholder_png(path, width=1086, height=1448, kind="detail", ordinal=1)
            artifact = projection.artifact_from_path("cup", path)
            machine = {"id": "machine", "type": "workflow", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            existing = {
                "id": projection.output_node_id("cup", "detail_01"),
                "type": "image",
                "position": {"x": 700, "y": 0},
                "width": 168,
                "height": 224,
                "metadata": {
                    "storageKey": "image:persisted",
                    "workflowProductionOutput": {"sha256": artifact.sha256},
                },
            }
            ops, _node = projection.build_output_projection_ops(machine, [machine, existing], artifact, "http://127.0.0.1:17373")

        self.assertFalse(any(op["type"] in {"delete_node", "add_node", "update_node"} for op in ops))
        self.assertTrue(any(op["type"] == "connect_nodes" for op in ops))

    def test_layout_avoids_machine_and_existing_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_02.png"
            write_placeholder_png(path, width=1254, height=1254, kind="main", ordinal=2)
            artifact = projection.artifact_from_path("cup", path)
            machine = {"id": "machine", "type": "workflow", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            blocker = {"id": "blocker", "type": "image", "position": {"x": 560, "y": 94}, "width": 176, "height": 176}
            _ops, node = projection.build_output_projection_ops(machine, [machine, blocker], artifact, "http://127.0.0.1:17373")
        self.assertGreater(node["position"]["x"], blocker["position"]["x"])

    def test_repaired_projection_has_distinct_id_label_source_and_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_02.png"
            write_placeholder_png(path, width=1024, height=1024, kind="main", ordinal=2)
            artifact = projection.artifact_from_path("cup", path, source="repaired")
            machine = {"id": "machine", "type": "workflow", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            _ops, node = projection.build_output_projection_ops(
                machine,
                [machine],
                artifact,
                "http://127.0.0.1:17373",
            )

        self.assertEqual("wfprod-repaired:cup:main_02", node["id"])
        self.assertEqual("返修·主图 2", node["title"])
        proof = node["metadata"]["workflowProductionOutput"]
        self.assertEqual("repaired", proof["source"])
        self.assertTrue(proof["downloadUrl"].endswith("/outputs/repaired/main_02"))

    def test_repaired_layout_starts_beyond_original_output_fan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "detail_06.png"
            write_placeholder_png(path, width=96, height=128, kind="detail", ordinal=6)
            artifact = projection.artifact_from_path("cup", path, source="repaired")
            machine = {"id": "machine", "type": "workflow", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            _ops, node = projection.build_output_projection_ops(
                machine,
                [machine],
                artifact,
                "http://127.0.0.1:17373",
            )
        self.assertGreaterEqual(node["position"]["x"], 1_650)

    def test_matching_legacy_render_gets_source_backfill_without_storage_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_01.png"
            write_placeholder_png(path, width=96, height=96, kind="main", ordinal=1)
            artifact = projection.artifact_from_path("cup", path, source="renders")
            node = {
                "id": projection.output_node_id("cup", "main_01"),
                "type": "image",
                "metadata": {
                    "content": "blob:kept",
                    "storageKey": "image:kept",
                    "workflowProductionOutput": {
                        "batchId": "cup",
                        "configId": "main_01",
                        "sha256": artifact.sha256,
                        "downloadUrl": "http://127.0.0.1:17373/workflow-production/cup/outputs/main_01",
                    },
                },
            }
            op = projection.build_render_source_backfill_op(
                node,
                artifact,
                "http://127.0.0.1:17373",
            )

        self.assertEqual("update_node", op["type"])
        self.assertEqual("renders", op["metadata"]["workflowProductionOutput"]["source"])
        self.assertNotIn("storageKey", op["metadata"])
        self.assertNotIn("content", op["metadata"])

    def test_mismatched_render_proof_records_safe_rejection_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".canvas_demo").write_text("safe\n", encoding="utf-8")
            path = root / "main_01.png"
            write_placeholder_png(path, width=96, height=96, kind="main", ordinal=1)
            artifact = projection.artifact_from_path("cup", path, source="renders")
            node = {
                "id": projection.output_node_id("cup", "main_01"),
                "type": "image",
                "metadata": {
                    "workflowProductionOutput": {
                        "batchId": "cup",
                        "configId": "main_01",
                        "sha256": "0" * 64,
                    }
                },
            }
            op = projection.build_render_source_backfill_op(
                node,
                artifact,
                "http://127.0.0.1:17373",
            )

        proof = op["metadata"]["workflowProductionOutput"]
        self.assertNotIn("source", proof)
        self.assertEqual("source_proof_mismatch", proof["sourceBackfillCode"])
        self.assertNotIn(str(path.parent), str(op))


if __name__ == "__main__":
    unittest.main()
