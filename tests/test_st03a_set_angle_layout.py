from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "scripts", ROOT / "canvas-bridge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import batch_type_gate  # noqa: E402
import detect_current_state  # noqa: E402
import projector  # noqa: E402
import run_controller  # noqa: E402
from category_recipes import CategoryRecipeError, load_shared_prompt  # noqa: E402
from codex_dev_executor import (  # noqa: E402
    CodexAttachment,
    CodexDevExecutor,
    CodexTurnResult,
)
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
)


STEPS = (
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
BLOCKED_MESSAGE = (
    "套装批次的后续生产工序尚未开通，本批次已停在未开通工序开始之前，"
    "未执行该工序，也未产生任何费用。"
)
COMPONENT_LAYOUT_TEXT = "单件白底图，不涉及编排"
ARTIFACT_KEYS = (
    "asset_manifest",
    "product_identity_archive",
    "style_master",
    "angle_inventory",
    "main_variable_configs",
    "detail_variable_configs",
    "set_product_identity",
    "set_angle_layout_inventory",
    "final_prompts",
    "comfyui_jobs",
    "qc_reports",
)
ARTIFACT_TYPES = {
    "product_identity_archive": "product_identity_archive",
    "style_master": "style_master",
    "angle_inventory": "angle_inventory",
    "main_variable_configs": "main_variable_config",
    "detail_variable_configs": "detail_variable_config",
    "set_product_identity": "set_product_identity",
    "set_angle_layout_inventory": "set_angle_layout_inventory",
    "final_prompts": "final_prompt",
    "qc_reports": "qc_report",
}

VALID_STYLE_MASTER = {
    "artifact_type": "style_master",
    "style_master": {
        "visual_positioning": "适合生活方式电商主视觉与品牌首屏。",
        "composition_and_layout": "竖幅，主体偏下居中，左上留出文字区。",
        "background_rules": "暖米色真实空间背景，前中后景清楚。",
        "color_rules": "低饱和米色基底，绿色与粉色作辅助色。",
        "lighting_rules": "左前方柔和自然光，保留真实接触阴影。",
        "subject_presentation_rules": "主体完整展示，视觉权重最高。",
        "prop_rules": "使用克制的花叶与布面环境元素。",
        "typography_rules": "小面积低干扰中文标题，不复制原文案。",
        "negative_space_rules": "左上留白承载标题并形成呼吸感。",
        "visual_mood": "明亮、柔和、生活化，由暖背景和自然光形成。",
        "reusable_rules": ["暖米色多层背景", "左前方柔光"],
        "fidelity_enhancements": {
            "style_anchors": ["右后方花叶虚化背景"],
            "reusable_prop_clusters": {"must_keep": ["虚化花叶"]},
            "background_layers": {"background": "花叶与暖色空间"},
            "prop_density_level": "常规",
            "contents_and_usage_state": "只记录参考图可见状态。",
            "text_inheritance": "只继承排版气质。",
            "anti_degradation_rules": ["不得退化为纯白背景"],
        },
        "forbidden_elements": [f"禁止项 {index}" for index in range(1, 9)],
        "concise_style_master": "保持暖米色层次、自然光与真实接触阴影。",
    },
    "missing_information": [],
    "notes": "",
}
VALID_SINGLE_IDENTITY = {
    "artifact_type": "product_identity_archive",
    "identity": {
        "confirmed_facts": ["已确认事实"],
        "visible_inferences": ["可见推断"],
        "unknowns": ["无法确认"],
        "prohibited_inventions": ["禁止虚构尺寸"],
        "product_lock_description": "保持可见结构不变。",
    },
    "missing_information": ["容量无法确认"],
    "blocked_reasons": [],
    "notes": "",
}


def summary(file_count: int = 0, artifact_type: str | None = None) -> dict[str, object]:
    typed = {artifact_type: 1} if artifact_type and file_count else {}
    return {"paths": [], "file_count": file_count, "typed_artifact_counts": typed}


def route_fixture(
    *,
    batch_type: str,
    available: set[str],
    group_count: int = 1,
    requested: tuple[str, ...] = ("main",),
) -> dict[str, object]:
    manifest = {
        "batch_type": batch_type,
        "user_declared_set_product": batch_type == "set",
        "requested_outputs": list(requested),
    }
    inputs = {
        "white_bg_images": summary(2),
        "style_reference_images": summary(1),
        "set_group_images": summary(group_count),
        "component_white_bg_images": summary(2),
    }
    drafts = {
        "product_identity_draft": summary(),
        "style_master_draft": summary(),
    }
    artifacts = {
        key: summary(1, ARTIFACT_TYPES[key]) if key in available else summary()
        for key in ARTIFACT_KEYS
    }
    outputs = {"renders": summary(), "repaired": summary()}
    return detect_current_state.route_batch(
        "st03a-route",
        ROOT / "manifests" / "st03a-route.batch_manifest.json",
        manifest,
        inputs,
        drafts,
        artifacts,
        outputs,
    )


def valid_set_identity(component_count: int = 2) -> dict[str, object]:
    return {
        "artifact_type": "set_product_identity",
        "set_identity": {
            "set_name": "测试套装",
            "set_piece_count": component_count,
            "set_lock_description": "保持组成、件数、主次和相对比例不变。",
        },
        "components": [
            {"component_index": index, "component_name": f"单件 {index}"}
            for index in range(1, component_count + 1)
        ],
        "notes": "",
    }


def valid_layout_response(
    group_names: tuple[str, ...],
    component_names: tuple[str, ...],
) -> dict[str, object]:
    layouts: list[dict[str, object]] = []
    combined = (*group_names, *component_names)
    for index, filename in enumerate(combined, start=1):
        is_group = index <= len(group_names)
        layouts.append(
            {
                "layout_id": f"layout_{index:03d}",
                "image_index": index,
                "file_name": filename,
                "is_set_group": is_group,
                "overall_camera": "A",
                "camera_decision_basis": "正面略高机位，整体轮廓可见。",
                "layout_slot": "编排槽位一" if is_group else COMPONENT_LAYOUT_TEXT,
                "layout_decision_basis": (
                    "各单件同一水平面并列。" if is_group else COMPONENT_LAYOUT_TEXT
                ),
                "piece_count_check": (
                    "可见件数与套装产品身份档案一致。"
                    if is_group
                    else COMPONENT_LAYOUT_TEXT
                ),
                "component_visibility": "各组成单件清晰可见，无明显裁切。",
                "naturally_visible_content": "自然展示套装件数与组合关系。",
                "must_not_force_content": "不强行展示被遮挡的底部结构。",
                "suitable_page_tasks": "适合套装整体认知任务。",
                "unsuitable_page_tasks": "不适合精确尺寸说明任务。",
                "main_image_suitability": "适合：套装关系清楚。",
                "detail_image_suitability": "适合：可说明组成关系。",
                "risk_notes": "无明显风险。",
                "recommended_task_binding": "建议绑定套装组成与组合关系任务。",
                "admission_result": "合格，可进入对应机位与编排槽位",
                "merged_reference_note": "无",
            }
        )
    return {
        "artifact_type": "set_angle_layout_inventory",
        "layouts": layouts,
        "notes": "",
    }


class SequenceTransport:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, tuple[CodexAttachment, ...]]] = []

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.calls.append((prompt, attachments))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return CodexTurnResult(
            text=self.responses.pop(0),
            thread_id=f"thread-{len(self.calls)}",
        )

    def continue_turn(self, *_args: object) -> CodexTurnResult:
        raise AssertionError("set angle/layout inventory must use one fresh turn")


class ExecutorFixture(unittest.TestCase):
    def make_set_executor(
        self,
        root: Path,
        responses: list[str],
        *,
        component_names: tuple[str, ...] = ("b.png", "A.png"),
        group_names: tuple[str, ...] = ("z-group.png", "a-group.png"),
        identity_type: str = "set_product_identity",
        write_identity: bool = True,
        repository_root: Path = ROOT,
    ) -> tuple[CodexDevExecutor, SequenceTransport, Path, dict[str, object]]:
        workspace = root / "workspace"
        artifacts_root = workspace / "artifacts"
        component_dir = workspace / "inputs" / "component_white_bg"
        group_dir = workspace / "inputs" / "set_group"
        component_dir.mkdir(parents=True, exist_ok=True)
        group_dir.mkdir(parents=True, exist_ok=True)
        for filename in component_names:
            (component_dir / filename).write_bytes(filename.encode("utf-8"))
        for filename in group_names:
            (group_dir / filename).write_bytes(filename.encode("utf-8"))

        identity_dir = artifacts_root / "identity"
        if write_identity:
            identity_dir.mkdir(parents=True, exist_ok=True)
            identity = valid_set_identity(len(component_names))
            identity["artifact_type"] = identity_type
            (identity_dir / "set_product_identity.json").write_text(
                json.dumps(identity, ensure_ascii=False),
                encoding="utf-8",
            )

        output_dir = artifacts_root / "angle_inventory"
        manifest: dict[str, object] = {
            "product_id": "st03a-product",
            "batch_type": "set",
            "user_declared_set_product": True,
            "category": "杯类",
            "workspace": {
                "root": str(workspace),
                "artifacts_root": str(artifacts_root),
            },
            "inputs": {
                "component_white_bg_images": [str(component_dir)],
                "set_group_images": [str(group_dir)],
            },
            "artifacts": {
                "set_product_identity": str(identity_dir),
                "set_angle_layout_inventory": str(output_dir),
            },
        }
        manifest_path = root / "manifests" / "st03a-product.batch_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        transport = SequenceTransport(responses)
        executor = CodexDevExecutor(
            ExecutorContext(
                manifest=manifest,
                manifest_path=manifest_path,
                environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
            ),
            transport=transport,
            repository_root=repository_root,
        )
        return executor, transport, output_dir / "set_angle_layout_inventory.json", manifest


class St03aGateRoutingAndCommandTests(unittest.TestCase):
    def test_gate_matrix_opens_three_steps_and_keeps_six_blocked(self) -> None:
        ready = {"identity", "style_master", "angle_inventory"}
        self.assertEqual(frozenset(ready), batch_type_gate.SET_READY_STEPS)
        for step in STEPS:
            with self.subTest(batch_type="set", step=step):
                expected = None if step in ready else BLOCKED_MESSAGE
                self.assertEqual(
                    expected,
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "set"},
                        step,
                    ),
                )
            with self.subTest(batch_type="single", step=step):
                self.assertIsNone(
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "single"},
                        step,
                    )
                )
            with self.subTest(batch_type="invalid", step=step):
                self.assertEqual(
                    BLOCKED_MESSAGE,
                    batch_type_gate.set_batch_blocked_message(
                        {"batch_type": "invalid"},
                        step,
                    ),
                )

    def test_set_route_skips_single_angle_inventory_and_reaches_layout(self) -> None:
        upstream = {"product_identity_archive", "style_master", "set_product_identity"}
        route = route_fixture(batch_type="set", available=upstream)

        self.assertEqual("needs_set_angle_layout_inventory", route["current_stage"])
        self.assertEqual("set-angle-layout-inventory", route["next_required_skill"])
        self.assertNotEqual("needs_angle_inventory", route["current_stage"])
        self.assertEqual([], route["blocked_reasons"])

        completed = route_fixture(
            batch_type="set",
            available=upstream | {"set_angle_layout_inventory"},
        )
        self.assertEqual("needs_main_variable_configs", completed["current_stage"])

    def test_set_route_without_group_image_keeps_existing_blocked_semantics(self) -> None:
        route = route_fixture(
            batch_type="set",
            available={"product_identity_archive", "style_master", "set_product_identity"},
            group_count=0,
        )
        self.assertEqual("needs_set_angle_layout_inventory", route["current_stage"])
        self.assertEqual(
            ["No set group images found for set angle/layout inventory."],
            route["blocked_reasons"],
        )

    def test_single_route_still_stops_at_single_angle_inventory(self) -> None:
        route = route_fixture(
            batch_type="single",
            available={"product_identity_archive", "style_master"},
        )
        self.assertEqual("needs_angle_inventory", route["current_stage"])
        self.assertEqual("angle-inventory", route["next_required_skill"])

    def test_command_mapping_runnable_and_retryable_steps_split_by_batch_type(self) -> None:
        route = route_fixture(
            batch_type="set",
            available={"product_identity_archive", "style_master", "set_product_identity"},
        )
        self.assertEqual(
            "angle_inventory",
            run_controller.SKILL_TO_STEP["set-angle-layout-inventory"],
        )
        self.assertEqual(["angle_inventory"], run_controller.runnable_steps(route, {}))
        self.assertEqual(
            ["angle_inventory"],
            run_controller.retryable_steps(
                {
                    "batch_type": "set",
                    "available_artifacts": ["set_angle_layout_inventory"],
                },
                {},
            ),
        )
        self.assertEqual(
            ["angle_inventory"],
            run_controller.retryable_steps(
                {
                    "batch_type": "single",
                    "available_artifacts": ["angle_inventory"],
                },
                {},
            ),
        )
        self.assertNotIn(
            "angle_inventory",
            run_controller.retryable_steps(
                {
                    "batch_type": "set",
                    "available_artifacts": ["angle_inventory"],
                },
                {},
            ),
        )


class St03aExecutorTests(ExecutorFixture):
    def test_full_set_inventory_chain_orders_attachments_prompts_and_lands_schema_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            group_names = ("a-group.png", "z-group.png")
            component_names = ("A.png", "b.png")
            response = valid_layout_response(group_names, component_names)
            executor, transport, output_path, _manifest = self.make_set_executor(
                root,
                [f"```json\n{json.dumps(response, ensure_ascii=False)}\n```"],
            )

            result = executor.execute(ExecutionRequest(step="angle_inventory"))

            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "product_id",
                    "artifact_type",
                    "user_declared_set_product",
                    "set_group_assets",
                    "layouts",
                    "notes",
                },
                set(artifact),
            )
            self.assertEqual("st03a-product", artifact["product_id"])
            self.assertEqual("set_angle_layout_inventory", artifact["artifact_type"])
            self.assertIs(True, artifact["user_declared_set_product"])
            self.assertEqual(
                [
                    {"asset_id": "set_group_001", "file_path": "a-group.png"},
                    {"asset_id": "set_group_002", "file_path": "z-group.png"},
                ],
                artifact["set_group_assets"],
            )
            self.assertEqual(4, len(artifact["layouts"]))
            self.assertEqual(
                ["layout_001", "layout_002", "layout_003", "layout_004"],
                [item["layout_id"] for item in artifact["layouts"]],
            )
            self.assertFalse(
                {"angle_slots", "variable_configs", "final_prompt", "qc_results"}
                & set(artifact)
            )
            schema = json.loads(
                (ROOT / "schemas" / "set_angle_layout_inventory.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(set(schema["required"]).issubset(artifact))
            self.assertIs(True, artifact["user_declared_set_product"])
            self.assertFalse(
                any(set(item["required"]).issubset(artifact) for item in schema["not"]["anyOf"])
            )

            prompt, attachments = transport.calls[0]
            self.assertEqual(
                (*group_names, *component_names),
                tuple(attachment.name for attachment in attachments),
            )
            for source in (
                ROOT / ".agents" / "skills" / "set-angle-layout-inventory" / "SKILL.md",
                ROOT / "categories" / "_shared" / "prompts" / "set_angle_layout_inventory.md",
                ROOT / "categories" / "_shared" / "prompts" / "set_layout_rules.md",
            ):
                self.assertIn(source.read_text(encoding="utf-8"), prompt)
            self.assertIn('"set_piece_count": 2', prompt)
            self.assertIn('"image_index": 1', prompt)
            self.assertIn('"image_index": 3', prompt)
            self.assertEqual((output_path,), result.outputs)
            self.assertEqual("套装角度与编排入库表已生成", result.detail)
            self.assertEqual("thread-1", result.metadata["thread_id"])

    def test_quantity_gates_reject_component_and_group_bounds(self) -> None:
        cases = (
            ("component-low", ("one.png",), ("group.png",), "套装角度与编排入库要求 2–8 张组成单件白底图"),
            (
                "component-high",
                tuple(f"component-{index}.png" for index in range(9)),
                ("group.png",),
                "套装角度与编排入库要求 2–8 张组成单件白底图",
            ),
            ("group-low", ("a.png", "b.png"), (), "套装角度与编排入库要求 1–3 张套装合影图"),
            (
                "group-high",
                ("a.png", "b.png"),
                tuple(f"group-{index}.png" for index in range(4)),
                "套装角度与编排入库要求 1–3 张套装合影图",
            ),
        )
        for name, component_names, group_names, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                executor, transport, output_path, _manifest = self.make_set_executor(
                    Path(temporary),
                    ["{}"],
                    component_names=component_names,
                    group_names=group_names,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual([], transport.calls)
                self.assertFalse(output_path.exists())

    def test_set_identity_missing_or_wrong_type_fails_closed_before_transport(self) -> None:
        cases = (("missing", False, "set_product_identity"), ("wrong-type", True, "product_identity_archive"))
        for name, write_identity, identity_type in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                executor, transport, output_path, _manifest = self.make_set_executor(
                    Path(temporary),
                    ["{}"],
                    write_identity=write_identity,
                    identity_type=identity_type,
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))
                self.assertEqual(
                    "codex-dev 无法读取有效的套装产品身份档案",
                    str(caught.exception),
                )
                self.assertEqual([], transport.calls)
                self.assertFalse(output_path.exists())

    def test_parse_matrix_rejects_each_contract_violation_exactly(self) -> None:
        group_names = ("a-group.png", "z-group.png")
        component_names = ("A.png", "b.png")
        cases: dict[str, tuple[dict[str, object], str]] = {}

        wrong_count = valid_layout_response(group_names, component_names)
        wrong_count["layouts"].pop()
        cases["wrong-count"] = (wrong_count, "codex-dev 返回格式异常：套装角度与编排条目数量无效")

        wrong_filename = valid_layout_response(group_names, component_names)
        wrong_filename["layouts"][0]["file_name"] = "other.png"
        cases["wrong-filename"] = (wrong_filename, "codex-dev 返回格式异常：文件名对应关系无效")

        wrong_camera = valid_layout_response(group_names, component_names)
        wrong_camera["layouts"][0]["overall_camera"] = "E"
        cases["wrong-camera"] = (wrong_camera, "codex-dev 返回格式异常：整体机位无效")

        missing_slot = valid_layout_response(group_names, component_names)
        missing_slot["layouts"][0].pop("layout_slot")
        cases["missing-layout-slot"] = (
            missing_slot,
            "codex-dev 返回格式异常：套装角度与编排条目字段无效",
        )

        wrong_slot = valid_layout_response(group_names, component_names)
        wrong_slot["layouts"][0]["layout_slot"] = "随意编排"
        cases["wrong-layout-slot"] = (wrong_slot, "codex-dev 返回格式异常：套装编排槽位无效")

        wrong_admission = valid_layout_response(group_names, component_names)
        wrong_admission["layouts"][0]["admission_result"] = "可以入库"
        cases["wrong-admission"] = (wrong_admission, "codex-dev 返回格式异常：入库结论无效")

        unknown_top = valid_layout_response(group_names, component_names)
        unknown_top["unknown"] = "PRIVATE"
        cases["unknown-top"] = (unknown_top, "codex-dev 返回格式异常：包含未声明顶层字段")

        injected = valid_layout_response(group_names, component_names)
        injected["product_id"] = "injected"
        cases["injected-domain"] = (injected, "codex-dev 返回格式异常：包含代码注入域字段")

        workflow = valid_layout_response(group_names, component_names)
        workflow["angle_slots"] = []
        cases["workflow-output"] = (workflow, "codex-dev 返回格式异常：包含越界工作流产物")

        damaged = valid_layout_response(group_names, component_names)
        damaged["layouts"][0]["risk_notes"] = "损坏�字符"
        cases["replacement-character"] = (damaged, "codex-dev 返回格式异常：文本包含损坏字符")

        for name, (response, message) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                executor, transport, output_path, _manifest = self.make_set_executor(
                    Path(temporary),
                    [json.dumps(response, ensure_ascii=False)],
                )
                with self.assertRaises(ExecutorExecutionError) as caught:
                    executor.execute(ExecutionRequest(step="angle_inventory"))
                self.assertEqual(message, str(caught.exception))
                self.assertEqual(1, len(transport.calls))
                self.assertFalse(output_path.exists())

    def test_layout_item_extra_field_is_rejected_by_closed_contract(self) -> None:
        group_names = ("a-group.png", "z-group.png")
        component_names = ("A.png", "b.png")
        response = valid_layout_response(group_names, component_names)
        response["layouts"][0]["extra_field"] = "x"

        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, output_path, _manifest = self.make_set_executor(
                Path(temporary),
                [json.dumps(response, ensure_ascii=False)],
            )
            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="angle_inventory"))
            self.assertEqual(
                "codex-dev 返回格式异常：套装角度与编排条目字段无效",
                str(caught.exception),
            )
            self.assertEqual(1, len(transport.calls))
            self.assertFalse(output_path.exists())

    def test_existing_artifact_blocks_whole_step_retry_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor, transport, output_path, _manifest = self.make_set_executor(
                Path(temporary),
                ["{}"],
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text('{"preserve": true}', encoding="utf-8")

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="angle_inventory"))

            self.assertEqual(
                "套装角度与编排入库表已存在，codex-dev 不会覆盖",
                str(caught.exception),
            )
            self.assertEqual('{"preserve": true}', output_path.read_text(encoding="utf-8"))
            self.assertEqual([], transport.calls)

    def test_rule_loader_failure_is_unified_and_never_reads_legacy_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor, transport, output_path, _manifest = self.make_set_executor(
                root,
                ["{}"],
                repository_root=root,
            )
            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="angle_inventory"))
            self.assertEqual(
                "codex-dev 无法加载套装角度与编排入库规则",
                str(caught.exception),
            )
            self.assertEqual([], transport.calls)
            self.assertFalse(output_path.exists())
            source = (ROOT / "canvas-bridge" / "codex_dev_executor.py").read_text(encoding="utf-8")
            self.assertNotIn('skill_root / "references"', source)


class St03aStyleMasterTests(ExecutorFixture):
    def test_set_style_master_uses_set_identity_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor, transport, _angle_output, manifest = self.make_set_executor(
                root,
                [json.dumps(VALID_STYLE_MASTER, ensure_ascii=False)],
            )
            workspace = Path(manifest["workspace"]["root"])
            style_dir = workspace / "inputs" / "style_refs"
            style_dir.mkdir(parents=True)
            (style_dir / "style.png").write_bytes(b"offline-style")
            style_output = Path(manifest["workspace"]["artifacts_root"]) / "style_master"
            manifest["inputs"]["style_reference_images"] = [str(style_dir)]
            manifest["artifacts"]["style_master"] = str(style_output)

            result = executor.execute(ExecutionRequest(step="style_master"))

            output_path = style_output / "style_master.json"
            self.assertTrue(output_path.is_file())
            prompt, attachments = transport.calls[0]
            self.assertIn('"artifact_type": "set_product_identity"', prompt)
            self.assertIn('"set_piece_count": 2', prompt)
            self.assertEqual(("style.png",), tuple(item.name for item in attachments))
            self.assertEqual((output_path,), result.outputs)

    def test_single_style_master_prompt_matches_prechange_byte_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "categories", root / "categories")
            style_skill = root / ".agents" / "skills" / "style-master-extractor"
            style_skill.mkdir(parents=True)
            (style_skill / "SKILL.md").write_text(
                "STYLE_SKILL_MARKER: 只提取视觉风格",
                encoding="utf-8",
            )
            (root / "categories" / "杯类" / "prompts" / "style.md").write_text(
                "STYLE_REFERENCE_MARKER: 不得覆盖产品身份",
                encoding="utf-8",
            )

            workspace = root / "workspace"
            artifacts_root = workspace / "artifacts"
            white_dir = workspace / "inputs" / "white_bg"
            style_dir = workspace / "inputs" / "style_refs"
            white_dir.mkdir(parents=True)
            style_dir.mkdir(parents=True)
            (white_dir / "front.jpg").write_bytes(b"offline-jpeg")
            (style_dir / "style.png").write_bytes(b"offline-png")
            identity_dir = artifacts_root / "identity"
            identity_dir.mkdir(parents=True)
            identity = dict(VALID_SINGLE_IDENTITY)
            identity["product_id"] = "p1"
            (identity_dir / "product_identity_archive.json").write_text(
                json.dumps(identity, ensure_ascii=False),
                encoding="utf-8",
            )
            style_output = artifacts_root / "style_master"
            manifest = {
                "product_id": "p1",
                "batch_type": "single",
                "notes": "只使用可见信息",
                "workspace": {"artifacts_root": str(artifacts_root)},
                "inputs": {
                    "white_bg_images": [str(white_dir)],
                    "style_reference_images": [str(style_dir)],
                },
                "artifacts": {
                    "product_identity_archive": str(identity_dir),
                    "style_master": str(style_output),
                },
            }
            transport = SequenceTransport([json.dumps(VALID_STYLE_MASTER, ensure_ascii=False)])
            executor = CodexDevExecutor(
                ExecutorContext(
                    manifest=manifest,
                    manifest_path=root / "manifests" / "p1.batch_manifest.json",
                    environment={"CODEX_DEV_ALLOW_REAL_EXECUTION": "1"},
                ),
                transport=transport,
                repository_root=root,
            )

            executor.execute(ExecutionRequest(step="style_master"))

            prompt = transport.calls[0][0].encode("utf-8")
            self.assertEqual(4513, len(prompt))
            self.assertEqual(
                "dcd9561950012bda7420b4bf32474b8ddf24728517d6484912552a9389fc3357",
                hashlib.sha256(prompt).hexdigest(),
            )


class St03aTeachingAndGraphTests(unittest.TestCase):
    def test_shared_prompt_sources_are_byte_equal_to_root_originals(self) -> None:
        pairs = (
            (
                "set_angle_layout_prompt",
                ROOT / "套装角度与编排入库表提示词.txt",
            ),
            ("set_layout_rules", ROOT / "套装编排规则.txt"),
        )
        for key, source in pairs:
            with self.subTest(key=key):
                self.assertEqual(
                    source.read_bytes(),
                    load_shared_prompt(ROOT, key).encode("utf-8"),
                )

    def test_shared_prompt_loader_fails_closed_for_unknown_missing_empty_and_whitespace(self) -> None:
        with self.assertRaisesRegex(CategoryRecipeError, "共享提示词键无效"):
            load_shared_prompt(ROOT, "unknown-set-prompt")
        cases = (("missing", None), ("empty", ""), ("whitespace", " \r\n\t"))
        for name, content in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                prompt_dir = root / "categories" / "_shared" / "prompts"
                prompt_dir.mkdir(parents=True)
                if content is not None:
                    (prompt_dir / "set_angle_layout_inventory.md").write_text(
                        content,
                        encoding="utf-8",
                    )
                with self.assertRaises(CategoryRecipeError):
                    load_shared_prompt(root, "set_angle_layout_prompt")

    def test_single_only_truth_table_and_active_subgraphs(self) -> None:
        condition = {"when": "single_only"}
        self.assertTrue(
            projector.condition_active(condition, set_enabled=False, requested=[])
        )
        self.assertFalse(
            projector.condition_active(condition, set_enabled=True, requested=[])
        )
        graph = json.loads(
            (ROOT / "manifests" / "workflow_graph.template.json").read_text(
                encoding="utf-8"
            )
        )
        single_nodes, single_edges = projector.active_subgraph(
            graph,
            {"batch_type": "single", "requested_outputs": ["main", "detail"]},
        )
        set_nodes, set_edges = projector.active_subgraph(
            graph,
            {
                "batch_type": "set",
                "user_declared_set_product": True,
                "requested_outputs": ["main", "detail"],
            },
        )
        for node_id in ("stage_angle_inventory", "art_angle_inventory"):
            self.assertIn(node_id, single_nodes)
            self.assertNotIn(node_id, set_nodes)
        new_set_edges = {
            ("art_style_master", "stage_set_product_identity"),
            ("in_set_group", "stage_set_product_identity"),
            ("in_component_white_bg", "stage_set_angle_layout"),
        }
        self.assertTrue(
            new_set_edges.issubset(
                {(edge["from"], edge["to"]) for edge in set_edges}
            )
        )
        self.assertTrue(
            new_set_edges.isdisjoint(
                {(edge["from"], edge["to"]) for edge in single_edges}
            )
        )

    def test_graph_nodes_edges_and_version_match_corrected_set_route(self) -> None:
        graph = json.loads(
            (ROOT / "manifests" / "workflow_graph.template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("2026-08-10", graph["version"])
        self.assertEqual(1, graph["graph_version"])
        by_id = {node["id"]: node for node in graph["nodes"]}
        for node_id in ("stage_angle_inventory", "art_angle_inventory"):
            self.assertEqual({"when": "single_only"}, by_id[node_id]["condition"])

        edges = graph["edges"]
        self.assertFalse(
            any(
                edge["from"] == "art_angle_inventory"
                and edge["to"] == "stage_set_product_identity"
                for edge in edges
            )
        )
        expected = (
            ("art_style_master", "stage_set_product_identity", "style_master", "sequence"),
            ("in_set_group", "stage_set_product_identity", "set_group_image", "data"),
            (
                "in_component_white_bg",
                "stage_set_angle_layout",
                "component_white_bg_image",
                "data",
            ),
        )
        for source, target, port, kind in expected:
            matching = [
                edge
                for edge in edges
                if edge["from"] == source and edge["to"] == target
            ]
            self.assertEqual(1, len(matching), (source, target))
            self.assertEqual(port, matching[0]["port"])
            self.assertEqual(kind, matching[0]["edge_kind"])
            self.assertEqual({"when": "set_enabled"}, matching[0]["condition"])


if __name__ == "__main__":
    unittest.main()
