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


if __name__ == "__main__":
    unittest.main()
