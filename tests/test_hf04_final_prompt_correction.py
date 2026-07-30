from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
TESTS = ROOT / "tests"
for extra in (BRIDGE, TESTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from codex_dev_executor import CodexAttachment, CodexDevExecutor, CodexTurnResult  # noqa: E402
from executor_contract import ExecutionRequest, ExecutorExecutionError  # noqa: E402
from test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    valid_final_prompt_response,
)


class _ContinuationFailureTransport(FakeTransport):
    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        self.continuation_calls.append((thread_id, prompt, attachments))
        raise RuntimeError("repair transport stopped")


class FinalPromptCorrectionBudgetTest(CodexDevFixture):
    @staticmethod
    def paraphrased_response(mode: str) -> dict[str, object]:
        response = copy.deepcopy(valid_final_prompt_response(mode))
        ratio = "1:1" if mode == "main" else "3:4"
        response["prompts"][0]["final_prompt"] = response["prompts"][0][
            "final_prompt"
        ].replace(
            f"画布比例固定为 {ratio}",
            f"输出画布比例：{ratio}",
        )
        return response

    def test_two_corrections_still_invalid_stop_before_detail_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid = self.paraphrased_response("main")
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="thread-main-budget",
                    ),
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="thread-main-budget",
                    ),
                    CodexTurnResult(
                        text=json.dumps(invalid, ensure_ascii=False),
                        thread_id="thread-main-budget",
                    ),
                ]
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "主图最终提示词纠正已达到上限",
            ) as caught:
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            failure_detail = str(caught.exception)
            self.assertIn("未保留画布比例", failure_detail)
            self.assertLessEqual(len(failure_detail), 160)
            self.assertNotIn("输出画布比例", failure_detail)
            self.assertEqual(1, len(transport.calls))
            self.assertEqual(2, len(transport.continuation_calls))
            self.assertEqual(
                ("thread-main-budget", "thread-main-budget"),
                tuple(call[0] for call in transport.continuation_calls),
            )
            self.assertTrue(
                all("画布比例固定为 1:1" in call[1] for call in transport.continuation_calls)
            )
            self.assertTrue(
                all("高度约 25 厘米" in call[1] for call in transport.continuation_calls)
            )
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_clear_water_business_violation_stops_before_literal_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            context = self.with_structured_facts(context, allow_clear_water=False)
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(valid_final_prompt_response("main"), ensure_ascii=False),
                    thread_id="thread-main-clear-water-business-stop",
                )
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "场景边界"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(1, len(transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_material_business_violation_stops_before_literal_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = valid_final_prompt_response("main")
            invalid_main["prompts"][0]["final_prompt"] += "；杯身为玻璃。"
            transport = FakeTransport(
                CodexTurnResult(
                    text=json.dumps(invalid_main, ensure_ascii=False),
                    thread_id="thread-main-material-business-stop",
                )
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "未确认商品事实"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(1, len(transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_correction_transport_failure_stops_without_retry_or_detail_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = self.paraphrased_response("main")
            transport = _ContinuationFailureTransport(
                CodexTurnResult(
                    text=json.dumps(invalid_main, ensure_ascii=False),
                    thread_id="thread-main-transport-failure",
                )
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "RuntimeError: repair transport stopped",
            ):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(1, len(transport.calls))
            self.assertEqual(1, len(transport.continuation_calls))
            self.assertEqual(
                "thread-main-transport-failure",
                transport.continuation_calls[0][0],
            )
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_main_and_detail_share_one_two_correction_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = self.paraphrased_response("main")
            invalid_detail = self.paraphrased_response("detail")
            transport = FakeTransport(
                [
                    CodexTurnResult(
                        text=json.dumps(invalid_main, ensure_ascii=False),
                        thread_id="thread-main-shared-budget",
                    ),
                    CodexTurnResult(
                        text=json.dumps(valid_final_prompt_response("main"), ensure_ascii=False),
                        thread_id="thread-main-shared-budget",
                    ),
                    CodexTurnResult(
                        text=json.dumps(invalid_detail, ensure_ascii=False),
                        thread_id="thread-detail-shared-budget",
                    ),
                    CodexTurnResult(
                        text=json.dumps(invalid_detail, ensure_ascii=False),
                        thread_id="thread-detail-shared-budget",
                    ),
                ]
            )

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "详情图最终提示词纠正已达到上限",
            ):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(2, len(transport.calls))
            self.assertEqual(2, len(transport.continuation_calls))
            self.assertEqual(
                ("thread-main-shared-budget", "thread-detail-shared-budget"),
                tuple(call[0] for call in transport.continuation_calls),
            )
            self.assertIn(
                "画布比例固定为 3:4",
                transport.continuation_calls[1][1],
            )
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
