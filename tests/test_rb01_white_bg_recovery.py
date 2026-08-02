from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "canvas-bridge", ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import detect_current_state  # noqa: E402
import run_controller  # noqa: E402
import white_bg_recovery  # noqa: E402
from white_bg_recovery import (  # noqa: E402
    WhiteBgRecoveryError,
    archive_recompute_artifacts,
    evaluate_rebind_eligibility,
    rollback_recompute_archive,
    sanitize_filename,
    sanitize_filenames,
    scan_white_bg_recovery,
)


ARCHIVE_ID = "20260802T010203Z_1234abcd"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class RecoveryFixture:
    def __init__(self, root: Path) -> None:
        self.repository_root = root / "repo"
        self.workspace = root / "workspace"
        self.artifacts_root = self.workspace / "artifacts"
        self.white_bg = self.workspace / "inputs" / "white_bg"
        self.white_bg.mkdir(parents=True)
        self.remaining = self.white_bg / "白底 正面.jpg"
        self.remaining.write_bytes(b"image")
        self.missing_name = "背面.png"

        self.identity = self.artifacts_root / "product_identity_archive"
        self.style = self.artifacts_root / "style_master"
        self.angle = self.artifacts_root / "angle_inventory"
        self.variables = self.artifacts_root / "variable_configs"
        self.final = self.artifacts_root / "final_prompts"
        self.jobs = self.artifacts_root / "comfyui_jobs"
        self.qc = self.artifacts_root / "qc_reports"

        _write_json(self.identity / "identity.json", {"artifact_type": "product_identity_archive"})
        _write_json(self.style / "style.json", {"artifact_type": "style_master"})
        _write_json(self.angle / "angle.json", {"artifact_type": "angle_inventory"})
        _write_json(self.variables / "main.json", {"artifact_type": "main_variable_config"})
        _write_json(self.variables / "detail.json", {"artifact_type": "detail_variable_config"})
        _write_json(self.jobs / "job.json", {"artifact_type": "comfyui_job"})
        _write_json(self.qc / "qc.json", {"artifact_type": "qc_report"})
        _write_json(
            self.final / "final_prompt_index.json",
            {
                "artifact_type": "final_prompt_index",
                "product_id": "cup",
                "prompt_count": 2,
                "items": [
                    {"config_id": "main_01", "bound_reference": self.missing_name},
                    {"config_id": "detail_01", "bound_reference": self.remaining.name},
                ],
            },
        )
        self.repo_integrity_json = (
            self.repository_root / "reports" / "cup_final_prompt_integrity_report.json"
        )
        self.repo_integrity_md = (
            self.repository_root / "reports" / "cup_final_prompt_integrity_report.md"
        )
        _write_json(self.repo_integrity_json, {"status": "pass"})
        self.repo_integrity_md.write_text("old final prompts", encoding="utf-8")

        self.manifest = {
            "product_id": "cup",
            "batch_type": "single",
            "user_declared_set_product": False,
            "requested_outputs": ["main", "detail", "final_prompts", "qc_reports"],
            "workspace": {
                "root": str(self.workspace),
                "artifacts_root": str(self.artifacts_root),
                "outputs_root": str(self.workspace / "outputs"),
            },
            "inputs": {
                "white_bg_images": [str(self.white_bg)],
                "style_reference_images": [str(self.workspace / "inputs" / "style_refs")],
            },
            "artifacts": {
                "product_identity_archive": [str(self.identity)],
                "style_master": [str(self.style)],
                "angle_inventory": [str(self.angle)],
                "main_variable_configs": [str(self.variables)],
                "detail_variable_configs": [str(self.variables)],
                "final_prompts": [str(self.final)],
                "comfyui_jobs": [str(self.jobs)],
                "qc_reports": [str(self.qc)],
            },
            "outputs": {
                "renders": [str(self.workspace / "outputs" / "renders")],
                "repaired": [str(self.workspace / "outputs" / "repaired")],
            },
        }


class WhiteBgClassificationTest(unittest.TestCase):
    def test_filename_sanitizer_supports_chinese_and_strips_both_separator_styles(self) -> None:
        self.assertEqual("白底 正面.jpg", sanitize_filename(r"D:\batch\白底 正面.jpg"))
        self.assertEqual("侧面（左）.png", sanitize_filename("/tmp/侧面（左）.png"))
        self.assertIsNone(sanitize_filename("a" * 81))
        self.assertIsNone(sanitize_filename("secret-token.jpg"))
        self.assertIsNone(sanitize_filename("bad:name.jpg"))
        self.assertEqual((), sanitize_filenames(("safe.jpg", "密钥.png")))

    def test_classifier_and_eligibility_cover_all_business_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            deleted_reference = fixture.white_bg / fixture.missing_name
            deleted_reference.write_bytes(b"present at batch creation")
            deleted_reference.unlink()
            missing = scan_white_bg_recovery(fixture.manifest)
            self.assertEqual("missing_reference", missing.kind)
            self.assertEqual((fixture.missing_name,), missing.missing_files)
            self.assertEqual((1, 1), (missing.missing_count, missing.remaining_count))
            self.assertTrue(evaluate_rebind_eligibility(missing, 0).eligible)

            (fixture.white_bg / fixture.missing_name).write_bytes(b"restored")
            available = scan_white_bg_recovery(fixture.manifest)
            self.assertEqual("available", available.kind)
            self.assertEqual(
                "missing_files_restored",
                evaluate_rebind_eligibility(available, 0).code,
            )

            for image in fixture.white_bg.iterdir():
                image.unlink()
            unavailable = scan_white_bg_recovery(fixture.manifest)
            self.assertEqual("inputs_unavailable", unavailable.kind)
            self.assertEqual(
                "inputs_unavailable",
                evaluate_rebind_eligibility(unavailable, 0).code,
            )
            rendered = evaluate_rebind_eligibility(missing, 3)
            self.assertEqual("render_outputs_exist", rendered.code)
            self.assertIn("3 张成图", rendered.message)

    def test_missing_unreadable_and_directory_unavailable_are_classified_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            unreadable = fixture.white_bg / fixture.missing_name
            unreadable.write_bytes(b"image")
            real_open = Path.open

            def selective_open(path: Path, *args, **kwargs):
                if path == unreadable:
                    raise OSError("simulated unreadable input")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=selective_open):
                scan = scan_white_bg_recovery(fixture.manifest)
            self.assertEqual("missing_reference", scan.kind)
            self.assertEqual((fixture.missing_name,), scan.missing_files)
            self.assertEqual(1, scan.remaining_count)

            fixture.manifest["inputs"]["white_bg_images"] = [
                str(fixture.workspace / "inputs" / "missing-root")
            ]
            self.assertEqual(
                "inputs_unavailable",
                scan_white_bg_recovery(fixture.manifest).kind,
            )

            fixture.manifest["inputs"]["white_bg_images"] = [str(fixture.white_bg)]
            with mock.patch.object(Path, "iterdir", side_effect=OSError("unreadable")):
                self.assertEqual(
                    "inputs_unavailable",
                    scan_white_bg_recovery(fixture.manifest).kind,
                )

    def test_unsafe_missing_filename_degrades_to_count_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            scan = scan_white_bg_recovery(
                fixture.manifest,
                bound_references=("secret-token.jpg", fixture.remaining.name),
            )
            self.assertEqual("missing_reference", scan.kind)
            self.assertEqual(1, scan.missing_count)
            self.assertEqual((), scan.missing_files)

    def test_filename_sanitizer_enforces_length_limit_after_pattern_match(self) -> None:
        filename = "a" * 78 + ".jpg"

        self.assertEqual(82, len(filename))
        self.assertIsNotNone(
            white_bg_recovery._SAFE_FILENAME_PATTERN.fullmatch(filename)
        )
        self.assertIsNone(sanitize_filename(filename))
        self.assertEqual((), sanitize_filenames(("safe.png", filename)))


class RecoveryArchiveTest(unittest.TestCase):
    def test_archive_rejects_artifacts_root_outside_workspace_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            external_root = Path(temp) / "external-artifacts"
            external_angle = external_root / "angle_inventory"
            _write_json(external_angle / "angle.json", {"artifact_type": "angle_inventory"})
            fixture.manifest["workspace"]["artifacts_root"] = str(external_root)
            for key in (
                "angle_inventory",
                "main_variable_configs",
                "detail_variable_configs",
                "final_prompts",
                "comfyui_jobs",
                "qc_reports",
            ):
                fixture.manifest["artifacts"][key] = []
            fixture.manifest["artifacts"]["angle_inventory"] = [str(external_angle)]

            with self.assertRaisesRegex(WhiteBgRecoveryError, "越出工作区"):
                archive_recompute_artifacts(
                    fixture.manifest,
                    fixture.repository_root,
                    archive_id_factory=lambda: ARCHIVE_ID,
                )

            self.assertTrue(external_angle.is_dir())
            self.assertTrue(fixture.repo_integrity_json.is_file())
            self.assertFalse((external_root / "_superseded").exists())

    def test_rollback_rejects_artifacts_root_outside_workspace_without_moving_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            archived = archive_recompute_artifacts(
                fixture.manifest,
                fixture.repository_root,
                archive_id_factory=lambda: ARCHIVE_ID,
            )
            fixture.manifest["workspace"]["root"] = str(Path(temp) / "other-workspace")

            with self.assertRaisesRegex(WhiteBgRecoveryError, "越出工作区"):
                rollback_recompute_archive(
                    fixture.manifest,
                    fixture.repository_root,
                    archived,
                )

            self.assertFalse(fixture.final.exists())
            self.assertFalse(fixture.repo_integrity_json.exists())
            self.assertTrue(
                (
                    fixture.artifacts_root
                    / "_superseded"
                    / ARCHIVE_ID
                    / "final_prompts"
                ).is_dir()
            )

    def test_archives_derived_artifacts_and_repo_json_in_place_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            result = archive_recompute_artifacts(
                fixture.manifest,
                fixture.repository_root,
                archive_id_factory=lambda: ARCHIVE_ID,
            )

            self.assertEqual(
                {
                    "angle_inventory",
                    "main_variable_configs",
                    "detail_variable_configs",
                    "final_prompts",
                    "comfyui_jobs",
                    "qc_reports",
                    "repo_final_prompt_integrity_report",
                },
                set(result.superseded),
            )
            self.assertEqual(f"artifacts/_superseded/{ARCHIVE_ID}", result.superseded_dir)
            batch_archive = fixture.artifacts_root / "_superseded" / ARCHIVE_ID
            self.assertTrue((batch_archive / "angle_inventory" / "angle.json").is_file())
            self.assertTrue((batch_archive / "variable_configs" / "main.json").is_file())
            self.assertTrue(fixture.identity.is_dir())
            self.assertTrue(fixture.style.is_dir())
            self.assertFalse(fixture.final.exists())
            self.assertFalse(fixture.repo_integrity_json.exists())
            self.assertTrue(
                (
                    fixture.repository_root
                    / "reports"
                    / "_superseded"
                    / ARCHIVE_ID
                    / fixture.repo_integrity_json.name
                ).is_file()
            )
            self.assertTrue(fixture.repo_integrity_md.is_file())

    def test_archive_name_collision_retries_without_touching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            collision_id = "20260802T010203Z_deadbeef"
            (fixture.artifacts_root / "_superseded" / collision_id).mkdir(parents=True)
            identifiers = iter((collision_id, ARCHIVE_ID))
            result = archive_recompute_artifacts(
                fixture.manifest,
                fixture.repository_root,
                archive_id_factory=lambda: next(identifiers),
            )
            self.assertEqual(ARCHIVE_ID, result.archive_id)
            self.assertTrue(
                (fixture.artifacts_root / "_superseded" / ARCHIVE_ID / "final_prompts").is_dir()
            )

    def test_any_oserror_rolls_back_already_moved_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            real_rename = os.rename

            def flaky_rename(source: object, destination: object) -> None:
                if Path(source) == fixture.final:
                    raise OSError("simulated cross-volume or access failure")
                real_rename(source, destination)

            with mock.patch.object(white_bg_recovery.os, "rename", side_effect=flaky_rename):
                with self.assertRaisesRegex(
                    WhiteBgRecoveryError,
                    "归档失败，已恢复原状",
                ):
                    archive_recompute_artifacts(
                        fixture.manifest,
                        fixture.repository_root,
                        archive_id_factory=lambda: ARCHIVE_ID,
                    )

            for source in (
                fixture.angle,
                fixture.variables,
                fixture.final,
                fixture.jobs,
                fixture.qc,
                fixture.repo_integrity_json,
            ):
                self.assertTrue(source.exists(), source)
            self.assertFalse((fixture.artifacts_root / "_superseded" / ARCHIVE_ID).exists())
            self.assertFalse(
                (fixture.repository_root / "reports" / "_superseded" / ARCHIVE_ID).exists()
            )

    def test_archive_then_real_route_and_controller_resume_at_angle_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            manifest_path = fixture.repository_root / "manifests" / "cup.batch_manifest.json"
            _write_json(manifest_path, fixture.manifest)

            archive_recompute_artifacts(
                fixture.manifest,
                fixture.repository_root,
                archive_id_factory=lambda: ARCHIVE_ID,
            )

            input_keys = (
                "white_bg_images",
                "style_reference_images",
                "set_group_images",
                "component_white_bg_images",
            )
            draft_keys = ("product_identity_draft", "style_master_draft")
            artifact_keys = tuple(detect_current_state.ARTIFACT_DEFAULTS)
            output_keys = tuple(detect_current_state.OUTPUT_DEFAULTS)

            def summaries(section: str, keys: tuple[str, ...]) -> dict[str, object]:
                return {
                    key: detect_current_state.summarize_path_values(
                        fixture.repository_root,
                        detect_current_state.values_from_manifest_or_default(
                            fixture.manifest,
                            section,
                            key,
                            "cup",
                        ),
                    )
                    for key in keys
                }

            route = detect_current_state.route_batch(
                "cup",
                manifest_path,
                fixture.manifest,
                summaries("inputs", input_keys),
                summaries("drafts", draft_keys),
                summaries("artifacts", artifact_keys),
                summaries("outputs", output_keys),
            )

            self.assertEqual("needs_angle_inventory", route["current_stage"])
            self.assertEqual(
                ["angle_inventory"],
                run_controller.runnable_steps(route, {"found": False}),
            )

    def test_archive_rejects_source_outside_artifacts_root_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RecoveryFixture(Path(temp))
            external_final = Path(temp) / "external-final-prompts"
            external_file = external_final / "marker.json"
            _write_json(external_file, {"safe": True})
            fixture.manifest["artifacts"]["final_prompts"] = [str(external_final)]

            with self.assertRaises(WhiteBgRecoveryError) as caught:
                archive_recompute_artifacts(
                    fixture.manifest,
                    fixture.repository_root,
                    archive_id_factory=lambda: ARCHIVE_ID,
                )

            self.assertEqual(
                "final_prompts 越出批次派生产物目录",
                str(caught.exception),
            )
            self.assertTrue(external_file.is_file())
            self.assertFalse((fixture.artifacts_root / "_superseded").exists())
            self.assertFalse(
                (fixture.repository_root / "reports" / "_superseded").exists()
            )
            for source in (
                fixture.identity / "identity.json",
                fixture.style / "style.json",
                fixture.angle / "angle.json",
                fixture.variables / "main.json",
                fixture.variables / "detail.json",
                fixture.final / "final_prompt_index.json",
                fixture.jobs / "job.json",
                fixture.qc / "qc.json",
                fixture.repo_integrity_json,
                fixture.repo_integrity_md,
            ):
                self.assertTrue(source.is_file(), source)


if __name__ == "__main__":
    unittest.main()
