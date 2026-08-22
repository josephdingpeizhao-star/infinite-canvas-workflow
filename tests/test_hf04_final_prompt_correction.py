from __future__ import annotations

import copy
import json
import re
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
from image_count_contract import pair_config_ids  # noqa: E402
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
        responses: dict[tuple[str, int], list[CodexTurnResult | Exception]],
    ) -> None:
        self._responses = {key: list(items) for key, items in responses.items()}
        self._thread_ids: dict[tuple[str, int], str] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, int, str, tuple[CodexAttachment, ...]]] = []
        self.continuation_calls: list[
            tuple[str, int, str, str, tuple[CodexAttachment, ...]]
        ] = []

    @staticmethod
    def _mode_from_prompt(prompt: str, markers: dict[str, str]) -> str:
        matches = [mode for mode, marker in markers.items() if marker in prompt]
        if len(matches) != 1:
            raise AssertionError("final prompt mode could not be identified")
        return matches[0]

    @staticmethod
    def _chunk_index_from_prompt(prompt: str) -> int:
        match = re.search(r"本轮只执行第 (\d+)/\d+ 段", prompt)
        if match is None:
            raise AssertionError("final prompt chunk could not be identified")
        return int(match.group(1))

    def _next_result(self, key: tuple[str, int]) -> CodexTurnResult:
        results = self._responses.get(key)
        if not results:
            raise AssertionError(f"unexpected {key} final prompt transport call")
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @staticmethod
    def _require_final_timeout(turn_timeout: float) -> None:
        if turn_timeout != 1200.0:
            raise AssertionError("final prompt turn must use the 1200-second timeout")

    def run_turn(
        self,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
        *,
        turn_timeout: float,
    ) -> CodexTurnResult:
        self._require_final_timeout(turn_timeout)
        mode = self._mode_from_prompt(prompt, self._INITIAL_MARKERS)
        chunk_index = self._chunk_index_from_prompt(prompt)
        key = (mode, chunk_index)
        with self._lock:
            self.calls.append((mode, chunk_index, prompt, attachments))
            result = self._next_result(key)
            expected_thread_id = self._thread_ids.setdefault(key, result.thread_id)
            if result.thread_id != expected_thread_id:
                raise AssertionError(f"{key} initial response changed thread identity")
            return result

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        attachments: tuple[CodexAttachment, ...],
        *,
        turn_timeout: float,
    ) -> CodexTurnResult:
        self._require_final_timeout(turn_timeout)
        mode = self._mode_from_prompt(prompt, self._REPAIR_MARKERS)
        chunk_index = self._chunk_index_from_prompt(prompt)
        key = (mode, chunk_index)
        with self._lock:
            expected_thread_id = self._thread_ids.get(key)
            if thread_id != expected_thread_id:
                raise AssertionError(f"{key} repair used the wrong thread identity")
            self.continuation_calls.append(
                (mode, chunk_index, thread_id, prompt, attachments)
            )
            result = self._next_result(key)
            if result.thread_id != expected_thread_id:
                raise AssertionError(f"{key} repair response changed thread identity")
            return result

    def remaining(self, mode: str) -> int:
        with self._lock:
            return sum(
                len(items)
                for (response_mode, _chunk_index), items in self._responses.items()
                if response_mode == mode
            )


class FinalPromptCorrectionBudgetTest(CodexDevFixture):
    @staticmethod
    def chunk_response(
        mode: str,
        chunk_index: int,
        *,
        response: dict[str, object] | None = None,
    ) -> dict[str, object]:
        full = copy.deepcopy(response or valid_final_prompt_response(mode))
        prompts = full["prompts"]
        batches = pair_config_ids(mode, len(prompts))
        expected_ids = set(batches[chunk_index - 1])
        return {
            "prompts": [
                prompt
                for prompt in prompts
                if prompt["config_id"] in expected_ids
            ]
        }

    @classmethod
    def valid_chunk_sequences(
        cls,
    ) -> dict[tuple[str, int], list[CodexTurnResult | Exception]]:
        sequences: dict[tuple[str, int], list[CodexTurnResult | Exception]] = {}
        for mode in ("main", "detail"):
            full = valid_final_prompt_response(mode)
            chunk_count = len(pair_config_ids(mode, len(full["prompts"])))
            for chunk_index in range(1, chunk_count + 1):
                sequences[(mode, chunk_index)] = [
                    CodexTurnResult(
                        text=json.dumps(
                            cls.chunk_response(mode, chunk_index, response=full),
                            ensure_ascii=False,
                        ),
                        thread_id=f"thread-{mode}-chunk-{chunk_index}",
                    )
                ]
        return sequences

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
            responses = self.valid_chunk_sequences()
            responses[("main", 1)] = [
                CodexTurnResult(
                    text=json.dumps(
                        self.chunk_response("main", 1, response=invalid),
                        ensure_ascii=False,
                    ),
                    thread_id="thread-main-chunk-1",
                )
                for _ in range(3)
            ]
            transport = _ModeAwareFinalPromptTransport(responses)

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
            self.assertEqual(
                {"main": 3, "detail": 4},
                {
                    mode: sum(call[0] == mode for call in transport.calls)
                    for mode in ("main", "detail")
                },
            )
            main_repairs = [
                call
                for call in transport.continuation_calls
                if call[:2] == ("main", 1)
            ]
            self.assertEqual(2, len(main_repairs))
            self.assertEqual(
                [],
                [call for call in transport.continuation_calls if call[0] == "detail"],
            )
            self.assertEqual(
                ("thread-main-chunk-1", "thread-main-chunk-1"),
                tuple(call[2] for call in main_repairs),
            )
            self.assertTrue(
                all("画布比例固定为 1:1" in call[3] for call in main_repairs)
            )
            self.assertTrue(
                all("高度约 25 厘米" in call[3] for call in main_repairs)
            )
            self.assertEqual(0, transport.remaining("main"))
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_clear_water_business_violation_waits_for_detail_without_literal_correction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            context = self.with_structured_facts(context, allow_clear_water=False)
            transport = _ModeAwareFinalPromptTransport(self.valid_chunk_sequences())

            with self.assertRaisesRegex(ExecutorExecutionError, "场景边界"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(3, sum(call[0] == "main" for call in transport.calls))
            self.assertEqual(4, sum(call[0] == "detail" for call in transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_material_business_violation_waits_for_detail_without_literal_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = valid_final_prompt_response("main")
            invalid_main["prompts"][0]["final_prompt"] += "；杯身为玻璃。"
            responses = self.valid_chunk_sequences()
            responses[("main", 1)] = [
                CodexTurnResult(
                    text=json.dumps(
                        self.chunk_response("main", 1, response=invalid_main),
                        ensure_ascii=False,
                    ),
                    thread_id="thread-main-chunk-1",
                )
            ]
            transport = _ModeAwareFinalPromptTransport(responses)

            with self.assertRaisesRegex(ExecutorExecutionError, "未确认商品事实"):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(3, sum(call[0] == "main" for call in transport.calls))
            self.assertEqual(4, sum(call[0] == "detail" for call in transport.calls))
            self.assertEqual([], transport.continuation_calls)
            self.assertEqual(0, transport.remaining("detail"))
            self.assertFalse(final_dir.exists() and any(final_dir.iterdir()))

    def test_correction_transport_failure_waits_for_detail_without_retry_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, final_dir, _main_path, _detail_path = self.make_final_prompt_fixture(root)
            invalid_main = self.paraphrased_response("main")
            responses = self.valid_chunk_sequences()
            responses[("main", 1)] = [
                CodexTurnResult(
                    text=json.dumps(
                        self.chunk_response("main", 1, response=invalid_main),
                        ensure_ascii=False,
                    ),
                    thread_id="thread-main-chunk-1",
                ),
                RuntimeError("repair transport stopped"),
            ]
            transport = _ModeAwareFinalPromptTransport(responses)

            with self.assertRaisesRegex(
                ExecutorExecutionError,
                "RuntimeError: repair transport stopped",
            ):
                CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

            self.assertEqual(3, sum(call[0] == "main" for call in transport.calls))
            self.assertEqual(4, sum(call[0] == "detail" for call in transport.calls))
            self.assertEqual(1, len(transport.continuation_calls))
            self.assertEqual(
                ("main", 1, "thread-main-chunk-1"),
                transport.continuation_calls[0][:3],
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
                    "main": f"thread-main-chunk-1-{scenario}",
                    "detail": f"thread-detail-chunk-1-{scenario}",
                }

                def chunk_responses(mode: str, correction_count: int) -> list[CodexTurnResult]:
                    return [
                        *(
                            CodexTurnResult(
                                text=json.dumps(
                                    self.chunk_response(
                                        mode,
                                        1,
                                        response=self.paraphrased_response(mode),
                                    ),
                                    ensure_ascii=False,
                                ),
                                thread_id=thread_ids[mode],
                            )
                            for _ in range(correction_count)
                        ),
                        CodexTurnResult(
                            text=json.dumps(
                                self.chunk_response(mode, 1),
                                ensure_ascii=False,
                            ),
                            thread_id=thread_ids[mode],
                        ),
                    ]

                responses = self.valid_chunk_sequences()
                responses[("main", 1)] = chunk_responses("main", main_corrections)
                responses[("detail", 1)] = chunk_responses("detail", detail_corrections)
                transport = _ModeAwareFinalPromptTransport(responses)
                result = CodexDevExecutor(
                    context,
                    transport=transport,
                    repository_root=root,
                ).execute(ExecutionRequest(step="final_prompts"))

                self.assertEqual(3, sum(call[0] == "main" for call in transport.calls))
                self.assertEqual(4, sum(call[0] == "detail" for call in transport.calls))
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
                            call[1] == 1 and call[2] == thread_ids[mode]
                            for call in transport.continuation_calls
                            if call[0] == mode
                        )
                    )
                    self.assertEqual(0, transport.remaining(mode))
                self.assertEqual(3, result.metadata["correction_attempts"])
                self.assertEqual(3, len(result.metadata["main_thread_ids"]))
                self.assertEqual(4, len(result.metadata["detail_thread_ids"]))
                self.assertEqual(thread_ids["main"], result.metadata["main_thread_ids"][0])
                self.assertEqual(thread_ids["detail"], result.metadata["detail_thread_ids"][0])
                self.assertNotIn("main_thread_id", result.metadata)
                self.assertNotIn("detail_thread_id", result.metadata)
                self.assertEqual(
                    "最终提示词已生成（受控纠正 3 次）",
                    result.detail,
                )
                self.assertTrue((final_dir / "final_prompt_index.json").exists())


if __name__ == "__main__":
    unittest.main()
