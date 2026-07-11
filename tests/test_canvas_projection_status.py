from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import detect_current_state  # noqa: E402
import projector  # noqa: E402


GRAPH = json.loads((ROOT / "manifests" / "workflow_graph.template.json").read_text(encoding="utf-8"))

ALL_REQUESTED = ["main", "detail", "final_prompts", "qc_reports"]

KEY_TYPES = {
    "asset_manifest": "asset_manifest",
    "product_identity_archive": "product_identity_archive",
    "style_master": "style_master",
    "angle_inventory": "angle_inventory",
    "main_variable_configs": "main_variable_config",
    "detail_variable_configs": "detail_variable_config",
    "set_product_identity": "set_product_identity",
    "set_angle_layout_inventory": "set_angle_layout_inventory",
    "final_prompts": "final_prompt",
    "comfyui_jobs": "comfyui_job",
    "qc_reports": "qc_report",
}


def summary(file_count: int = 0, typed: dict | None = None) -> dict:
    return {"paths": [], "file_count": file_count, "typed_artifact_counts": dict(typed or {})}


def make_batch() -> dict:
    return {
        "product_id": "status_test",
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": list(ALL_REQUESTED),
        "notes": "",
    }


def make_route(produced: tuple[str, ...] = (), *, white_bg: int = 0, style_refs: int = 0, renders: int = 0):
    batch = make_batch()
    inputs = {
        "white_bg_images": summary(white_bg),
        "style_reference_images": summary(style_refs),
        "set_group_images": summary(),
        "component_white_bg_images": summary(),
    }
    drafts = {"product_identity_draft": summary(), "style_master_draft": summary()}
    artifacts = {
        key: summary(1 if key in produced else 0, {KEY_TYPES[key]: 1} if key in produced else None)
        for key in KEY_TYPES
    }
    outputs = {"renders": summary(renders), "repaired": summary()}
    route = detect_current_state.route_batch(
        "status_test",
        ROOT / "manifests" / "status_test.batch_manifest.json",
        batch,
        inputs,
        drafts,
        artifacts,
        outputs,
    )
    return batch, route


class NodeRuntimeViewTest(unittest.TestCase):
    def test_blocked_identity_shows_error_with_reason(self) -> None:
        batch, route = make_route()
        view = projector.node_runtime_view(GRAPH, batch, route)
        entry = view["stage_product_identity"]
        self.assertEqual("error", entry["status"])
        self.assertIn("No product source inputs", entry["errorDetails"])
        self.assertEqual("idle", view["art_product_identity"]["status"])

    def test_next_stage_marked_with_arrow(self) -> None:
        batch, route = make_route(white_bg=2, style_refs=1)
        view = projector.node_runtime_view(GRAPH, batch, route)
        entry = view["stage_product_identity"]
        self.assertEqual("idle", entry["status"])
        self.assertTrue(entry["title"].startswith("▶ "), entry["title"])
        self.assertEqual("success", view["in_white_bg"]["status"])
        self.assertIn("files: 2", view["in_white_bg"]["content"])

    def test_produced_artifact_and_stage_turn_success(self) -> None:
        batch, route = make_route(("product_identity_archive",), white_bg=2, style_refs=1)
        view = projector.node_runtime_view(GRAPH, batch, route)
        self.assertEqual("success", view["art_product_identity"]["status"])
        self.assertEqual("success", view["stage_product_identity"]["status"])
        self.assertTrue(view["stage_style_master"]["title"].startswith("▶ "), view["stage_style_master"]["title"])

    def test_full_pipeline_with_renders_and_integrity(self) -> None:
        produced = tuple(key for key in KEY_TYPES if key not in {"asset_manifest", "set_product_identity", "set_angle_layout_inventory"})
        batch, route = make_route(produced, white_bg=2, style_refs=1, renders=2)
        integrity = {"found": True, "path": "x", "status": "pass", "render_blocked": False}
        view = projector.node_runtime_view(GRAPH, batch, route, integrity)
        self.assertEqual("success", view["gate_final_prompt_integrity"]["status"])
        self.assertEqual("success", view["stage_render"]["status"])
        self.assertIn("renders: 2", view["stage_render"]["content"])
        self.assertEqual("success", view["stage_qc"]["status"])
        self.assertEqual("success", view["out_renders"]["status"])

    def test_runtime_update_ops_only_for_changes(self) -> None:
        batch, route_before = make_route(white_bg=2, style_refs=1)
        view_before = projector.node_runtime_view(GRAPH, batch, route_before)
        batch, route_after = make_route(("product_identity_archive",), white_bg=2, style_refs=1)
        view_after = projector.node_runtime_view(GRAPH, batch, route_after)
        ops = projector.runtime_update_ops("status_test", view_before, view_after)
        changed_ids = {op["id"] for op in ops}
        self.assertIn("wf:status_test:stage_product_identity", changed_ids)
        self.assertIn("wf:status_test:art_product_identity", changed_ids)
        self.assertIn("wf:status_test:stage_style_master", changed_ids)
        self.assertLess(len(ops), 8, sorted(changed_ids))


if __name__ == "__main__":
    unittest.main()
