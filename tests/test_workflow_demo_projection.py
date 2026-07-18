from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from workflow_demo_executor import WorkflowDemoArtifact, write_placeholder_png  # noqa: E402
from workflow_demo_projection import (  # noqa: E402
    OUTPUT_NODE_PREFIX,
    build_output_projection_ops,
    clear_workflow_demo_output_ids,
    output_node_id,
)


class WorkflowDemoProjectionTests(unittest.TestCase):
    def artifact(self, root: Path, index: int = 1, kind: str = "main") -> WorkflowDemoArtifact:
        marker = root / ".canvas_demo"
        if not marker.exists():
            marker.write_text("safe\n", encoding="utf-8")
        ordinal = index if kind == "main" else index - 6
        path = root / (f"main_{ordinal:02d}.png" if kind == "main" else f"detail_{ordinal:02d}.png")
        width, height = ((720, 720) if kind == "main" else (720, 960))
        write_placeholder_png(path, width=width, height=height, kind=kind, ordinal=ordinal)
        return WorkflowDemoArtifact(path=path, index=index, total=14, kind=kind, ordinal=ordinal, width=width, height=height)

    def test_output_id_is_run_scoped_and_uses_only_demo_prefix(self) -> None:
        node_id = output_node_id("workflow-1", "run-123", 4)
        self.assertEqual("wfdemo-output:workflow-1:run-123:04", node_id)
        self.assertTrue(node_id.startswith(OUTPUT_NODE_PREFIX))

    def test_projection_reads_landed_png_into_demo_only_data_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self.artifact(Path(tmp))
            machine = {"id": "workflow-1", "position": {"x": 100, "y": 100}, "width": 420, "height": 300}
            ops, projected = build_output_projection_ops(machine, [], "run-123", artifact)
            add = next(op for op in ops if op["type"] == "add_node")
            content = add["metadata"]["content"]
            self.assertTrue(content.startswith("data:image/png;base64,"))
            self.assertTrue(base64.b64decode(content.split(",", 1)[1]).startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual("image", add["nodeType"])
            self.assertEqual(projected["id"], add["id"])

    def test_projection_is_idempotent_delete_add_connect_for_exact_current_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self.artifact(Path(tmp))
            machine = {"id": "workflow-1", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            ops, _projected = build_output_projection_ops(machine, [], "run-123", artifact)
            expected = "wfdemo-output:workflow-1:run-123:01"
            self.assertEqual({"type": "delete_node", "ids": [expected]}, ops[0])
            self.assertEqual(expected, ops[1]["id"])
            self.assertEqual(
                {"type": "connect_nodes", "id": f"conn:{expected}", "fromNodeId": "workflow-1", "toNodeId": expected},
                ops[2],
            )

    def test_projection_places_every_result_right_of_machine_without_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            machine = {"id": "workflow-1", "position": {"x": 0, "y": 0}, "width": 420, "height": 300}
            occupied = [machine, {"id": "user", "position": {"x": 560, "y": 0}, "width": 220, "height": 220}]
            projected_nodes = []
            for index in range(1, 15):
                kind = "main" if index <= 6 else "detail"
                artifact = self.artifact(root, index=index, kind=kind)
                _ops, projected = build_output_projection_ops(machine, occupied + projected_nodes, "run-layout", artifact)
                self.assertGreater(projected["position"]["x"], machine["position"]["x"] + machine["width"])
                for other in occupied + projected_nodes:
                    self.assertFalse(_overlap(projected, other, 20))
                projected_nodes.append(projected)

    def test_manual_cleanup_selects_only_demo_prefix_and_optional_machine(self) -> None:
        nodes = [
            {"id": "wfdemo-output:workflow-1:run-a:01"},
            {"id": "wfdemo-output:workflow-2:run-b:01"},
            {"id": "workflow-1"},
            {"id": "wf:real:stage_render"},
            {"id": "user-image"},
        ]
        self.assertEqual(
            ["wfdemo-output:workflow-1:run-a:01", "wfdemo-output:workflow-2:run-b:01"],
            clear_workflow_demo_output_ids(nodes),
        )
        self.assertEqual(
            ["wfdemo-output:workflow-1:run-a:01"],
            clear_workflow_demo_output_ids(nodes, machine_id="workflow-1"),
        )


def _overlap(first: dict, second: dict, gap: int) -> bool:
    return not (
        first["position"]["x"] + first["width"] + gap <= second["position"]["x"] - gap
        or second["position"]["x"] + second["width"] + gap <= first["position"]["x"] - gap
        or first["position"]["y"] + first["height"] + gap <= second["position"]["y"] - gap
        or second["position"]["y"] + second["height"] + gap <= first["position"]["y"] - gap
    )


if __name__ == "__main__":
    unittest.main()
