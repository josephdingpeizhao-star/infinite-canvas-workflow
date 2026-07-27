from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import workflow_qc_summary  # noqa: E402


CONFIG_IDS = tuple(
    [f"main_{index:02d}" for index in range(1, 7)]
    + [f"detail_{index:02d}" for index in range(1, 9)]
)


class WorkflowQcSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        (self.repo / "reports").mkdir()
        (self.repo / "manifests").mkdir()
        (self.repo / "manifests" / "cup.batch_manifest.json").write_text(
            json.dumps(
                {
                    "product_id": "cup",
                    "user_confirmed_facts": {
                        "main_image_count": 6,
                        "detail_image_count": 8,
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_report(self, **patch) -> Path:
        report = {
            "product_id": "cup",
            "artifact_type": "qc_report",
            "checked_assets": [f"{config_id}.png" for config_id in CONFIG_IDS],
            "results": [
                {
                    "affected_asset": f"{config_id}.png",
                    "check_item": "identity",
                    "status": "pass",
                    "notes": "safe",
                }
                for config_id in CONFIG_IDS
            ],
            "issues": [],
            "repair_targets": [],
            "notes": "do not expose report notes",
            **patch,
        }
        path = self.repo / "reports" / "cup_qc_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return path

    def test_fixed_three_state_rule_uses_issues_before_needs_review(self) -> None:
        self._write_report(
            results=[
                {"affected_asset": f"{config_id}.png", "check_item": "identity", "status": "pass", "notes": "safe"}
                for config_id in CONFIG_IDS
            ]
            + [
                {"affected_asset": "detail_03.png", "check_item": "size_ratio", "status": "needs_review", "notes": "private"},
                {"affected_asset": "detail_06.png", "check_item": "product_angle", "status": "needs_review", "notes": "private"},
                {"check_item": "batch_platform_readiness", "status": "fail", "notes": "batch-only"},
            ],
            issues=[
                {
                    "affected_asset": "detail_06.png",
                    "category": "product_angle",
                    "description": "private issue body",
                    "issue_id": "issue-1",
                    "severity": "needs_review",
                }
            ],
        )

        payload = workflow_qc_summary.build_qc_summary(self.repo, "cup")
        by_id = {item["configId"]: item for item in payload["images"]}

        self.assertEqual("needs_review", by_id["detail_03"]["status"])
        self.assertEqual(0, by_id["detail_03"]["issueCount"])
        self.assertEqual("fail", by_id["detail_06"]["status"])
        self.assertEqual(1, by_id["detail_06"]["issueCount"])
        self.assertEqual("pass", by_id["main_01"]["status"])

    def test_categories_are_deterministic_and_payload_is_summary_only(self) -> None:
        self._write_report(
            issues=[
                {"affected_asset": "main_02.png", "category": "text", "description": "secret-a", "issue_id": "a", "severity": "major"},
                {"affected_asset": "main_02.png", "category": "composition", "description": "secret-b", "issue_id": "b", "severity": "major"},
                {"affected_asset": "main_02.png", "category": "text", "description": "secret-c", "issue_id": "c", "severity": "critical"},
                {"affected_asset": "main_02.png", "category": "realism", "description": "secret-d", "issue_id": "d", "severity": "major"},
                {"affected_asset": "main_02.png", "category": "ai_artifacts", "description": "secret-e", "issue_id": "e", "severity": "major"},
            ],
        )

        payload = workflow_qc_summary.build_qc_summary(self.repo, "cup")
        main_02 = next(item for item in payload["images"] if item["configId"] == "main_02")

        self.assertEqual(14, len(payload["images"]))
        self.assertEqual(["text", "ai_artifacts", "composition"], main_02["topCategories"])
        self.assertEqual({"configId", "status", "issueCount", "topCategories"}, set(main_02))
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))

    def test_missing_report_returns_not_found_without_fallback_to_workspace(self) -> None:
        with self.assertRaises(workflow_qc_summary.QcSummaryNotFound):
            workflow_qc_summary.build_qc_summary(self.repo, "cup")

    def test_mismatched_or_noncanonical_report_is_rejected(self) -> None:
        self._write_report(product_id="other")
        with self.assertRaises(workflow_qc_summary.QcSummaryInvalid):
            workflow_qc_summary.build_qc_summary(self.repo, "cup")
