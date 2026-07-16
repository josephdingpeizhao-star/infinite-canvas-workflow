from __future__ import annotations

import json
import subprocess
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
from validate_final_prompt_integrity import (  # noqa: E402
    build_prompts_only_report,
    write_report_files,
)


class PromptsOnlyIntegrityTest(unittest.TestCase):
    def _report(self, root: Path):
        bundle = build_final_prompt_bundle(root)
        report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)
        return bundle, report

    def test_prompts_only_valid_bundle_passes_and_double_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle, report = self._report(Path(tmp))
            external_json = bundle.qc_dir / "final_prompt_integrity_report.json"
            external_md = external_json.with_suffix(".md")
            repo_dir = Path(tmp) / "repo_reports"
            paths = write_report_files(
                report=report,
                output_report=external_json,
                output_markdown=external_md,
                repo_report_dir=repo_dir,
                repo_report_prefix=None,
            )

            self.assertEqual("pass", report["status"])
            self.assertFalse(report["render_blocked"])
            self.assertEqual("prompts-only", report["mode"])
            self.assertEqual(14, report["checked_prompt_count"])
            self.assertTrue(all(path and Path(path).is_file() for path in paths))
            self.assertEqual(report, read_json(external_json))
            self.assertEqual(report, read_json(repo_dir / "fixture_product_final_prompt_integrity_report.json"))
            skipped = {item["check"]: item["reason"] for item in report["skipped_checks"]}
            self.assertIn("comfyui_job_manifest", skipped)
            self.assertIn("legacy_content_heuristics", skipped)
            self.assertIn("避免真实批次误报", skipped["legacy_content_heuristics"])
            self.assertIn("legacy_compiler_literal_scan", skipped)

    def test_prompts_only_missing_prompt_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            bundle.prompt_path("main_03").unlink()
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertEqual("fail", report["status"])
            self.assertTrue(report["render_blocked"])
            self.assertTrue(any(item["issue_id"] == "final_prompt_unreadable_main_03" for item in report["issues"]))

    def test_prompts_only_index_count_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            index = read_json(bundle.index_path)
            index["prompt_count"] = 13
            write_json(bundle.index_path, index)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertTrue(report["render_blocked"])
            self.assertTrue(any(item["issue_id"] == "final_prompt_index_count_mismatch" for item in report["issues"]))

    def test_prompts_only_handheld_counts_match_manifest_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)
            self.assertEqual(
                {
                    "expected_main": 2,
                    "expected_detail": 1,
                    "variable_config_main": 2,
                    "variable_config_detail": 1,
                    "final_prompt_main": 2,
                    "final_prompt_detail": 1,
                },
                report["handheld_count_summary"],
            )

            manifest = read_json(bundle.manifest_path)
            manifest["notes"] = manifest["notes"].replace("主图手持数量: 2", "主图手持数量: 3")
            write_json(bundle.manifest_path, manifest)
            failed = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)
            self.assertTrue(any(item["issue_id"] == "handheld_count_main_mismatch" for item in failed["issues"]))

    def test_prompts_only_rejects_source_hash_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            source = read_json(bundle.main_config_path)
            source["notes"] = "changed after compilation"
            write_json(bundle.main_config_path, source)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertTrue(report["render_blocked"])
            self.assertTrue(any(item["issue_id"].startswith("variable_config_source_hash_mismatch_") for item in report["issues"]))

    def test_prompts_only_rejects_resolved_hash_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            prompt_path = bundle.prompt_path("detail_04")
            prompt = read_json(prompt_path)
            prompt["variable_config"]["resolved_variable_config_sha256"] = "0" * 64
            write_json(prompt_path, prompt)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertTrue(report["render_blocked"])
            self.assertTrue(any(item["issue_id"] == "resolved_variable_config_hash_mismatch_detail_04" for item in report["issues"]))

    def test_prompts_only_schema_violation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            prompt_path = bundle.prompt_path("main_01")
            prompt = read_json(prompt_path)
            del prompt["negative_prompt"]
            write_json(prompt_path, prompt)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertTrue(any(item["issue_id"] == "final_prompt_schema_invalid_main_01" for item in report["issues"]))

    def test_prompts_only_ratio_or_confirmed_height_mismatch_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            main_path = bundle.prompt_path("main_01")
            main = read_json(main_path)
            main["final_prompt"] = main["final_prompt"].replace("画布比例固定为 1:1。", "")
            write_json(main_path, main)
            detail_path = bundle.prompt_path("detail_01")
            detail = read_json(detail_path)
            detail["final_prompt"] = detail["final_prompt"].replace("25 厘米", "30 厘米")
            write_json(detail_path, detail)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            issue_ids = {item["issue_id"] for item in report["issues"]}
            self.assertIn("canvas_ratio_literal_mismatch_main_01", issue_ids)
            self.assertIn("confirmed_height_literal_mismatch_detail_01", issue_ids)

    def test_prompts_only_unicode_replacement_character_blocks_without_prompt_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            prompt_path = bundle.prompt_path("detail_03")
            prompt = read_json(prompt_path)
            prompt["final_prompt"] += " UNIQUE_PRIVATE_PROMPT \ufffd"
            write_json(prompt_path, prompt)
            report = build_prompts_only_report(batch_manifest_path=bundle.manifest_path)

            self.assertTrue(any(item["issue_id"] == "unicode_replacement_character_detail_03" for item in report["issues"]))
            self.assertNotIn("UNIQUE_PRIVATE_PROMPT", json.dumps(report, ensure_ascii=False))

    def test_default_mode_keeps_missing_job_manifest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_final_prompt_bundle(Path(tmp))
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "validate_final_prompt_integrity.py"),
                    "--batch-manifest",
                    str(bundle.manifest_path),
                    "--repo-report-dir",
                    str(Path(tmp) / "repo_reports"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            payload = json.loads(result.stdout)
            self.assertEqual("error", payload["status"])
            self.assertIn("comfyui_job_manifest.json", payload["message"])
            self.assertFalse((bundle.qc_dir / "final_prompt_integrity_report.json").exists())


if __name__ == "__main__":
    unittest.main()
