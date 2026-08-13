from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
for extra in (BRIDGE, SCRIPTS, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_type_gate  # noqa: E402
import final_prompt_integrity_fixtures as single_fixtures  # noqa: E402
import run_controller  # noqa: E402
import test_st03c_set_final_prompts as set_prompt_fixtures  # noqa: E402
import validate_final_prompt_integrity as integrity_validator  # noqa: E402
import workflow_production_http_server as production_http  # noqa: E402
from executor_contract import ExecutorExecutionError  # noqa: E402
from render_task_assembler import (  # noqa: E402
    RenderTaskAssemblyError,
    assemble_render_tasks,
)
from white_bg_recovery import (  # noqa: E402
    WhiteBgRecoveryError,
    WhiteBgScan,
    allows_rebind_recompute,
    evaluate_rebind_eligibility,
    scan_white_bg_recovery,
    set_reference_filenames,
)
from workflow_production_service import WorkflowProductionService  # noqa: E402


SET_BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)
SINGLE_REPORT_SHA256 = "e49a889daa73217db8e8e34438fc02408b7b49cae62065137d3ed727a324a467"
SET_REPORT_SHA256 = "4460a6aededcc9ca85a4d76ab231b1d5dcabdbae7bdab265dffe4c80b8782afe"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _issue_ids(report: dict[str, Any]) -> set[str]:
    return {str(issue["issue_id"]) for issue in report["issues"]}


def _normalized_report_bytes(report: dict[str, Any], temporary_root: Path) -> bytes:
    root_text = str(temporary_root)

    def normalize(value: object, *, key: str | None = None) -> object:
        if key == "checked_at":
            return "<TIMESTAMP>"
        if isinstance(value, dict):
            return {
                item_key: normalize(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return value.replace(root_text, "<TMP>")
        return value

    normalized = normalize(report)
    return (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class SetRenderFixture:
    def __init__(
        self,
        root: Path,
        *,
        duplicate_component: bool = False,
        rejected_component: bool = False,
    ) -> None:
        self.product_id = "st04a-set-render"
        self.workspace = root / "workspace"
        self.inputs_root = self.workspace / "inputs"
        self.white_bg_dir = self.inputs_root / "white_bg"
        self.group_dir = self.inputs_root / "set_group"
        self.component_dir = self.inputs_root / "components"
        self.artifacts_root = self.workspace / "artifacts"
        self.final_dir = self.artifacts_root / "final_prompts"
        self.layout_dir = self.artifacts_root / "angle_inventory"
        self.layout_path = self.layout_dir / "set_angle_layout_inventory.json"
        self.outputs_root = self.workspace / "outputs"
        self.renders_dir = self.outputs_root / "renders"
        for directory in (
            self.white_bg_dir,
            self.group_dir,
            self.component_dir,
            self.final_dir,
            self.layout_dir,
            self.outputs_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.legacy_white = self.white_bg_dir / "single-only.jpg"
        self.group_paths = (
            self.group_dir / "group_01.jpg",
            self.group_dir / "group_02.png",
        )
        self.component_paths = (
            self.component_dir / "component_01.jpg",
            self.component_dir / "component_02.png",
            self.component_dir / "component_03.webp",
        )
        for path in (self.legacy_white, *self.group_paths, *self.component_paths):
            path.write_bytes(f"offline:{path.name}".encode("utf-8"))

        component_two_admission = (
            "不适合入库，需重拍"
            if rejected_component
            else "合格，可进入对应机位与编排槽位"
        )
        layouts: list[dict[str, object]] = [
            {
                "layout_id": "layout_001",
                "image_index": 1,
                "file_name": self.group_paths[0].name,
                "is_set_group": True,
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
            {
                "layout_id": "layout_002",
                "image_index": 2,
                "file_name": self.group_paths[1].name,
                "is_set_group": True,
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
            {
                "layout_id": "layout_005",
                "image_index": 5,
                "file_name": self.component_paths[2].name,
                "is_set_group": False,
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
            {
                "layout_id": "layout_003",
                "image_index": 3,
                "file_name": self.component_paths[0].name,
                "is_set_group": False,
                "admission_result": "合格，可进入对应机位与编排槽位",
            },
            {
                "layout_id": "layout_004",
                "image_index": 4,
                "file_name": self.component_paths[1].name,
                "is_set_group": False,
                "admission_result": component_two_admission,
            },
        ]
        if duplicate_component:
            layouts.append(
                {
                    "layout_id": "layout_006",
                    "image_index": 6,
                    "file_name": self.component_paths[0].name,
                    "is_set_group": False,
                    "admission_result": "不适合入库，需重拍",
                }
            )
        _write_json(
            self.layout_path,
            {
                "product_id": self.product_id,
                "artifact_type": "set_angle_layout_inventory",
                "layouts": layouts,
                "notes": "offline fixture",
            },
        )

        items: list[dict[str, object]] = []
        for config_id, output_type in (("main_01", "main"), ("detail_01", "detail")):
            prompt_path = self.final_dir / f"{config_id}_final_prompt.json"
            _write_json(
                prompt_path,
                {
                    "product_id": self.product_id,
                    "artifact_type": "final_prompt",
                    "uses_upstream_prompt_files_as_visual_requirements": False,
                    "variable_config": {
                        "config_id": config_id,
                        "output_type": output_type,
                    },
                    "final_prompt": f"{config_id} 离线套装渲染提示词。",
                    "negative_prompt": "禁止改变套装组成、结构与颜色。",
                },
            )
            items.append(
                {
                    "config_id": config_id,
                    "output_type": output_type,
                    "final_prompt_path": str(prompt_path),
                    "bound_reference": self.group_paths[1].name,
                }
            )
        self.index_path = self.final_dir / "final_prompt_index.json"
        _write_json(
            self.index_path,
            {
                "product_id": self.product_id,
                "artifact_type": "final_prompt_index",
                "prompt_count": len(items),
                "uses_upstream_prompt_files_as_visual_requirements": False,
                "items": items,
            },
        )
        self.manifest: dict[str, Any] = {
            "product_id": self.product_id,
            "batch_type": "set",
            "user_declared_set_product": True,
            "user_confirmed_facts": {
                "main_image_count": 1,
                "detail_image_count": 1,
            },
            "workspace": {
                "root": str(self.workspace),
                "artifacts_root": str(self.artifacts_root),
                "outputs_root": str(self.outputs_root),
            },
            "inputs": {
                "white_bg_images": [str(self.white_bg_dir)],
                "set_group_images": [str(self.group_dir)],
                "component_white_bg_images": [str(self.component_dir)],
            },
            "artifacts": {
                "set_angle_layout_inventory": [str(self.layout_dir)],
                "final_prompts": [str(self.final_dir)],
            },
            "outputs": {
                "renders": [str(self.renders_dir)],
                "repaired": [str(self.outputs_root / "repaired")],
            },
        }


class St04aGateTests(unittest.TestCase):
    def test_g1_gate_matrix_opens_eight_set_steps_and_keeps_qc_closed(self) -> None:
        steps = (
            "identity",
            "style_master",
            "angle_inventory",
            "main_vc",
            "detail_vc",
            "final_prompts",
            "integrity",
            "renders",
            "qc",
        )
        self.assertEqual(set(steps[:-1]), set(batch_type_gate.SET_READY_STEPS))
        for step in steps[:-1]:
            with self.subTest(batch_type="set", step=step):
                self.assertIsNone(
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "set"},
                        step,
                    )
                )
        self.assertEqual(
            SET_BLOCKED_MESSAGE,
            batch_type_gate.set_batch_blocked_message({"batch_type": "set"}, "qc"),
        )
        for step in steps:
            with self.subTest(batch_type="single", step=step):
                self.assertIsNone(
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "single"},
                        step,
                    )
                )


class St04aSetRenderAssemblerTests(unittest.TestCase):
    def test_g2_set_tasks_upload_group_first_then_all_components_and_keep_output_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            plan = assemble_render_tasks(fixture.manifest, fixture.index_path)
            expected_references = (
                fixture.group_paths[1],
                fixture.component_paths[0],
                fixture.component_paths[1],
                fixture.component_paths[2],
            )
            self.assertEqual(("main_01", "detail_01"), plan.planned)
            self.assertEqual((), plan.skipped)
            self.assertEqual(["1024x1024", "1024x1536"], [task.size for task in plan.tasks])
            self.assertEqual(
                [fixture.renders_dir / "main_01.png", fixture.renders_dir / "detail_01.png"],
                [task.output_path for task in plan.tasks],
            )
            self.assertTrue(
                all(task.reference_images == expected_references for task in plan.tasks)
            )

            (fixture.renders_dir / "main_01.png").write_bytes(b"accepted existing output")
            resumed = assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertEqual(("main_01",), resumed.skipped)
            self.assertEqual(("detail_01",), resumed.planned)

    def test_g3_missing_component_and_unavailable_component_directory_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            fixture.component_paths[1].unlink()
            with self.assertRaises(RenderTaskAssemblyError) as caught:
                assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertEqual("render_input_missing", getattr(caught.exception, "code", None))
            self.assertEqual(
                (fixture.component_paths[1].name,),
                getattr(caught.exception, "missing_files", None),
            )
            self.assertEqual(1, getattr(caught.exception, "missing_count", None))
            self.assertEqual(5, getattr(caught.exception, "remaining_count", None))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            fixture.manifest["inputs"]["component_white_bg_images"] = [
                str(fixture.inputs_root / "missing-components")
            ]
            with self.assertRaises(RenderTaskAssemblyError) as caught:
                assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertEqual("render_inputs_unavailable", getattr(caught.exception, "code", None))
            self.assertFalse(hasattr(caught.exception, "missing_files"))

    def test_g3_inventory_identity_and_layout_shape_fail_closed(self) -> None:
        cases = (
            ("artifact_type", "wrong_type", "套装角度与编排入库表契约无效"),
            ("product_id", "other-product", "套装角度与编排入库表契约无效"),
            ("layouts", {}, "套装角度与编排入库表契约无效"),
            ("layouts", ["not-an-object"], "套装角度与编排入库表 layouts 结构无效"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                fixture = SetRenderFixture(Path(temporary))
                inventory = _read_json(fixture.layout_path)
                inventory[field] = value
                _write_json(fixture.layout_path, inventory)
                with self.assertRaisesRegex(RenderTaskAssemblyError, message):
                    assemble_render_tasks(fixture.manifest, fixture.index_path)

    def test_g3_group_directory_boundary_format_and_missing_group_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            index = _read_json(fixture.index_path)
            index["items"][0]["bound_reference"] = "../outside.png"
            _write_json(fixture.index_path, index)
            with self.assertRaisesRegex(RenderTaskAssemblyError, "最终提示词绑定参考图无效"):
                assemble_render_tasks(fixture.manifest, fixture.index_path)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            unsupported = fixture.group_dir / "group_bad.gif"
            unsupported.write_bytes(b"offline gif")
            index = _read_json(fixture.index_path)
            index["items"][0]["bound_reference"] = unsupported.name
            _write_json(fixture.index_path, index)
            with self.assertRaisesRegex(RenderTaskAssemblyError, "绑定参考图格式不受支持"):
                assemble_render_tasks(fixture.manifest, fixture.index_path)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            index = _read_json(fixture.index_path)
            index["items"][0]["bound_reference"] = fixture.component_paths[0].name
            _write_json(fixture.index_path, index)
            with self.assertRaises(RenderTaskAssemblyError) as caught:
                assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertIn("绑定参考图不在白底图目录中", str(caught.exception))
            self.assertEqual("render_input_missing", getattr(caught.exception, "code", None))
            self.assertEqual(
                (fixture.component_paths[0].name,),
                getattr(caught.exception, "missing_files", None),
            )
            self.assertEqual(1, getattr(caught.exception, "missing_count", None))
            self.assertEqual(6, getattr(caught.exception, "remaining_count", None))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            fixture.group_paths[1].unlink()
            with self.assertRaises(RenderTaskAssemblyError) as caught:
                assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertEqual("render_input_missing", getattr(caught.exception, "code", None))
            self.assertEqual(
                (fixture.group_paths[1].name,),
                getattr(caught.exception, "missing_files", None),
            )
            self.assertEqual(1, getattr(caught.exception, "missing_count", None))
            self.assertEqual(5, getattr(caught.exception, "remaining_count", None))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            unreadable = fixture.group_paths[1]
            real_open = Path.open

            def selective_open(path: Path, *args: object, **kwargs: object):
                if path == unreadable and (args[:1] == ("rb",) or kwargs.get("mode") == "rb"):
                    raise OSError("simulated unreadable set group")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", new=selective_open):
                with self.assertRaises(RenderTaskAssemblyError) as caught:
                    assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertEqual("render_input_missing", getattr(caught.exception, "code", None))
            self.assertEqual((unreadable.name,), getattr(caught.exception, "missing_files", None))

        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            duplicate_root = fixture.group_dir / "nested"
            duplicate_root.mkdir()
            (duplicate_root / fixture.group_paths[1].name).write_bytes(b"duplicate")
            with self.assertRaisesRegex(
                RenderTaskAssemblyError,
                "绑定参考图必须在白底图目录中唯一匹配",
            ):
                assemble_render_tasks(fixture.manifest, fixture.index_path)

    def test_g4_component_names_are_deduplicated_in_order_without_admission_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(
                Path(temporary),
                duplicate_component=True,
                rejected_component=True,
            )
            self.assertEqual(
                (
                    fixture.group_paths[1].name,
                    fixture.component_paths[0].name,
                    fixture.component_paths[1].name,
                    fixture.component_paths[2].name,
                ),
                set_reference_filenames(fixture.manifest),
            )
            task = assemble_render_tasks(fixture.manifest, fixture.index_path).tasks[0]
            self.assertEqual(
                (
                    fixture.group_paths[1],
                    fixture.component_paths[0],
                    fixture.component_paths[1],
                    fixture.component_paths[2],
                ),
                task.reference_images,
            )

    def test_g3_component_in_the_wrong_input_directory_is_structured_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = SetRenderFixture(Path(temporary))
            missing_component = fixture.component_paths[1]
            missing_component.unlink()
            (fixture.group_dir / missing_component.name).write_bytes(b"wrong role")
            with self.assertRaises(RenderTaskAssemblyError) as caught:
                assemble_render_tasks(fixture.manifest, fixture.index_path)
            self.assertIn("绑定参考图不在白底图目录中", str(caught.exception))
            self.assertEqual("render_input_missing", getattr(caught.exception, "code", None))
            self.assertEqual(
                (missing_component.name,),
                getattr(caught.exception, "missing_files", None),
            )
            self.assertEqual(1, getattr(caught.exception, "missing_count", None))
            self.assertEqual(6, getattr(caught.exception, "remaining_count", None))


class St04aSetRecoveryTests(unittest.TestCase):
    def test_g5_set_scan_attributes_missing_group_and_component_references(self) -> None:
        for missing_kind, missing_index in (("group", 1), ("component", 1)):
            with self.subTest(missing_kind=missing_kind), tempfile.TemporaryDirectory() as temporary:
                fixture = SetRenderFixture(Path(temporary))
                missing_path = (
                    fixture.group_paths[missing_index]
                    if missing_kind == "group"
                    else fixture.component_paths[missing_index]
                )
                missing_path.unlink()
                scan = scan_white_bg_recovery(fixture.manifest)
                self.assertEqual("missing_reference", scan.kind)
                self.assertEqual((missing_path.name,), scan.missing_files)
                self.assertEqual(1, scan.missing_count)
                self.assertEqual(5, scan.remaining_count)

    def test_g9_set_render_failure_never_offers_discard_and_recompute(self) -> None:
        service = object.__new__(WorkflowProductionService)
        service.artifact_reader = mock.Mock(
            side_effect=AssertionError("set recovery must not inspect rendered outputs")
        )
        fields = {
            "code": "render_input_missing",
            "missing_count": 1,
            "missing_files": ("component_02.png",),
            "remaining_count": 5,
        }
        recovery = service._render_failure_recovery(fields, {"batch_type": "set"})
        self.assertEqual(False, recovery["recomputeEligible"])
        service.artifact_reader.assert_not_called()

        failure = ExecutorExecutionError("offline missing reference")
        failure.code = "render_input_missing"
        failure.missing_count = 1
        failure.missing_files = ("component_02.png",)
        failure.remaining_count = 5
        messages = service._structured_render_failure_messages(
            failure,
            recompute_eligible=bool(recovery["recomputeEligible"]),
        )
        self.assertIsNotNone(messages)
        workbench_message = messages[1]
        self.assertIn("恢复文件后重新开始", workbench_message)
        self.assertNotIn("剔除", workbench_message)
        self.assertNotIn("重新分配", workbench_message)

    def test_g10_recompute_predicate_is_exact_string_single_only(self) -> None:
        self.assertTrue(allows_rebind_recompute("single"))
        blocked_values = ("set", "", "SINGLE", 0, None, [], {}.get("batch_type"))
        scan = WhiteBgScan("missing_reference", ("missing.jpg",), 1, 1)
        for batch_type in blocked_values:
            with self.subTest(batch_type=batch_type):
                self.assertFalse(allows_rebind_recompute(batch_type))
                eligibility = evaluate_rebind_eligibility(
                    scan,
                    -1,
                    batch_type=batch_type,
                )
                self.assertFalse(eligibility.eligible)
                self.assertEqual("recompute_unsupported_for_set", eligibility.code)
                self.assertIn("恢复缺失的白底图后重新开始", eligibility.message)
        with self.assertRaises(WhiteBgRecoveryError):
            evaluate_rebind_eligibility(scan, -1, batch_type="single")

    def test_g11_http_rejects_set_before_journal_or_archive_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SetRenderFixture(root)
            fixture.component_paths[1].unlink()
            repository = root / "repository"
            manifest_path = repository / "manifests" / f"{fixture.product_id}.batch_manifest.json"
            _write_json(manifest_path, fixture.manifest)
            journal_path = run_controller.journal_path(manifest_path, fixture.product_id)
            journal_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
            original_journal = journal_path.read_bytes()

            application = production_http.WorkflowProductionHttpApplication(
                repository,
                "offline-token",
                batch_lock_root=root / "locks",
            )
            application._manifest = mock.Mock(
                return_value=(fixture.manifest, manifest_path, fixture.workspace)
            )
            application.quote = mock.Mock(return_value={"readyCount": 0})
            with mock.patch.object(
                production_http,
                "archive_recompute_artifacts",
                side_effect=AssertionError("archive must be after set rejection"),
            ) as archive:
                with self.assertRaises(production_http.RebindRecomputeRejected) as caught:
                    application.rebind_recompute(fixture.product_id)

            self.assertEqual(409, caught.exception.status)
            self.assertEqual("recompute_unsupported_for_set", caught.exception.error_code)
            self.assertIn("恢复缺失的白底图后重新开始", str(caught.exception))
            archive.assert_not_called()
            self.assertFalse((fixture.artifacts_root / "_superseded").exists())
            self.assertFalse((repository / "reports" / "_superseded").exists())
            self.assertEqual(original_journal, journal_path.read_bytes())
            self.assertNotIn("white_bg_rebind_recompute", journal_path.read_text(encoding="utf-8"))


class St04aIntegrityTests(unittest.TestCase):
    def test_g6_legacy_full_mode_rejects_set_while_prompts_only_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = set_prompt_fixtures.St03cSetFinalPromptFixture().prepare_full_chain(
                root,
                handheld_target=0,
            )
            missing = root / "must-not-be-read.json"
            with self.assertRaises(integrity_validator.ScriptError) as caught:
                integrity_validator.build_report(
                    batch_manifest_path=prepared["manifest_path"],
                    identity_path=missing,
                    final_prompt_index_path=missing,
                    job_manifest_path=missing,
                    compiler_path=missing,
                )
            self.assertIn("套装批次", str(caught.exception))
            self.assertIn("--prompts-only", str(caught.exception))

            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=prepared["manifest_path"]
            )
            self.assertEqual("pass", report["status"], report["blocking_issues"])

    def test_g7_single_and_set_reports_match_pre_extraction_byte_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            single_root = root / "single"
            set_root = root / "set"
            single_bundle = single_fixtures.build_final_prompt_bundle(single_root)
            set_prepared = set_prompt_fixtures.St03cSetFinalPromptFixture().prepare_full_chain(
                set_root,
                handheld_target=0,
            )
            single_report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=single_bundle.manifest_path
            )
            set_report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=set_prepared["manifest_path"]
            )
            single_snapshot = _normalized_report_bytes(single_report, single_root)
            set_snapshot = _normalized_report_bytes(set_report, set_root)
            self.assertEqual("pass", single_report["status"], single_report["blocking_issues"])
            self.assertEqual("pass", set_report["status"], set_report["blocking_issues"])
            self.assertEqual([], single_report["blocking_issues"])
            self.assertEqual([], set_report["blocking_issues"])
            self.assertEqual(SINGLE_REPORT_SHA256, hashlib.sha256(single_snapshot).hexdigest())
            self.assertEqual(SET_REPORT_SHA256, hashlib.sha256(set_snapshot).hexdigest())

    def test_g7_single_shared_config_sequence_requires_exact_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = single_fixtures.build_final_prompt_bundle(Path(temporary))
            main_config = _read_json(bundle.main_config_path)
            main_config["configs"][0]["config_id"] = "main_010"
            _write_json(bundle.main_config_path, main_config)
            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=bundle.manifest_path
            )
            self.assertIn("variable_config_sequence_mismatch_main_01", _issue_ids(report))

    def test_g7_set_shared_config_sequence_requires_exact_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prepared = set_prompt_fixtures.St03cSetFinalPromptFixture().prepare_full_chain(
                Path(temporary),
                handheld_target=0,
            )
            main_config_path = prepared["paths"]["main"] / "main_variable_configs.json"
            main_config = _read_json(main_config_path)
            main_config["configs"][0]["config_id"] = "main_010"
            _write_json(main_config_path, main_config)
            report = integrity_validator.build_prompts_only_report(
                batch_manifest_path=prepared["manifest_path"]
            )
            self.assertIn("variable_config_sequence_mismatch_main_01", _issue_ids(report))


if __name__ == "__main__":
    unittest.main()
