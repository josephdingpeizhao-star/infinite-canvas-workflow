from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from batch_intake_controller import BatchIntakeGateError, _parse_facts  # noqa: E402
from codex_dev_downstream import parse_user_confirmed_requirements  # noqa: E402
from codex_dev_executor import CodexDevExecutor, CodexTurnResult  # noqa: E402
from content_correction import CONTENT_CORRECTION_CODES  # noqa: E402
from executor_contract import ExecutionRequest, ExecutorExecutionError  # noqa: E402
from image_count_contract import handheld_count_maximum  # noqa: E402
from workflow_production_service import WorkflowProductionService  # noqa: E402
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    detail_chunk_turns,
    valid_detail_chunk_responses,
    valid_main_variable_response,
)


EXPECTED_CONTENT_CORRECTION_CODES = {
    "unsupported_claims",
    "scene_policy",
    "common_constraints",
    "required_fields",
    "field_content",
    "angle_binding",
    "canvas_ratio",
    "confirmed_height_literal",
    "handheld_reference",
    "module_coverage",
    "module05_handheld",
    "module05_height_literal",
    "module05_forbidden_terms",
    "size_annotation_scope",
    "chunk_coverage",
    "handheld_count",
    "handheld_summary",
    "set_key_scope",
}
HARD_PRIORITY = "模块05硬约束优先于手持名额分配"
HARD_ALLOCATION = "手持名额只在标准模块归属不包含模块05的图位之间分配"
N_MINUS_ONE = "详情图手持上限限制为详情图张数 N−1"


def _field_teaching_block(text: str, field: str) -> str:
    marker = f"\n【{field}】\n"
    start = text.index(marker) + len(marker)
    remainder = text[start:]
    next_heading = re.search(r"\n【[^】\n]+】", remainder)
    return remainder[: next_heading.start() if next_heading else None]


def _intake_facts(detail_count: int, handheld_detail: int) -> dict[str, object]:
    return {
        "product_type": "杯子",
        "length_cm": None,
        "width_cm": None,
        "height_cm": 25,
        "main_image_count": detail_count,
        "detail_image_count": detail_count,
        "handheld_main": detail_count,
        "handheld_detail": handheld_detail,
        "forbid_pouring_and_heating": True,
        "missing_d_no_retake": True,
    }


def _module05_handheld_chunk(
    chunk: dict[str, object],
) -> dict[str, object]:
    result = copy.deepcopy(chunk)
    config = result["configs"][0]  # type: ignore[index]
    overrides = config["per_image_overrides"]  # type: ignore[index]
    overrides["手持交互声明"] = (  # type: ignore[index]
        "本张图启用手持场景。手持子场景类型：静态握持。"
        "单手自然握住把手，不离桌，不倾倒"
    )
    overrides["动态手持样式参考图调用"] = (  # type: ignore[index]
        "无，仅动态拿起场景可调用"
    )
    return result


class Cat08Module05HandheldContractTest(CodexDevFixture):
    def test_content_correction_code_table_is_closed_and_stable(self) -> None:
        self.assertEqual(EXPECTED_CONTENT_CORRECTION_CODES, CONTENT_CORRECTION_CODES)

    def test_teaching_and_detail_template_state_the_hard_priority(self) -> None:
        teaching_paths = (
            ROOT / "详情图单张变量配置提示词生成.txt",
            ROOT
            / ".agents"
            / "skills"
            / "detail-variable-config"
            / "references"
            / "详情图单张变量配置提示词生成.txt",
            ROOT
            / ".codex"
            / "skills"
            / "detail-variable-config"
            / "references"
            / "详情图单张变量配置提示词生成.txt",
        )
        source_texts = tuple(path.read_text(encoding="utf-8") for path in teaching_paths)
        self.assertEqual(1, len(set(source_texts)))
        self.assertEqual(704, len(source_texts[0].splitlines()))

        runtime_packages: dict[str, dict[str, object]] = {}
        runtime_texts: list[str] = []
        for category in ("杯类", "碗", "盘子"):
            package = json.loads(
                (ROOT / "categories" / category / "runtime" / "detail.json").read_text(
                    encoding="utf-8"
                )
            )
            runtime_packages[category] = package
            runtime_texts.append("\n".join(item["text"] for item in package["slices"]))

        for text in (*source_texts, *runtime_texts):
            with self.subTest(text_length=len(text)):
                self.assertIn(HARD_ALLOCATION, text)
                self.assertIn(N_MINUS_ONE, text)
                single_product_text = text.replace(
                    "套装产品尺寸标注图默认不启用手持，也不调用动态手持样式参考图。",
                    "",
                )
                self.assertNotIn("模块05默认不启用手持", single_product_text)
                self.assertNotIn("模块05通常不启用手持", single_product_text)
                self.assertNotIn("产品尺寸标注图默认不启用手持", single_product_text)

        for category_text in runtime_texts[1:]:
            self.assertIn(HARD_PRIORITY, category_text)

        hf02_ids = {
            "杯类": "detail_required_fields_core",
            "碗": "bowl-detail-hf02-required-field-teaching",
            "盘子": "plate-detail-hf02-required-field-teaching",
        }
        hf03_ids = {
            "杯类": "detail_handheld_enable_rule",
            "碗": "bowl-detail-hf03-handheld-enable-rule",
            "盘子": "plate-detail-hf03-handheld-enable-rule",
        }
        fields = ("手持交互声明", "动态手持样式参考图调用")
        for category, package in runtime_packages.items():
            slices = {
                item["slice_id"]: item["text"]
                for item in package["slices"]
            }
            for field in fields:
                matrix = {
                    "蓝本": _field_teaching_block(source_texts[0], field),
                    "品类 hf02 字段契约": _field_teaching_block(
                        slices[hf02_ids[category]], field
                    ),
                    "品类 hf03 手持规则": slices[hf03_ids[category]],
                }
                for layer, teaching in matrix.items():
                    with self.subTest(category=category, layer=layer, field=field):
                        self.assertIn("标准模块归属包含模块05", teaching)
                        self.assertIn("硬约束优先于手持名额分配", teaching)
                        self.assertIn(HARD_ALLOCATION, teaching)
                        self.assertIn(N_MINUS_ONE, teaching)
                        if field == "手持交互声明":
                            self.assertIn("本张图不启用手持场景", teaching)
                        else:
                            self.assertIn("不得调用动态手持样式参考图", teaching)

        downstream_source = (BRIDGE / "codex_dev_downstream.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "标准模块归属包含模块05的项必须不启用手持",
            downstream_source,
        )
        self.assertIn(HARD_ALLOCATION, downstream_source)

    def test_backend_detail_handheld_boundary_matrix_uses_n_minus_one(self) -> None:
        for detail_count in (1, 2, 8, 30):
            maximum = detail_count - 1
            with self.subTest(detail_count=detail_count, handheld_detail=maximum):
                self.assertEqual(
                    maximum,
                    handheld_count_maximum("detail", detail_count),
                )
                parsed = parse_user_confirmed_requirements(
                    {
                        "category": "杯类",
                        "user_confirmed_facts": _intake_facts(
                            detail_count,
                            maximum,
                        ),
                    },
                    ROOT,
                )
                self.assertEqual(maximum, parsed.handheld_detail)

            with self.subTest(detail_count=detail_count, handheld_detail=detail_count):
                with self.assertRaises(ExecutorExecutionError) as caught:
                    parse_user_confirmed_requirements(
                        {
                            "category": "杯类",
                            "user_confirmed_facts": _intake_facts(
                                detail_count,
                                detail_count,
                            ),
                        },
                        ROOT,
                    )
                self.assertEqual(
                    "codex-dev 缺少有效的用户确认商品信息",
                    str(caught.exception),
                )
                with self.assertRaises(BatchIntakeGateError) as intake_caught:
                    _parse_facts(
                        _intake_facts(detail_count, detail_count),
                        category="杯类",
                        repository_root=ROOT,
                        info_node_id="info-cat08",
                        request_id="request-cat08",
                    )
                self.assertEqual("invalid_facts", intake_caught.exception.code)
                self.assertEqual(
                    "含尺寸标注的详情图位不可手持，"
                    f"详情图手持最多 {maximum} 张。",
                    intake_caught.exception.user_message,
                )

    def test_main_content_violation_gets_one_correction_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path = self.make_downstream_fixture(root)
            invalid = valid_main_variable_response()
            invalid["configs"][0]["per_image_overrides"]["输出画布比例"] = "3:4"  # type: ignore[index]
            valid = valid_main_variable_response()
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="thread-main-cat08",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid, ensure_ascii=False),
                        thread_id="thread-main-cat08",
                    ),
                ]
            )
            corrections: list[tuple[int, str, str]] = []
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(
                lambda chunk_index, code, config_id: corrections.append(
                    (chunk_index, code, config_id)
                )
            )

            executor.execute(ExecutionRequest(step="main_vc"))

            self.assertTrue(output_path.exists())
            self.assertEqual([(1, "canvas_ratio", "main_01")], corrections)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(1, len(transport.continuation_calls))
            correction_prompt = transport.continuation_calls[0][1]
            self.assertIn(
                "配置 ID：main_01；违规字段：输出画布比例；必须满足：必须逐字写 1:1。"
                "其余内容不变，完整重发本段。",
                correction_prompt,
            )
            self.assertIn(
                '["common_constraints", "configs", "handheld_count_summary", "notes"]',
                correction_prompt,
            )
            for config_id in (f"main_{index:02d}" for index in range(1, 7)):
                self.assertIn(config_id, correction_prompt)
            self.assertIn("不得只重发单条配置", correction_prompt)
            self.assertIn("只返回一个完整 JSON 对象", correction_prompt)

    def test_detail_module05_correction_writes_safe_event_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path, _ = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            invalid_chunk = _module05_handheld_chunk(chunks[2])
            transport = FakeTransport(
                detail_chunk_turns(
                    [chunks[0], chunks[1], invalid_chunk, chunks[2], chunks[3]],
                    thread_id="thread-detail-cat08",
                )
            )
            journal = root / "manifests" / "p1.events.jsonl"
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(
                lambda chunk_index, code, config_id: WorkflowProductionService._record_content_correction(
                    journal,
                    "request-cat08",
                    "detail_vc",
                    chunk_index,
                    code,
                    config_id,
                )
            )

            executor.execute(ExecutionRequest(step="detail_vc"))

            self.assertTrue(output_path.exists())
            correction_prompts = [
                prompt
                for _, prompt, _ in transport.continuation_calls
                if "配置 ID：detail_05" in prompt
            ]
            self.assertEqual(1, len(correction_prompts))
            self.assertIn("违规字段：手持交互声明、动态手持样式参考图调用", correction_prompts[0])
            self.assertIn("本张图不启用手持场景", correction_prompts[0])
            self.assertIn("其余内容不变，完整重发本段", correction_prompts[0])
            events = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1, len(events))
            self.assertEqual(
                {
                    "event": "content_correction",
                    "request_id": "request-cat08",
                    "step": "detail_vc",
                    "chunk_index": 3,
                    "code": "module05_handheld",
                    "config_id": "detail_05",
                },
                {key: value for key, value in events[0].items() if key != "ts"},
            )
            self.assertNotIn("手持子场景类型", json.dumps(events, ensure_ascii=False))

    def test_second_detail_content_violation_fails_without_looping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path, _ = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            invalid_chunk = _module05_handheld_chunk(chunks[2])
            transport = FakeTransport(
                detail_chunk_turns(
                    [chunks[0], chunks[1], invalid_chunk, invalid_chunk],
                    thread_id="thread-detail-cat08-fail",
                )
            )
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(lambda *_: None)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="detail_vc"))

            self.assertEqual(
                "codex-dev 收到的详情图变量配置模块05规则异常",
                str(caught.exception),
            )
            self.assertFalse(output_path.exists())
            self.assertEqual(4, len(transport.calls) + len(transport.continuation_calls))
            self.assertEqual(
                1,
                sum(
                    "配置 ID：detail_05" in prompt
                    for _, prompt, _ in transport.continuation_calls
                ),
            )

    def test_transport_repair_and_content_correction_have_independent_quotas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path, _ = self.make_detail_fixture(root)
            chunks = valid_detail_chunk_responses()
            invalid_chunk = _module05_handheld_chunk(chunks[2])
            thread_id = "thread-detail-cat08-independent"
            turns = [
                CodexTurnResult(text='{"chunk_index": 1', thread_id=thread_id),
                *detail_chunk_turns(
                    [chunks[0], chunks[1], invalid_chunk, chunks[2], chunks[3]],
                    thread_id=thread_id,
                ),
            ]
            transport = FakeTransport(turns)
            corrections: list[tuple[int, str, str]] = []
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(
                lambda chunk_index, code, config_id: corrections.append(
                    (chunk_index, code, config_id)
                )
            )

            executor.execute(ExecutionRequest(step="detail_vc"))

            self.assertTrue(output_path.exists())
            self.assertEqual([(3, "module05_handheld", "detail_05")], corrections)
            prompts = [prompt for _, prompt, _ in transport.continuation_calls]
            self.assertEqual(1, sum("传输完整性门禁" in prompt for prompt in prompts))
            self.assertEqual(1, sum("配置 ID：detail_05" in prompt for prompt in prompts))
            self.assertEqual(6, len(transport.calls) + len(transport.continuation_calls))


if __name__ == "__main__":
    unittest.main()
