from __future__ import annotations

import sys
import unittest
import importlib
import copy
import json
import struct
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "canvas-bridge") not in sys.path:
    sys.path.insert(0, str(ROOT / "canvas-bridge"))

from codex_dev_executor import (  # noqa: E402
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
    SUPPORTED_STEPS,
)
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
)
import codex_dev_qc as qc  # noqa: E402
from codex_dev_qc import load_qc_plan  # noqa: E402


class FakeQcTransport:
    def __init__(self, results: list[CodexTurnResult]):
        self.results = list(results)
        self.calls: list[tuple[str, tuple[CodexAttachment, ...]]] = []
        self.continuation_calls: list[tuple[str, str, tuple[CodexAttachment, ...]]] = []

    def run_turn(self, prompt: str, attachments: tuple[CodexAttachment, ...]) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        return self.results.pop(0)

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.continuation_calls.append((thread_id, prompt, attachments))
        return self.results.pop(0)


class CodexDevQcTest(unittest.TestCase):
    @staticmethod
    def valid_batch_response(batch: object) -> dict[str, object]:
        results: list[dict[str, object]] = []
        common_items = (
            "product_identity",
            "product_color",
            "product_angle",
            "page_task",
            "composition",
            "realism",
            "props",
            "text",
            "size_ratio",
            "style_consistency",
            "platform_spec",
            "ai_artifacts",
        )
        for asset in batch.assets:
            items = common_items + (("handheld",) if asset.handheld else ())
            results.extend(
                {
                    "affected_asset": asset.asset_id,
                    "check_item": check_item,
                    "status": "pass",
                    "notes": f"evidence for {check_item}",
                }
                for check_item in items
            )
        return {
            "chunk_index": batch.index,
            "chunk_count": 8,
            "checked_assets": [asset.asset_id for asset in batch.assets],
            "results": results,
            "issues": [],
            "repair_targets": [],
        }

    @staticmethod
    def valid_summary_response(plan: object) -> dict[str, object]:
        return {
            "chunk_index": 8,
            "chunk_count": 8,
            "checked_assets": [asset.asset_id for asset in plan.assets],
            "results": [
                {"check_item": check_item, "status": "pass", "notes": f"evidence for {check_item}"}
                for check_item in (
                    "main_set_consistency",
                    "detail_module_chain",
                    "batch_style_consistency",
                    "batch_platform_readiness",
                )
            ],
            "issues": [],
            "repair_targets": [],
        }

    @staticmethod
    def _write_png(path: Path, width: int, height: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + b"\x00\x00\x00\x00")

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def make_qc_fixture(self, root: Path) -> tuple[dict[str, object], Path]:
        workspace = root / "workspace"
        inputs_root = workspace / "inputs"
        artifacts_root = workspace / "artifacts"
        outputs_root = workspace / "outputs"
        white_bg = inputs_root / "white_bg"
        renders = outputs_root / "renders"
        variables = artifacts_root / "variable_configs"
        final_prompts = artifacts_root / "final_prompts"
        qc_reports = artifacts_root / "qc_reports"

        refs = ("front.jpg", "side.jpg", "top.jpg")
        for name in refs:
            path = white_bg / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff\xd9")

        config_ids = tuple(f"main_{index:02d}" for index in range(1, 7)) + tuple(
            f"detail_{index:02d}" for index in range(1, 9)
        )
        handheld_ids = {"main_02", "main_05", "detail_02"}
        main_configs: list[dict[str, object]] = []
        detail_configs: list[dict[str, object]] = []
        index_items: list[dict[str, object]] = []
        for index, config_id in enumerate(config_ids):
            output_type = "main" if config_id.startswith("main_") else "detail"
            declaration = (
                "本张图启用手持场景。单手自然握持。"
                if config_id in handheld_ids
                else "本张图不启用手持场景"
            )
            config = {
                "config_id": config_id,
                "output_type": output_type,
                "per_image_overrides": {"手持交互声明": declaration},
            }
            (main_configs if output_type == "main" else detail_configs).append(config)
            reference = refs[index % len(refs)]
            prompt_path = final_prompts / f"{config_id}_final_prompt.json"
            self._write_json(
                prompt_path,
                {
                    "product_id": "p1",
                    "artifact_type": "final_prompt",
                    "final_prompt": f"PROMPT_{config_id}",
                    "variable_config": {
                        "config_id": config_id,
                        "output_type": output_type,
                    },
                },
            )
            index_items.append(
                {
                    "config_id": config_id,
                    "output_type": output_type,
                    "bound_reference": reference,
                    "final_prompt_path": str(prompt_path),
                }
            )
            self._write_png(renders / f"{config_id}.png", 10 if output_type == "main" else 9, 10 if output_type == "main" else 12)

        identity_dir = artifacts_root / "identity"
        style_dir = artifacts_root / "style_master"
        angle_dir = artifacts_root / "angle_inventory"
        self._write_json(identity_dir / "product_identity_archive.json", {"product_id": "p1", "artifact_type": "product_identity_archive", "identity": {}})
        self._write_json(style_dir / "style_master.json", {"product_id": "p1", "artifact_type": "style_master", "style_master": {}})
        self._write_json(angle_dir / "angle_inventory.json", {"product_id": "p1", "artifact_type": "angle_inventory", "angle_slots": []})
        self._write_json(
            variables / "main_variable_configs.json",
            {"product_id": "p1", "artifact_type": "main_variable_config", "config_count": 6, "configs": main_configs},
        )
        self._write_json(
            variables / "detail_variable_configs.json",
            {"product_id": "p1", "artifact_type": "detail_variable_config", "config_count": 8, "configs": detail_configs},
        )
        self._write_json(
            final_prompts / "final_prompt_index.json",
            {"product_id": "p1", "artifact_type": "final_prompt_index", "prompt_count": 14, "items": index_items},
        )

        skill_root = root / ".agents" / "skills" / "qc-inspector"
        (skill_root / "references" / "runtime_rule_slices").mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("QC_SKILL_MARKER", encoding="utf-8")
        self._write_json(
            skill_root / "references" / "runtime_rule_slices" / "qc-inspector.runtime_rule_slices.json",
            {"artifact_type": "runtime_rule_slice_package", "skill": "qc-inspector", "slices": [{"text": "QC_RUNTIME_MARKER"}]},
        )
        for name, marker in (
            ("电商图片通用质检清单.txt", "QC_CHECKLIST_MARKER"),
            ("工作流总控规则.txt", "QC_WORKFLOW_MARKER"),
            ("真实感约束.txt", "QC_REALISM_MARKER"),
        ):
            (skill_root / "references" / name).write_text(marker, encoding="utf-8")

        self._write_json(
            root / "schemas" / "qc_report.schema.json",
            {
                "$id": "qc_report.schema.json",
                "type": "object",
                "required": [
                    "product_id",
                    "artifact_type",
                    "checked_assets",
                    "results",
                    "issues",
                    "repair_targets",
                    "adds_new_generation_direction",
                ],
                "properties": {
                    "artifact_type": {"const": "qc_report"},
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "status": {
                                    "enum": ["pass", "fail", "needs_review", "not_applicable"]
                                }
                            },
                        },
                    },
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {
                                    "enum": ["critical", "major", "minor", "needs_review"]
                                }
                            },
                        },
                    },
                    "repair_targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "severity": {
                                    "enum": ["critical", "major", "minor", "needs_review"]
                                }
                            },
                        },
                    },
                    "adds_new_generation_direction": {"const": False},
                },
                "not": {
                    "anyOf": [
                        {"required": ["new_generation_direction"]},
                        {"required": ["creative_direction"]},
                        {"required": ["generation_prompt"]},
                        {"required": ["final_prompt"]},
                    ]
                },
            },
        )

        manifest: dict[str, object] = {
            "product_id": "p1",
            "batch_type": "single",
            "workspace": {
                "inputs_root": str(inputs_root),
                "artifacts_root": str(artifacts_root),
                "outputs_root": str(outputs_root),
            },
            "inputs": {"white_bg_images": [str(white_bg)]},
            "artifacts": {
                "product_identity_archive": str(identity_dir),
                "style_master": str(style_dir),
                "angle_inventory": str(angle_dir),
                "main_variable_configs": [str(variables)],
                "detail_variable_configs": [str(variables)],
                "final_prompts": [str(final_prompts)],
                "qc_reports": [str(qc_reports)],
            },
        }
        return manifest, qc_reports / "qc_report.json"

    def test_qc_is_a_supported_codex_dev_step(self) -> None:
        self.assertIn("qc", SUPPORTED_STEPS)

    def test_qc_domain_module_exposes_plan_loader(self) -> None:
        try:
            module = importlib.import_module("codex_dev_qc")
        except ModuleNotFoundError:
            self.fail("codex_dev_qc module is missing")
        self.assertTrue(callable(module.load_qc_plan))

    def test_qc_plan_loads_ordered_assets_bindings_rules_and_seven_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, expected_output = self.make_qc_fixture(root)

            plan = load_qc_plan(manifest, root)

            expected_ids = tuple(f"main_{index:02d}.png" for index in range(1, 7)) + tuple(
                f"detail_{index:02d}.png" for index in range(1, 9)
            )
            self.assertEqual("p1", plan.product_id)
            self.assertEqual(expected_output, plan.output_path)
            self.assertEqual(expected_ids, tuple(asset.asset_id for asset in plan.assets))
            self.assertEqual(7, len(plan.batches))
            self.assertEqual((2,) * 7, tuple(len(batch.assets) for batch in plan.batches))
            self.assertEqual(
                {"main_02.png", "main_05.png", "detail_02.png"},
                {asset.asset_id for asset in plan.assets if asset.handheld},
            )
            self.assertTrue(all(asset.reference_path.name in {"front.jpg", "side.jpg", "top.jpg"} for asset in plan.assets))
            self.assertEqual(
                {
                    "SKILL.md",
                    "qc-inspector.runtime_rule_slices.json",
                    "电商图片通用质检清单.txt",
                    "工作流总控规则.txt",
                    "真实感约束.txt",
                },
                {document.name for document in plan.rule_documents},
            )

    def test_qc_domain_module_exposes_batch_prompt_and_attachment_order(self) -> None:
        self.assertTrue(callable(getattr(qc, "build_qc_batch_prompt", None)))
        self.assertTrue(callable(getattr(qc, "qc_batch_attachment_paths", None)))

    def test_qc_batch_prompt_contains_complete_rules_inputs_and_strict_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            plan = load_qc_plan(manifest, root)
            batch = plan.batches[0]

            prompt = qc.build_qc_batch_prompt(plan, batch)
            attachment_paths = qc.qc_batch_attachment_paths(batch)

            self.assertEqual(
                ("main_01.png", "front.jpg", "main_02.png", "side.jpg"),
                tuple(path.name for path in attachment_paths),
            )
            for marker in (
                "QC_SKILL_MARKER",
                "QC_RUNTIME_MARKER",
                "QC_CHECKLIST_MARKER",
                "QC_WORKFLOW_MARKER",
                "QC_REALISM_MARKER",
                "PROMPT_main_01",
                "PROMPT_main_02",
            ):
                self.assertIn(marker, prompt)
            for check_item in (
                "product_identity",
                "product_color",
                "product_angle",
                "page_task",
                "composition",
                "realism",
                "props",
                "text",
                "size_ratio",
                "style_consistency",
                "platform_spec",
                "ai_artifacts",
            ):
                self.assertIn(check_item, prompt)
            self.assertIn('"main_01.png": false', prompt)
            self.assertIn('"main_02.png": true', prompt)
            self.assertIn("不得虚构尺寸、容量、重量、材质、认证、品牌或型号", prompt)
            self.assertIn("不得新增生成方向", prompt)
            self.assertIn('"chunk_index": 1', prompt)
            self.assertIn('"chunk_count": 8', prompt)

    def test_qc_plan_rejects_attachment_batch_over_twenty_mebibytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            white_bg = Path(manifest["inputs"]["white_bg_images"][0])
            for name in ("front.jpg", "side.jpg"):
                with (white_bg / name).open("wb") as handle:
                    handle.truncate(16 * 1024 * 1024)
                    handle.seek(0)
                    handle.write(b"\xff\xd8\xff\xd9")

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 附件大小超过单批限制"):
                load_qc_plan(manifest, root)

    def test_qc_domain_module_exposes_batch_response_parser(self) -> None:
        self.assertTrue(callable(getattr(qc, "parse_qc_batch_response", None)))

    def test_qc_batch_parser_accepts_complete_fixed_checks_and_handheld_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            plan = load_qc_plan(manifest, root)
            expected = self.valid_batch_response(plan.batches[0])

            parsed = qc.parse_qc_batch_response(
                json.dumps(expected, ensure_ascii=False), plan.batches[0]
            )

            self.assertEqual(expected, parsed)

    def test_qc_domain_module_exposes_summary_assembly_and_exclusive_write(self) -> None:
        for name in (
            "build_qc_summary_prompt",
            "parse_qc_summary_response",
            "assemble_qc_report",
            "write_qc_report_exclusive",
        ):
            self.assertTrue(callable(getattr(qc, name, None)), name)

    def test_qc_summary_assembles_one_schema_shaped_report_and_preserves_integrity_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output_path = self.make_qc_fixture(root)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            integrity_json = output_path.parent / "final_prompt_integrity_report.json"
            integrity_md = output_path.parent / "final_prompt_integrity_report.md"
            integrity_json.write_text("INTEGRITY_JSON", encoding="utf-8")
            integrity_md.write_text("INTEGRITY_MD", encoding="utf-8")
            plan = load_qc_plan(manifest, root)
            chunks = tuple(self.valid_batch_response(batch) for batch in plan.batches)
            summary_value = self.valid_summary_response(plan)

            prompt = qc.build_qc_summary_prompt(plan, chunks)
            summary = qc.parse_qc_summary_response(
                json.dumps(summary_value, ensure_ascii=False),
                plan,
                prior_chunks=chunks,
            )
            report = qc.assemble_qc_report(plan, chunks, summary)
            written = qc.write_qc_report_exclusive(plan, report)

            for check_item in (
                "main_set_consistency",
                "detail_module_chain",
                "batch_style_consistency",
                "batch_platform_readiness",
            ):
                self.assertIn(check_item, prompt)
            self.assertIn("不附加图片", prompt)
            self.assertEqual(output_path, written)
            stored = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("p1", stored["product_id"])
            self.assertEqual("qc_report", stored["artifact_type"])
            self.assertEqual([asset.asset_id for asset in plan.assets], stored["checked_assets"])
            self.assertFalse(stored["adds_new_generation_direction"])
            self.assertEqual("INTEGRITY_JSON", integrity_json.read_text(encoding="utf-8"))
            self.assertEqual("INTEGRITY_MD", integrity_md.read_text(encoding="utf-8"))
            self.assertEqual(
                {"final_prompt_integrity_report.json", "final_prompt_integrity_report.md", "qc_report.json"},
                {path.name for path in output_path.parent.iterdir()},
            )
            with self.assertRaisesRegex(ExecutorExecutionError, "正式 QC 报告已存在"):
                qc.write_qc_report_exclusive(plan, report)

    def test_qc_executor_runs_eight_same_thread_fake_turns_and_writes_after_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output_path = self.make_qc_fixture(root)
            manifest_path = root / "manifests" / "p1.batch_manifest.json"
            manifest_path.parent.mkdir(parents=True)
            context = ExecutorContext(
                manifest=manifest,
                manifest_path=manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            )
            plan = load_qc_plan(manifest, root)
            values = [self.valid_batch_response(batch) for batch in plan.batches]
            values.append(self.valid_summary_response(plan))
            transport = FakeQcTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(value, ensure_ascii=False),
                        thread_id="qc-thread",
                    )
                    for value in values
                ]
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="qc"))

            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("qc-thread", result.metadata["thread_id"])
            self.assertEqual(0, result.metadata["recovery_attempts"])
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(7, len(transport.continuation_calls))
            self.assertEqual(4, len(transport.calls[0][1]))
            self.assertEqual((4, 4, 4, 4, 4, 4, 0), tuple(len(call[2]) for call in transport.continuation_calls))
            self.assertEqual(("main_01.png", "front.jpg", "main_02.png", "side.jpg"), tuple(item.name for item in transport.calls[0][1]))
            self.assertTrue(output_path.is_file())

    def test_qc_executor_recovers_unicode_and_truncation_twice_in_same_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output_path = self.make_qc_fixture(root)
            context = ExecutorContext(
                manifest=manifest,
                manifest_path=root / "manifests" / "p1.batch_manifest.json",
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            )
            plan = load_qc_plan(manifest, root)
            valid_chunks = [self.valid_batch_response(batch) for batch in plan.batches]
            texts = [
                '{"chunk_index":1,"damaged":"\ufffd"',
                json.dumps(valid_chunks[0], ensure_ascii=False),
                '{"chunk_index":2,"chunk_count":8,"checked_assets":["main_03.png"',
                json.dumps(valid_chunks[1], ensure_ascii=False),
                *(json.dumps(chunk, ensure_ascii=False) for chunk in valid_chunks[2:]),
                json.dumps(self.valid_summary_response(plan), ensure_ascii=False),
            ]
            transport = FakeQcTransport(
                [CodexTurnResult(text=text, thread_id="qc-thread") for text in texts]
            )

            result = CodexDevExecutor(
                context, transport=transport, repository_root=root
            ).execute(ExecutionRequest(step="qc"))

            self.assertEqual(2, result.metadata["recovery_attempts"])
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(9, len(transport.continuation_calls))
            repair_calls = [call for call in transport.continuation_calls if "重新发送完整 JSON" in call[1]]
            self.assertEqual(2, len(repair_calls))
            self.assertTrue(all(call[0] == "qc-thread" and not call[2] for call in repair_calls))
            self.assertTrue(output_path.is_file())

    def test_qc_executor_stops_after_two_recoveries_without_output_or_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output_path = self.make_qc_fixture(root)
            context = ExecutorContext(
                manifest=manifest,
                manifest_path=root / "manifests" / "p1.batch_manifest.json",
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            )
            transport = FakeQcTransport(
                [
                    CodexTurnResult(text='{ "x": "\ufffd"', thread_id="qc-thread"),
                    CodexTurnResult(text='{ "x":', thread_id="qc-thread"),
                    CodexTurnResult(text='{ "x": [', thread_id="qc-thread"),
                ]
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 传输恢复已达到上限"):
                CodexDevExecutor(
                    context, transport=transport, repository_root=root
                ).execute(ExecutionRequest(step="qc"))

            self.assertFalse(output_path.exists())
            self.assertEqual([], list(output_path.parent.glob(".*.tmp")))
            self.assertEqual(2, len(transport.continuation_calls))

    def test_qc_executor_does_not_retry_legal_json_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, output_path = self.make_qc_fixture(root)
            context = ExecutorContext(
                manifest=manifest,
                manifest_path=root / "manifests" / "p1.batch_manifest.json",
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            )
            plan = load_qc_plan(manifest, root)
            invalid = self.valid_batch_response(plan.batches[0])
            invalid["results"][0]["status"] = "maybe"
            transport = FakeQcTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="qc-thread",
                    )
                ]
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 检查项内容无效"):
                CodexDevExecutor(
                    context, transport=transport, repository_root=root
                ).execute(ExecutionRequest(step="qc"))

            self.assertEqual(1, len(transport.calls))
            self.assertEqual(0, len(transport.continuation_calls))
            self.assertFalse(output_path.exists())
            self.assertEqual([], list(output_path.parent.glob(".*.tmp")))

    def test_qc_batch_parser_rejects_unknown_fields_enums_and_handheld_scope_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            plan = load_qc_plan(manifest, root)
            batch = plan.batches[0]
            valid = self.valid_batch_response(batch)
            variants: list[dict[str, object]] = []

            extra_top = copy.deepcopy(valid)
            extra_top["new_generation_direction"] = "forbidden"
            variants.append(extra_top)

            extra_result = copy.deepcopy(valid)
            extra_result["results"][0]["confidence"] = 0.9
            variants.append(extra_result)

            invalid_status = copy.deepcopy(valid)
            invalid_status["results"][0]["status"] = "unknown"
            variants.append(invalid_status)

            missing_handheld = copy.deepcopy(valid)
            missing_handheld["results"] = [
                item
                for item in missing_handheld["results"]
                if not (item["affected_asset"] == "main_02.png" and item["check_item"] == "handheld")
            ]
            variants.append(missing_handheld)

            extra_handheld = copy.deepcopy(valid)
            extra_handheld["results"].append(
                {
                    "affected_asset": "main_01.png",
                    "check_item": "handheld",
                    "status": "not_applicable",
                    "notes": "not declared",
                }
            )
            variants.append(extra_handheld)

            for value in variants:
                with self.subTest(keys=tuple(value)):
                    with self.assertRaises(ExecutorExecutionError):
                        qc.parse_qc_batch_response(
                            json.dumps(value, ensure_ascii=False), batch
                        )

    def test_qc_batch_parser_validates_issue_ids_and_repair_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            plan = load_qc_plan(manifest, root)
            first = self.valid_batch_response(plan.batches[0])
            first["issues"] = [
                {
                    "issue_id": "issue_shared",
                    "severity": "major",
                    "description": "identity mismatch",
                    "affected_asset": "main_01.png",
                    "category": "product_identity",
                }
            ]
            first["repair_targets"] = [
                {
                    "target_id": "repair_shared",
                    "repair_goal": "restore identity",
                    "severity": "major",
                    "affected_asset": "main_01.png",
                    "return_stage": "product_identity",
                    "issue_id": "issue_shared",
                }
            ]
            parsed_first = qc.parse_qc_batch_response(
                json.dumps(first, ensure_ascii=False), plan.batches[0]
            )

            invalid_stage = copy.deepcopy(first)
            invalid_stage["repair_targets"][0]["return_stage"] = "render_again"
            with self.assertRaises(ExecutorExecutionError):
                qc.parse_qc_batch_response(
                    json.dumps(invalid_stage, ensure_ascii=False), plan.batches[0]
                )

            second = self.valid_batch_response(plan.batches[1])
            second["issues"] = [
                {
                    "issue_id": "issue_shared",
                    "severity": "minor",
                    "description": "duplicate id",
                    "affected_asset": "main_03.png",
                    "category": "text",
                }
            ]
            second["repair_targets"] = []
            with self.assertRaises(ExecutorExecutionError):
                qc.parse_qc_batch_response(
                    json.dumps(second, ensure_ascii=False),
                    plan.batches[1],
                    prior_chunks=(parsed_first,),
                )

    def test_qc_parser_only_classifies_unicode_or_structural_truncation_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            plan = load_qc_plan(manifest, root)
            batch = plan.batches[0]

            for damaged in ('{"x":"\ufffd"}', '{"chunk_index":1,"results":['):
                with self.assertRaises(qc.QcTransportCorruption):
                    qc.parse_qc_batch_response(damaged, batch)
            for invalid in ("not-json", '{"chunk_index": nope}', "```json\n{}\n```"):
                with self.assertRaises(ExecutorExecutionError):
                    qc.parse_qc_batch_response(invalid, batch)

    def test_qc_plan_requires_exactly_the_fourteen_declared_render_pngs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            renders = Path(manifest["workspace"]["outputs_root"]) / "renders"
            self._write_png(renders / "unexpected_15.png", 10, 10)

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 渲染图集合异常"):
                load_qc_plan(manifest, root)

    def test_qc_plan_rejects_invalid_qc_runtime_rule_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            runtime = (
                root
                / ".agents"
                / "skills"
                / "qc-inspector"
                / "references"
                / "runtime_rule_slices"
                / "qc-inspector.runtime_rule_slices.json"
            )
            runtime.write_text("not-json", encoding="utf-8")

            with self.assertRaisesRegex(ExecutorExecutionError, "完整的 QC 规则"):
                load_qc_plan(manifest, root)

    def test_qc_plan_rejects_bad_ratio_path_escape_product_mismatch_and_existing_report(self) -> None:
        cases = ("bad_ratio", "path_escape", "product_mismatch", "existing_report")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest, output_path = self.make_qc_fixture(root)
                if case == "bad_ratio":
                    renders = Path(manifest["workspace"]["outputs_root"]) / "renders"
                    self._write_png(renders / "main_01.png", 9, 10)
                    expected = "画布比例异常"
                elif case == "path_escape":
                    index_path = Path(manifest["artifacts"]["final_prompts"][0]) / "final_prompt_index.json"
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    index["items"][0]["final_prompt_path"] = str(root / "outside.json")
                    self._write_json(index_path, index)
                    expected = "最终提示词路径异常"
                elif case == "product_mismatch":
                    prompt_path = Path(manifest["artifacts"]["final_prompts"][0]) / "main_01_final_prompt.json"
                    prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
                    prompt["product_id"] = "other-product"
                    self._write_json(prompt_path, prompt)
                    expected = "最终提示词与当前商品不匹配"
                else:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
                    expected = "正式 QC 报告已存在"

                with self.assertRaisesRegex(ExecutorExecutionError, expected):
                    load_qc_plan(manifest, root)
                if case == "existing_report":
                    self.assertEqual("DO_NOT_OVERWRITE", output_path.read_text(encoding="utf-8"))

    def test_qc_plan_rejects_schema_contract_drift_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            schema_path = root / "schemas" / "qc_report.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["properties"]["results"]["items"]["properties"]["status"]["enum"] = ["pass"]
            self._write_json(schema_path, schema)

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 报告 schema 合同不匹配"):
                load_qc_plan(manifest, root)

    def test_qc_plan_rejects_unsupported_reference_format_during_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _output = self.make_qc_fixture(root)
            white_bg = Path(manifest["inputs"]["white_bg_images"][0])
            (white_bg / "front.gif").write_bytes((white_bg / "front.jpg").read_bytes())
            index_path = Path(manifest["artifacts"]["final_prompts"][0]) / "final_prompt_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for item in index["items"]:
                if item["bound_reference"] == "front.jpg":
                    item["bound_reference"] = "front.gif"
            self._write_json(index_path, index)

            with self.assertRaisesRegex(ExecutorExecutionError, "QC 附件格式无效"):
                load_qc_plan(manifest, root)


if __name__ == "__main__":
    unittest.main()
