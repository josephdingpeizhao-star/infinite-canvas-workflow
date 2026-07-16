from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import detect_current_state  # noqa: E402


ALL_REQUESTED = ["main", "detail", "final_prompts", "qc_reports"]

UPSTREAM_PRODUCED = {
    "product_identity_archive": "product_identity_archive",
    "style_master": "style_master",
    "angle_inventory": "angle_inventory",
    "main_variable_configs": "main_variable_config",
    "detail_variable_configs": "detail_variable_config",
    "final_prompts": "final_prompt",
    "comfyui_jobs": "comfyui_job",
}

EMPTY_KEYS = ["asset_manifest", "set_product_identity", "set_angle_layout_inventory"]


def summary(file_count: int = 0, typed: dict | None = None) -> dict:
    return {"paths": [], "file_count": file_count, "typed_artifact_counts": dict(typed or {})}


def route_with_qc_summary(qc_summary: dict) -> dict:
    manifest = {
        "batch_type": "single",
        "user_declared_set_product": False,
        "requested_outputs": list(ALL_REQUESTED),
        "notes": "",
    }
    inputs = {
        "white_bg_images": summary(2),
        "style_reference_images": summary(1),
        "set_group_images": summary(),
        "component_white_bg_images": summary(),
    }
    drafts = {"product_identity_draft": summary(), "style_master_draft": summary()}
    artifacts = {key: summary(1, {typed: 1}) for key, typed in UPSTREAM_PRODUCED.items()}
    artifacts.update({key: summary() for key in EMPTY_KEYS})
    artifacts["qc_reports"] = qc_summary
    outputs = {"renders": summary(2), "repaired": summary()}
    return detect_current_state.route_batch(
        "qc_gate_test",
        ROOT / "manifests" / "qc_gate_test.batch_manifest.json",
        manifest,
        inputs,
        drafts,
        artifacts,
        outputs,
    )


class QcGateRoutingTest(unittest.TestCase):
    """Regression: the final prompt integrity gate writes its reports into the
    external qc_reports/ folder; that output must not count as completed QC."""

    def test_integrity_report_alone_does_not_complete_qc(self) -> None:
        qc_summary = summary(2, {"final_prompt_integrity_report": 1})
        route = route_with_qc_summary(qc_summary)
        self.assertNotIn("qc_reports", route["available_artifacts"])
        self.assertEqual("needs_qc_reports", route["current_stage"])
        self.assertEqual("qc-inspector", route["next_required_skill"])

    def test_prompts_only_integrity_report_does_not_complete_qc(self) -> None:
        report = {
            "artifact_type": "final_prompt_integrity_report",
            "mode": "prompts-only",
            "status": "pass",
            "render_blocked": False,
        }
        qc_summary = summary(2, {report["artifact_type"]: 1})
        route = route_with_qc_summary(qc_summary)

        self.assertNotIn("qc_reports", route["available_artifacts"])
        self.assertEqual("needs_qc_reports", route["current_stage"])

    def test_untyped_files_in_qc_folder_do_not_complete_qc(self) -> None:
        qc_summary = summary(1)
        route = route_with_qc_summary(qc_summary)
        self.assertNotIn("qc_reports", route["available_artifacts"])
        self.assertEqual("qc-inspector", route["next_required_skill"])

    def test_typed_qc_report_completes_qc(self) -> None:
        qc_summary = summary(3, {"qc_report": 1, "final_prompt_integrity_report": 1})
        route = route_with_qc_summary(qc_summary)
        self.assertIn("qc_reports", route["available_artifacts"])
        self.assertEqual("ready", route["current_stage"])
        self.assertIsNone(route["next_required_skill"])


if __name__ == "__main__":
    unittest.main()
