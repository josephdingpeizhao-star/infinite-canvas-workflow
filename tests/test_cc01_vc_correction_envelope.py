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

from category_recipes import CategoryRecipe  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    UserConfirmedRequirements,
    build_variable_config_correction_prompt,
    parse_user_confirmed_requirements,
)
from codex_dev_executor import CodexDevExecutor, CodexTurnResult  # noqa: E402
from content_correction import (  # noqa: E402
    ContentPredicateViolation,
    build_content_correction_instruction,
)
from executor_contract import ExecutionRequest, ExecutorExecutionError  # noqa: E402
from image_count_contract import chinese_image_count, config_ids  # noqa: E402
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    valid_main_variable_response,
)


ALLOWED_TOP_LEVEL_KEYS = [
    "common_constraints",
    "configs",
    "handheld_count_summary",
    "notes",
]


def _requirements(main_image_count: int) -> UserConfirmedRequirements:
    image_counts = {
        mode: {"default": 1, "minimum": 1, "maximum": 30}
        for mode in ("main", "detail")
    }
    recipe = CategoryRecipe(
        key="test",
        display_name="test",
        product_noun="test",
        business_review_status="test",
        form={"image_counts": image_counts},
        lexicons={},
        prompts={},
        runtime_packages={},
        qc_documents={},
        content_sha256="test",
    )
    return UserConfirmedRequirements(
        product_type="杯子",
        height_cm=25,
        handheld_main=0,
        handheld_detail=0,
        allow_clear_water=False,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        main_image_count=main_image_count,
        detail_image_count=1,
        category=recipe.key,
        recipe=recipe,
    )


def _canvas_ratio_violation() -> ContentPredicateViolation:
    return ContentPredicateViolation(
        "codex-dev 收到的主图变量配置输出画布比例不符合要求",
        code="canvas_ratio",
        config_id="main_01",
        field="输出画布比例",
        expected="必须逐字写 1:1",
    )


def _invalid_canvas_ratio_response() -> dict[str, object]:
    invalid = valid_main_variable_response()
    invalid["configs"][0]["per_image_overrides"]["输出画布比例"] = "3:4"  # type: ignore[index]
    return invalid


class VariableConfigCorrectionEnvelopeUnitTest(unittest.TestCase):
    def test_prompt_repeats_complete_envelope_for_small_and_double_digit_counts(
        self,
    ) -> None:
        correction = _canvas_ratio_violation()
        raw_instruction = build_content_correction_instruction(correction)
        allowed_keys = json.dumps(ALLOWED_TOP_LEVEL_KEYS, ensure_ascii=False)

        for image_count in (2, 12):
            with self.subTest(image_count=image_count):
                prompt = build_variable_config_correction_prompt(
                    correction,
                    mode="main",
                    requirements=_requirements(image_count),
                )
                identifiers = config_ids("main", image_count)

                self.assertTrue(
                    prompt.startswith("继续同一 main_vc 任务。" + raw_instruction)
                )
                self.assertIn(f"顶层键仅允许：{allowed_keys}。", prompt)
                self.assertIn(
                    "configs 必须按顺序包含全部"
                    f"{chinese_image_count(image_count)}项配置："
                    + "、".join(identifiers)
                    + "。",
                    prompt,
                )
                for config_id in identifiers:
                    self.assertIn(config_id, prompt)
                self.assertIn(
                    "每项只包含 config_id、per_image_overrides、notes。",
                    prompt,
                )
                self.assertIn("common_constraints 必须是非空 JSON 对象", prompt)
                self.assertIn("handheld_count_summary 必须是 JSON 对象", prompt)
                self.assertIn("不得只重发单条配置", prompt)
                self.assertTrue(
                    prompt.endswith(
                        "只返回一个完整 JSON 对象，不要 Markdown、代码围栏或额外说明。"
                    )
                )

    def test_prompt_rejects_invalid_mode_with_safe_error(self) -> None:
        unsafe_mode = "https://secret.example/private"
        with self.assertRaises(ExecutorExecutionError) as caught:
            build_variable_config_correction_prompt(
                _canvas_ratio_violation(),
                mode=unsafe_mode,
                requirements=_requirements(2),
            )

        self.assertEqual(
            "codex-dev 收到不支持的变量配置模式",
            str(caught.exception),
        )
        self.assertNotIn(unsafe_mode, str(caught.exception))


class VariableConfigCorrectionEnvelopeExecutorTest(CodexDevFixture):
    def test_main_correction_resends_complete_document_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path = self.make_downstream_fixture(root)
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(
                            _invalid_canvas_ratio_response(),
                            ensure_ascii=False,
                        ),
                        thread_id="thread-cc01-complete",
                    ),
                    CodexTurnResult(
                        text=json.dumps(
                            valid_main_variable_response(),
                            ensure_ascii=False,
                        ),
                        thread_id="thread-cc01-complete",
                    ),
                ]
            )
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(lambda *_: None)

            result = executor.execute(ExecutionRequest(step="main_vc"))

            requirements = parse_user_confirmed_requirements(context.manifest, root)
            expected_prompt = build_variable_config_correction_prompt(
                _canvas_ratio_violation(),
                mode="main",
                requirements=requirements,
            )
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(
                [("thread-cc01-complete", expected_prompt, ())],
                transport.continuation_calls,
            )
            self.assertEqual((output_path,), result.outputs)
            self.assertTrue(output_path.exists())
            artifact = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(6, artifact["config_count"])
            self.assertEqual(
                list(config_ids("main", 6)),
                [item["config_id"] for item in artifact["configs"]],
            )

    def test_single_config_correction_response_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            context, output_path = self.make_downstream_fixture(root)
            single_config = valid_main_variable_response()["configs"][0]  # type: ignore[index]
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(
                            _invalid_canvas_ratio_response(),
                            ensure_ascii=False,
                        ),
                        thread_id="thread-cc01-single",
                    ),
                    CodexTurnResult(
                        text=json.dumps(single_config, ensure_ascii=False),
                        thread_id="thread-cc01-single",
                    ),
                ]
            )
            executor = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            )
            executor.set_content_correction_callback(lambda *_: None)

            with self.assertRaises(ExecutorExecutionError) as caught:
                executor.execute(ExecutionRequest(step="main_vc"))

            self.assertEqual(
                "codex-dev 收到的主图变量配置包含越界顶层字段",
                str(caught.exception),
            )
            self.assertEqual(1, len(transport.continuation_calls))
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
