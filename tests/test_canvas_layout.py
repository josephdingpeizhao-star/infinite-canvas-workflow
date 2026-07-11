from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import layout_store  # noqa: E402
import projector  # noqa: E402


GRAPH = json.loads((ROOT / "manifests" / "workflow_graph.template.json").read_text(encoding="utf-8"))


def make_batch() -> dict:
    return {
        "product_id": "p1",
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
    }


class BuildLayoutTest(unittest.TestCase):
    def test_filters_other_nodes_and_strips_prefix(self) -> None:
        canvas_nodes = [
            {"id": "wf:p1:stage_qc", "position": {"x": 1.5, "y": 2.5}, "width": 111, "height": 222},
            {"id": "wf:p1:art_qc_reports", "position": {"x": 3, "y": 4}},
            {"id": "wf:other:stage_qc", "position": {"x": 9, "y": 9}},
            {"id": "wfimg:demo", "position": {"x": 9, "y": 9}},
        ]
        layout = layout_store.build_layout("p1", "g1", canvas_nodes, {"x": 10, "y": 20, "k": 0.5})
        self.assertEqual({"stage_qc", "art_qc_reports"}, set(layout["nodes"]))
        self.assertEqual({"x": 1.5, "y": 2.5}, layout["nodes"]["stage_qc"]["position"])
        self.assertEqual(111, layout["nodes"]["stage_qc"]["width"])
        self.assertEqual({"x": 10, "y": 20, "k": 0.5}, layout["viewport"])
        self.assertEqual("canvas_layout", layout["artifact_type"])
        self.assertEqual(layout_store.LAYOUT_VERSION, layout["layout_version"])

    def test_viewport_omitted_when_missing(self) -> None:
        layout = layout_store.build_layout("p1", "g1", [], None)
        self.assertNotIn("viewport", layout)


class ProjectWithLayoutTest(unittest.TestCase):
    def test_delete_covers_condition_filtered_nodes(self) -> None:
        batch = make_batch()
        batch["requested_outputs"] = ["main"]
        ops = projector.project_batch(GRAPH, batch)
        delete_ids = set(ops[0]["ids"])
        added_ids = {op["id"] for op in ops if op["type"] == "add_node"}
        self.assertIn("wf:p1:stage_qc", delete_ids)
        self.assertIn("wf:p1:stage_detail_variable_config", delete_ids)
        self.assertNotIn("wf:p1:stage_qc", added_ids)
        self.assertTrue(added_ids.issubset(delete_ids), "every re-added node must first be deleted")

    def test_layout_overrides_position_and_size(self) -> None:
        batch = make_batch()
        layout = {
            "artifact_type": "canvas_layout",
            "layout_version": 1,
            "nodes": {"stage_product_identity": {"position": {"x": 999, "y": 888}, "width": 500}},
        }
        ops = projector.project_batch(GRAPH, batch, layout=layout)
        adds = {op["id"]: op for op in ops if op["type"] == "add_node"}
        moved = adds["wf:p1:stage_product_identity"]
        self.assertEqual({"x": 999, "y": 888}, moved["position"])
        self.assertEqual(500, moved["width"])
        untouched = adds["wf:p1:in_white_bg"]
        self.assertEqual(80, untouched["position"]["x"])


class SaveLoadTest(unittest.TestCase):
    def test_roundtrip_and_missing(self) -> None:
        layout = layout_store.build_layout("p1", "g1", [{"id": "wf:p1:stage_qc", "position": {"x": 1, "y": 2}}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p1.canvas_layout.json"
            layout_store.save_layout(path, layout)
            loaded = layout_store.load_layout(path)
            self.assertEqual(layout, loaded)
            self.assertIsNone(layout_store.load_layout(Path(tmp) / "missing.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{\"artifact_type\": \"something_else\"}", encoding="utf-8")
            with self.assertRaises(ValueError):
                layout_store.load_layout(bad)


if __name__ == "__main__":
    unittest.main()
