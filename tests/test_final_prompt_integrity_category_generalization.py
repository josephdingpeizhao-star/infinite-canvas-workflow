from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for extra in (SCRIPTS, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from final_prompt_integrity_fixtures import (  # noqa: E402
    build_final_prompt_bundle,
    read_json,
    write_json,
)
from validate_final_prompt_integrity import build_prompts_only_report  # noqa: E402


STRUCTURED_FACTS = {
    "product_type": "水壶",
    "height_cm": 25,
    "handheld_main": 2,
    "handheld_detail": 1,
    "allow_clear_water": True,
    "forbid_pouring_and_heating": True,
    "missing_d_no_retake": True,
}


class FinalPromptIntegrityCategoryGeneralizationTest(unittest.TestCase):
    def test_structured_facts_are_preferred_with_empty_or_conflicting_notes(self) -> None:
        notes_values = (
            "",
            "用户确认产品类型: 水壶 | 用户确认高度厘米: 30 | 主图手持数量: 3 | 详情图手持数量: 2",
        )
        for notes in notes_values:
            with self.subTest(notes=notes), tempfile.TemporaryDirectory() as tmp:
                bundle = build_final_prompt_bundle(Path(tmp))
                manifest = read_json(bundle.manifest_path)
                manifest["notes"] = notes
                manifest["user_confirmed_facts"] = dict(STRUCTURED_FACTS)
                write_json(bundle.manifest_path, manifest)

                report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

                self.assertEqual("pass", report["status"])
                self.assertFalse(report["render_blocked"])
                self.assertEqual(2, report["handheld_count_summary"]["expected_main"])
                self.assertEqual(1, report["handheld_count_summary"]["expected_detail"])

    def test_invalid_structured_facts_never_fall_back_to_valid_notes(self) -> None:
        invalid_values = (
            None,
            {key: value for key, value in STRUCTURED_FACTS.items() if key != "height_cm"},
            {**STRUCTURED_FACTS, "unexpected": "value"},
            {**STRUCTURED_FACTS, "height_cm": True},
        )
        for facts in invalid_values:
            with self.subTest(facts=facts), tempfile.TemporaryDirectory() as tmp:
                bundle = build_final_prompt_bundle(Path(tmp))
                manifest = read_json(bundle.manifest_path)
                manifest["user_confirmed_facts"] = facts
                write_json(bundle.manifest_path, manifest)

                report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)
                issue_ids = {item["issue_id"] for item in report["issues"]}

                self.assertEqual("fail", report["status"])
                self.assertTrue(report["render_blocked"])
                self.assertIn("user_confirmed_facts_invalid", issue_ids)
                self.assertNotIn("confirmed_height_missing_from_manifest_notes", issue_ids)

    def test_legacy_notes_remain_supported_without_structured_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            manifest = read_json(bundle.manifest_path)
            self.assertNotIn("user_confirmed_facts", manifest)

            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertEqual("pass", report["status"])
            self.assertEqual(2, report["handheld_count_summary"]["expected_main"])
            self.assertEqual(1, report["handheld_count_summary"]["expected_detail"])

    def test_ratio_phrase_allows_optional_whitespace_for_main_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            replacements = (
                ("main_01", "画布比例固定为 1:1", "画布比例固定为1:1"),
                ("detail_01", "画布比例固定为 3:4", "画布比例固定为3:4"),
            )
            for config_id, old, new in replacements:
                path = bundle.prompt_path(config_id)
                prompt = read_json(path)
                prompt["final_prompt"] = prompt["final_prompt"].replace(old, new)
                write_json(path, prompt)

            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertEqual("pass", report["status"])
            self.assertFalse(any(item["issue_id"].startswith("canvas_ratio_") for item in report["issues"]))

    def test_ratio_values_without_required_phrase_still_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            replacements = (
                ("main_01", "画布比例固定为 1:1", "输出比例为 1:1"),
                ("detail_01", "画布比例固定为 3:4", "输出比例为 3:4"),
            )
            for config_id, old, new in replacements:
                path = bundle.prompt_path(config_id)
                prompt = read_json(path)
                prompt["final_prompt"] = prompt["final_prompt"].replace(old, new)
                write_json(path, prompt)

            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)
            issue_ids = {item["issue_id"] for item in report["issues"]}

            self.assertEqual("fail", report["status"])
            self.assertIn("canvas_ratio_literal_mismatch_main_01", issue_ids)
            self.assertIn("canvas_ratio_literal_mismatch_detail_01", issue_ids)


if __name__ == "__main__":
    unittest.main()
