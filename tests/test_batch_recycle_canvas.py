from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_recycle_canvas import (  # noqa: E402
    batch_canvas_node_ids,
    clear_batch_canvas_nodes,
)
import ic_client  # noqa: E402


class FakeCanvas:
    def __init__(self, nodes):
        self.nodes = nodes
        self.state = {"nodes": nodes}
        self.ops = []

    def call_tool(self, name):
        if name != "canvas_get_state":
            raise AssertionError(name)
        return self.state

    def apply_ops(self, ops):
        self.ops.append(ops)
        return 1


class BatchRecycleCanvasTests(unittest.TestCase):
    def test_known_batch_prefixes_are_selected(self) -> None:
        nodes = [
            {"id": "wf:cup:stage_render"},
            {"id": "wfedit:cup:batch"},
            {"id": "wfrun:cup:batch"},
            {"id": "wflog:cup:events"},
            {"id": "wfprod-receiving:cup"},
            {"id": "wfprod-output:cup:main_01"},
            {"id": "wfprod-repaired:cup:detail_01"},
        ]
        self.assertEqual(
            [node["id"] for node in nodes],
            batch_canvas_node_ids(nodes, "cup"),
        )

    def test_output_metadata_is_durable_ownership_proof(self) -> None:
        nodes = [
            {
                "id": "legacy-output",
                "metadata": {"workflowProductionOutput": {"batchId": "cup"}},
            }
        ]
        self.assertEqual(["legacy-output"], batch_canvas_node_ids(nodes, "cup"))

    def test_completed_batch_information_card_is_selected(self) -> None:
        nodes = [
            {
                "id": "card",
                "type": "batch-info",
                "metadata": {
                    "batchIntake": {
                        "status": "completed",
                        "receipt": {"batchId": "cup"},
                    }
                },
            }
        ]
        self.assertEqual(["card"], batch_canvas_node_ids(nodes, "cup"))

    def test_other_batch_with_common_prefix_is_protected(self) -> None:
        nodes = [
            {"id": "wfprod-output:cup_extra:main_01"},
            {"id": "wfprod-receiving:cup_extra"},
            {"id": "wf:cup_extra:stage_render"},
        ]
        self.assertEqual([], batch_canvas_node_ids(nodes, "cup"))

    def test_user_nodes_and_shared_machine_are_protected(self) -> None:
        nodes = [
            {"id": "user-text", "type": "text"},
            {
                "id": "machine",
                "type": "workflow",
                "metadata": {"workflowProduction": {"batchId": "cup"}},
            },
            {"id": "style-image", "type": "image"},
        ]
        self.assertEqual([], batch_canvas_node_ids(nodes, "cup"))

    def test_duplicate_ids_are_returned_once(self) -> None:
        nodes = [
            {"id": "wfprod-output:cup:main_01"},
            {"id": "wfprod-output:cup:main_01"},
        ]
        self.assertEqual(
            ["wfprod-output:cup:main_01"],
            batch_canvas_node_ids(nodes, "cup"),
        )

    def test_clear_uses_one_delete_operation(self) -> None:
        client = FakeCanvas(
            [
                {"id": "wfprod-output:cup:main_01"},
                {"id": "user", "type": "text"},
            ]
        )
        ids = clear_batch_canvas_nodes(client, "cup")
        self.assertEqual(["wfprod-output:cup:main_01"], ids)
        self.assertEqual(
            [[{"type": "delete_node", "ids": ids}]],
            client.ops,
        )

    def test_empty_clear_is_idempotent_and_writes_no_operation(self) -> None:
        client = FakeCanvas([{"id": "user", "type": "text"}])
        self.assertEqual([], clear_batch_canvas_nodes(client, "cup"))
        self.assertEqual([], client.ops)
        empty = FakeCanvas([])
        self.assertEqual([], clear_batch_canvas_nodes(empty, "cup"))
        self.assertEqual([], empty.ops)
        for invalid_state in (
            None,
            {},
            {"nodes": None},
            {"nodes": {}},
        ):
            with self.subTest(invalid_state=invalid_state):
                malformed = FakeCanvas([])
                malformed.state = invalid_state
                with self.assertRaises(ic_client.CanvasAgentError):
                    clear_batch_canvas_nodes(malformed, "cup")
                self.assertEqual([], malformed.ops)


if __name__ == "__main__":
    unittest.main()
