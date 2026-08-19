from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
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
    valid_final_prompt_response,
)


class _ModeAwareFinalPromptTransport:
    _INITIAL_MARKERS = {
        "main": "编译 main 配置的最终提示词",
        "detail": "编译 detail 配置的最终提示词",
    }
    _REPAIR_MARKERS = {
        "main": "final_prompts 主图批次任务",
        "detail": "final_prompts 详情图批次任务",
    }

    def __init__(
        self,
        responses: dict[str, list[CodexTurnResult | Exception]],
    ) -> None:
        self._responses = {mode: list(items) for mode, items in responses.items()}
        self._thread_ids: dict[str, str] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, tuple[CodexAttachment, ...]]] = []
        self.continuation_calls: list[
            tuple[str, str, str, tuple[CodexAttachment, ...]]
        ] = []

    @staticmethod
    def _mode_from_prompt(prompt: str, markers: dict[str, str]) -> str:
        matches = [mode for mode, marker in markers.items() if marker in prompt]
        if len(matches) != 1:
            raise AssertionError("final prompt mode could not be identified")
        return matches[0]

    def _next_result(self, mode: str) -> CodexTurnResult:
        results = self._responses.get(mode)
        if not results:
            raise AssertionError(f"unexpected {mode} final prompt transport call")
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        mode = self._mode_from_prompt(prompt, self._INITIAL_MARKERS)
        with self._lock:
            self.calls.append((mode, prompt, attachments))
            result = self._next_result(mode)
            expected_thread_id = self._thread_ids.setdefault(mode, result.thread_id)
            if result.thread_id != expected_thread_id:
                raise AssertionError(f"{mode} initial response changed thread identity")
            return result

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
    ) -> CodexTurnResult:
        mode = self._mode_from_prompt(prompt, self._REPAIR_MARKERS)
        with self._lock:
            expected_thread_id = self._thread_ids.get(mode)
            if thread_id != expected_thread_id:
                raise AssertionError(f"{mode} repair used the wrong thread identity")
            self.continuation_calls.append((mode, thread_id, prompt, attachments))
            result = self._next_result(mode)
            if result.thread_id != expected_thread_id:
                raise AssertionError(f"{mode} repair response changed thread identity")
            return result

    def remaining(self, mode: str) -> int:
        with self._lock:
            return len(self._responses.get(mode, ()))


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

    def test_two_corrections_still_invalid_waits_for_detail_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid = self.paraphrased_response("main")
            transport = _ModeAwareFinalPromptTransport(
                {
                    "main": [
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
                    ],
                    "detail": [
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response("detail"),
                                ensure_ascii=False,
                            ),
                            thread_id="thread-detail-budget",
                        )
                    ],
                }
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
            self.assertCountEqual(("main", "detail"), tuple(call[0] for call in transport.calls))
            main_repairs = [call for call in transport.continuation_calls if call[0] == "main"]
            self.assertEqual(2, len(main_repairs))
            self.assertEqual(
                [],
                [call for call in transport.continuation_calls if call[0] == "detail"],
            )
            self.assertEqual(
                ("thread-main-budget", "thread-main-budget"),
                tuple(call[1] for call in main_repairs),
            )
            self.assertTrue(all("画布比例固定为 1:1" in call[2] for call in main_repairs))
            self.assertTrue(all("高度约 25 厘米" in call[2] for call in main_repairs))
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_clear_water_business_violation_waits_for_detail_without_literal_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            context = self.with_structured_facts(context, allow_clear_water=False)
            transport = _ModeAwareFinalPromptTransport(
                {
                    "main": [
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response("main"), ensure_ascii=False
                            ),
                            thread_id="thread-main-clear-water-business-stop",
                        )
                    ],
                    "detail": [
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response("detail"), ensure_ascii=False
                            ),
                            thread_id="thread-detail-clear-water-business-stop",
                        )
                    ],
                }
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "场景边界"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertCountEqual(("main", "detail"), tuple(call[0] for call in transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_material_business_violation_waits_for_detail_without_literal_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = valid_final_prompt_response("main")
            invalid_main["prompts"][0]["final_prompt"] += "；杯身为玻璃。"
            transport = _ModeAwareFinalPromptTransport(
                {
                    "main": [
                        CodexTurnResult(
                            text=json.dumps(invalid_main, ensure_ascii=False),
                            thread_id="thread-main-material-business-stop",
                        )
                    ],
                    "detail": [
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response("detail"), ensure_ascii=False
                            ),
                            thread_id="thread-detail-material-business-stop",
                        )
                    ],
                }
            )

            with self.assertRaisesRegex(ExecutorExecutionError, "未确认商品事实"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertCountEqual(("main", "detail"), tuple(call[0] for call in transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_correction_transport_failure_waits_for_detail_without_retry_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = self.paraphrased_response("main")
            transport = _ModeAwareFinalPromptTransport(
                {
                    "main": [
                        CodexTurnResult(
                            text=json.dumps(invalid_main, ensure_ascii=False),
                            thread_id="thread-main-transport-failure",
                        ),
                        RuntimeError("repair transport stopped"),
                    ],
                    "detail": [
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response("detail"), ensure_ascii=False
                            ),
                            thread_id="thread-detail-transport-failure",
                        )
                    ],
                }
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

            self.assertCountEqual(("main", "detail"), tuple(call[0] for call in transport.calls))
            self.assertEqual(1, len(transport.continuation_calls))
            self.assertEqual(
                ("main", "thread-main-transport-failure"),
                transport.continuation_calls[0][:2],
            )
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_main_and_detail_have_independent_two_correction_budgets(self) -> None:
        scenarios = (
            ("main_uses_two_then_detail_corrects", 2, 1),
            ("detail_uses_two_then_main_corrects", 1, 2),
        )
        for scenario, main_corrections, detail_corrections in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
                thread_ids = {
                    "main": f"thread-main-independent-budget-{scenario}",
                    "detail": f"thread-detail-independent-budget-{scenario}",
                }

                def mode_responses(mode: str, correction_count: int) -> list[CodexTurnResult]:
                    return [
                        *(
                            CodexTurnResult(
                                text=json.dumps(
                                    self.paraphrased_response(mode), ensure_ascii=False
                                ),
                                thread_id=thread_ids[mode],
                            )
                            for _ in range(correction_count)
                        ),
                        CodexTurnResult(
                            text=json.dumps(
                                valid_final_prompt_response(mode), ensure_ascii=False
                            ),
                            thread_id=thread_ids[mode],
                        ),
                    ]

                transport = _ModeAwareFinalPromptTransport(
                    {
                        "main": mode_responses("main", main_corrections),
                        "detail": mode_responses("detail", detail_corrections),
                    }
                )
                result = CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

                self.assertCountEqual(
                    ("main", "detail"), tuple(call[0] for call in transport.calls)
                )
                actual_corrections = {
                    mode: sum(call[0] == mode for call in transport.continuation_calls)
                    for mode in ("main", "detail")
                }
                self.assertEqual(
                    {"main": main_corrections, "detail": detail_corrections},
                    actual_corrections,
                )
                for mode in ("main", "detail"):
                    self.assertTrue(
                        all(
                            call[1] == thread_ids[mode]
                            for call in transport.continuation_calls
                            if call[0] == mode
                        )
                    )
                    self.assertEqual(0, transport.remaining(mode))
                self.assertEqual(3, result.metadata["correction_attempts"])
                self.assertEqual(thread_ids["main"], result.metadata["main_thread_id"])
                self.assertEqual(thread_ids["detail"], result.metadata["detail_thread_id"])
                self.assertEqual(
                    "最终提示词已生成（受控纠正 3 次）",
                    result.detail,
                )
                self.assertTrue((final_dir / "final_prompt_index.json").exists())


if __name__ == "__main__":
    unittest.main()
