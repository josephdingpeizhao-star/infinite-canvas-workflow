from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import codex_dev_executor as executor_module  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    FinalPromptLiteralViolation,
    UserConfirmedRequirements,
    assemble_detail_variable_config_chunks,
    build_detail_variable_config_chunk_prompt,
    build_set_variable_config_prompt,
    build_variable_config_prompt,
    parse_detail_variable_config_chunk,
    parse_set_variable_config_response,
    parse_user_confirmed_requirements,
    parse_variable_config_response,
)
from codex_dev_executor import (  # noqa: E402
    DETAIL_CHUNK_MAX_CONCURRENCY,
    FINAL_PROMPT_TURN_TIMEOUT_SECONDS,
    CanvasAgentCodexTransport,
    CodexDevExecutor,
    CodexTurnResult,
)
from content_correction import (  # noqa: E402
    ContentPredicateViolation,
)
from executor_contract import ExecutorContext, ExecutorExecutionError  # noqa: E402
from image_count_contract import (  # noqa: E402
    DIMENSION_MODULE,
    ImageCountContractError,
    detail_handheld_chunk_quotas,
    detail_module_groups,
    pair_config_ids,
)
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    VALID_STYLE_MASTER,
    valid_detail_variable_response,
)
from tests.test_st03b_set_variable_config import (  # noqa: E402
    PRODUCT_ID as SET_PRODUCT_ID,
    set_requirements,
    valid_component_identity,
    valid_set_identity,
    valid_set_layout_inventory,
    valid_set_variable_response,
    valid_style_master as valid_set_style_master,
)


_CHUNK_INDEX_PATTERN = re.compile(r"本轮只返回第 (\d+)/(\d+) 段")


def _single_requirements(
    *,
    detail_count: int = 8,
    handheld_detail: int = 1,
) -> UserConfirmedRequirements:
    return UserConfirmedRequirements(
        product_type="家居盛水水壶",
        height_cm=25,
        handheld_main=2,
        handheld_detail=handheld_detail,
        allow_clear_water=True,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        main_image_count=6,
        detail_image_count=detail_count,
        category="杯类",
    )


def _single_angle_inventory() -> dict[str, object]:
    return {
        "product_id": "p2c-single",
        "artifact_type": "angle_inventory",
        "image_assets": [
            {"asset_id": asset_id, "file_path": f"{asset_id}.jpg"}
            for asset_id in ("img_001", "img_006", "img_007")
        ],
        "angle_slots": [
            {
                "source_asset_id": "img_001",
                "angle_slot": "A",
                "admission_result": "合格，可进入对应槽位",
                "camera_angle": "正面",
            },
            {
                "source_asset_id": "img_006",
                "angle_slot": "B",
                "admission_result": "合格，可进入对应槽位",
                "camera_angle": "斜侧面",
            },
            {
                "source_asset_id": "img_007",
                "angle_slot": "C",
                "admission_result": "勉强可用，但建议重拍",
                "camera_angle": "侧面",
            },
        ],
        "missing_angle_slots": ["D"],
        "retake_recommendations": [],
        "notes": "D 不补拍",
    }


def _set_handheld(config: dict[str, object], *, enabled: bool, is_set: bool) -> None:
    overrides = config["per_image_overrides"]
    if enabled:
        if is_set:
            overrides["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：静态握持。"
                "持握套装中某一主体单件，其余单件作为静物陈列。"
            )
            overrides["动态手持样式参考图调用"] = "无，仅动态拿起场景可调用"
        else:
            overrides["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：动态拿起。"
                "单手自然握住把手，轻微拿起展示比例，不倾倒"
            )
            overrides["动态手持样式参考图调用"] = "未提供，不调用"
    else:
        overrides["手持交互声明"] = "本张图不启用手持场景"
        overrides["动态手持样式参考图调用"] = "无"


def _single_response(*, enabled_ids: tuple[str, ...]) -> dict[str, object]:
    response = copy.deepcopy(valid_detail_variable_response())
    for config in response["configs"]:
        _set_handheld(
            config,
            enabled=config["config_id"] in enabled_ids,
            is_set=False,
        )
    return response


def _detail_chunks_v2(
    response: dict[str, object],
    *,
    requirements: UserConfirmedRequirements,
    is_set: bool,
) -> list[dict[str, object]]:
    configs = response["configs"]
    batches = pair_config_ids("detail", len(configs))
    quotas = detail_handheld_chunk_quotas(
        len(configs),
        requirements.handheld_detail,
    )
    chunks: list[dict[str, object]] = []
    offset = 0
    for chunk_index, (batch, quota) in enumerate(zip(batches, quotas, strict=True), start=1):
        chunk_configs = copy.deepcopy(configs[offset : offset + len(batch)])
        offset += len(batch)
        enabled_ids = [
            str(config["config_id"])
            for config in chunk_configs
            if "本张图不启用手持场景"
            not in config["per_image_overrides"]["手持交互声明"]
        ]
        summary: dict[str, object] = {
            "本段手持配额": quota,
            "本段实际启用数量": len(enabled_ids),
            "本段启用手持配置": enabled_ids,
        }
        if is_set:
            summary["本段手持启用说明"] = (
                f"已按配额启用，实际启用 {len(enabled_ids)} 项。"
                if len(enabled_ids) == quota
                else f"实际启用 {len(enabled_ids)} 项，原因：档案置信度不足。"
            )
        chunk: dict[str, object] = {
            "chunk_index": chunk_index,
            "chunk_count": len(batches),
            "configs": chunk_configs,
            "handheld_chunk_summary": summary,
        }
        if chunk_index == 1:
            chunk["common_constraints"] = copy.deepcopy(response["common_constraints"])
            chunk["notes"] = response["notes"]
        chunks.append(chunk)
    return chunks


def _parse_chunk_index(prompt: str) -> int:
    match = _CHUNK_INDEX_PATTERN.search(prompt)
    if match is None:
        raise AssertionError("detail prompt did not expose its chunk identity")
    return int(match.group(1))


def _synthetic_chunk(chunk_index: int, chunk_count: int) -> dict[str, object]:
    return {
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "configs": [],
    }


def _run_detail_executor(
    *,
    requirements: UserConfirmedRequirements,
    transport: object,
    parse_chunk: object,
):
    context = ExecutorContext(manifest={"batch_type": "single"})
    executor = CodexDevExecutor(context, transport=transport, repository_root=ROOT)
    with tempfile.TemporaryDirectory() as temporary:
        output_path = Path(temporary) / "detail_variable_configs.json"
        with (
            mock.patch.object(executor, "_validate_single_product_batch"),
            mock.patch.object(
                executor_module,
                "artifact_file_under_root",
                return_value=output_path,
            ),
            mock.patch.object(
                executor_module,
                "parse_user_confirmed_requirements",
                return_value=requirements,
            ),
            mock.patch.object(
                executor_module,
                "load_typed_artifact",
                side_effect=lambda *_args, **_kwargs: ({}, Path("upstream.json")),
            ),
            mock.patch.object(
                executor_module,
                "build_variable_config_prompt",
                return_value="P2C_COMPLETE_BASE_PROMPT",
            ),
            mock.patch.object(
                executor_module,
                "parse_detail_variable_config_chunk",
                new=parse_chunk,
            ),
            mock.patch.object(
                executor_module,
                "assemble_detail_variable_config_chunks",
                side_effect=lambda chunks, **_kwargs: {"chunks": chunks},
            ),
            mock.patch.object(
                executor_module,
                "parse_variable_config_response",
                return_value={"artifact_type": "detail_variable_config"},
            ),
            mock.patch.object(executor_module, "write_json_exclusive"),
        ):
            return executor._execute_detail_variable_config("p2c-product")


class _OverlapTransport:
    def __init__(self, *, gate_size: int, reverse_completion: bool = False) -> None:
        self.gate_size = gate_size
        self.reverse_completion = reverse_completion
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.completion_events = {
            chunk_index: threading.Event()
            for chunk_index in range(1, gate_size + 1)
        }
        self.active = 0
        self.peak_active = 0
        self.started: dict[int, float] = {}
        self.finished: dict[int, float] = {}
        self.completion_order: list[int] = []
        self.timeouts: dict[int, float] = {}

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        chunk_index = _parse_chunk_index(prompt)
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.started[chunk_index] = time.perf_counter()
            self.timeouts[chunk_index] = turn_timeout
            if self.active >= self.gate_size:
                self.release.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("parallel detail workers did not overlap")
        if self.reverse_completion and chunk_index <= self.gate_size:
            if chunk_index < self.gate_size and not self.completion_events[
                chunk_index + 1
            ].wait(timeout=2.0):
                raise AssertionError("reverse completion chain did not advance")
        else:
            time.sleep(0.02)
        with self.lock:
            self.finished[chunk_index] = time.perf_counter()
            self.completion_order.append(chunk_index)
            self.active -= 1
        if self.reverse_completion and chunk_index <= self.gate_size:
            self.completion_events[chunk_index].set()
        return CodexTurnResult(
            text=f"ok-{chunk_index}",
            thread_id=f"thread-{chunk_index}",
        )

    def continue_turn(self, *_args, **_kwargs) -> CodexTurnResult:
        raise AssertionError("valid overlap responses must not continue")


class _RoutedSequenceTransport:
    def __init__(self, plans: dict[int, list[CodexTurnResult]]) -> None:
        self.plans = {chunk_index: list(values) for chunk_index, values in plans.items()}
        self.lock = threading.Lock()
        self.run_counts: dict[int, int] = {}
        self.continue_counts: dict[int, int] = {}

    def _take(self, chunk_index: int) -> CodexTurnResult:
        with self.lock:
            values = self.plans[chunk_index]
            if not values:
                raise AssertionError(f"missing response for chunk {chunk_index}")
            return values.pop(0)

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        if turn_timeout != 600.0:
            raise AssertionError("detail_vc must keep the default timeout")
        chunk_index = _parse_chunk_index(prompt)
        with self.lock:
            self.run_counts[chunk_index] = self.run_counts.get(chunk_index, 0) + 1
        return self._take(chunk_index)

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        if turn_timeout != 600.0:
            raise AssertionError("detail_vc corrections must keep the default timeout")
        chunk_index = _parse_chunk_index(prompt)
        if thread_id != f"thread-{chunk_index}":
            raise AssertionError("continuation was routed to another chunk")
        with self.lock:
            self.continue_counts[chunk_index] = (
                self.continue_counts.get(chunk_index, 0) + 1
            )
        return self._take(chunk_index)


class _TimeoutRecordingTransport:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.run_calls: list[tuple[str, float]] = []
        self.continue_calls: list[tuple[str, float]] = []

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        with self.lock:
            self.run_calls.append((prompt, turn_timeout))
        return CodexTurnResult(text=prompt, thread_id=f"thread-{prompt}")

    def continue_turn(
        self,
        thread_id: str,
        _prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        with self.lock:
            self.continue_calls.append((thread_id, turn_timeout))
        return CodexTurnResult(text="repaired", thread_id=thread_id)


class P2cDetailHandheldQuotaTests(unittest.TestCase):
    def test_representative_counts_have_exact_round_robin_vectors(self) -> None:
        expected = {
            (2, 1): (1,),
            (3, 2): (1, 1),
            (8, 6): (2, 2, 1, 1),
            (13, 9): (2, 2, 1, 1, 1, 1, 1),
            (30, 17): (2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1),
        }
        for (detail_count, target), expected_quotas in expected.items():
            with self.subTest(detail_count=detail_count, target=target):
                first = detail_handheld_chunk_quotas(detail_count, target)
                second = detail_handheld_chunk_quotas(detail_count, target)
                self.assertEqual(expected_quotas, first)
                self.assertEqual(first, second)
                self.assertEqual(target, sum(first))
                groups = detail_module_groups(detail_count)
                capacities = tuple(
                    sum(
                        DIMENSION_MODULE not in modules
                        for modules in groups[index : index + 2]
                    )
                    for index in range(0, len(groups), 2)
                )
                self.assertTrue(
                    all(quota <= capacity for quota, capacity in zip(first, capacities, strict=True))
                )

    def test_module05_slot_contributes_zero_to_its_chunk_capacity(self) -> None:
        for detail_count in (2, 3, 8, 13, 30):
            groups = detail_module_groups(detail_count)
            capacities = tuple(
                sum(
                    DIMENSION_MODULE not in modules
                    for modules in groups[index : index + 2]
                )
                for index in range(0, len(groups), 2)
            )
            quotas = detail_handheld_chunk_quotas(detail_count, sum(capacities))
            with self.subTest(detail_count=detail_count):
                self.assertEqual(capacities, quotas)
                self.assertEqual(detail_count - 1, sum(capacities))

        self.assertEqual((1,), detail_handheld_chunk_quotas(2, 1))
        with self.assertRaises(ImageCountContractError):
            detail_handheld_chunk_quotas(2, 2)

    def test_target_above_total_eligible_capacity_fails_closed(self) -> None:
        for detail_count in (2, 3, 8, 13, 30):
            with self.subTest(detail_count=detail_count):
                with self.assertRaises(ImageCountContractError):
                    detail_handheld_chunk_quotas(detail_count, detail_count)


class P2cDetailParallelExecutorTests(unittest.TestCase):
    @staticmethod
    def _successful_parser(
        _text: str,
        chunk_index: int,
        *,
        requirements: UserConfirmedRequirements,
        **_kwargs,
    ) -> dict[str, object]:
        return _synthetic_chunk(
            chunk_index,
            len(pair_config_ids("detail", requirements.detail_image_count or 8)),
        )

    def test_reverse_completion_is_reassembled_by_chunk_and_thread_identity(self) -> None:
        transport = _OverlapTransport(gate_size=4, reverse_completion=True)

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            self.assertEqual(f"ok-{chunk_index}", text)
            return _synthetic_chunk(
                chunk_index,
                len(pair_config_ids("detail", requirements.detail_image_count or 8)),
            )

        result = _run_detail_executor(
            requirements=_single_requirements(detail_count=8, handheld_detail=0),
            transport=transport,
            parse_chunk=parse_chunk,
        )

        self.assertEqual({1, 2, 3, 4}, set(transport.started))
        self.assertLess(
            max(transport.started[chunk_index] for chunk_index in (1, 2, 3, 4)),
            min(transport.finished[chunk_index] for chunk_index in (1, 2, 3, 4)),
        )
        self.assertEqual([4, 3, 2, 1], transport.completion_order)
        self.assertEqual(
            ("thread-1", "thread-2", "thread-3", "thread-4"),
            result.metadata["thread_ids"],
        )
        self.assertEqual({600.0}, set(transport.timeouts.values()))

    def test_seven_chunks_never_exceed_four_in_flight(self) -> None:
        transport = _OverlapTransport(gate_size=4)
        _run_detail_executor(
            requirements=_single_requirements(detail_count=13, handheld_detail=0),
            transport=transport,
            parse_chunk=self._successful_parser,
        )

        self.assertEqual(set(range(1, 8)), set(transport.started))
        self.assertEqual(4, transport.peak_active)
        self.assertLessEqual(transport.peak_active, DETAIL_CHUNK_MAX_CONCURRENCY)

    def test_continuation_thread_drift_uses_the_exact_detail_error(self) -> None:
        transport = _RoutedSequenceTransport(
            {
                1: [CodexTurnResult(text="ok-1", thread_id="thread-1")],
                2: [
                    CodexTurnResult(text="bad-2", thread_id="thread-2"),
                    CodexTurnResult(text="ok-2", thread_id="thread-1"),
                ],
            }
        )

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            if text.startswith("bad"):
                raise executor_module.DetailChunkTransportCorruption("truncated")
            return _synthetic_chunk(
                chunk_index,
                len(pair_config_ids("detail", requirements.detail_image_count or 4)),
            )

        with self.assertRaises(ExecutorExecutionError) as caught:
            _run_detail_executor(
                requirements=_single_requirements(detail_count=4, handheld_detail=0),
                transport=transport,
                parse_chunk=parse_chunk,
            )
        self.assertEqual(
            "codex-dev 收到无效的详情图变量配置线程返回",
            str(caught.exception),
        )

    def test_two_recoveries_in_chunk_one_do_not_consume_chunk_two_budget(self) -> None:
        transport = _RoutedSequenceTransport(
            {
                1: [
                    CodexTurnResult(text="bad-1a", thread_id="thread-1"),
                    CodexTurnResult(text="bad-1b", thread_id="thread-1"),
                    CodexTurnResult(text="ok-1", thread_id="thread-1"),
                ],
                2: [
                    CodexTurnResult(text="bad-2a", thread_id="thread-2"),
                    CodexTurnResult(text="ok-2", thread_id="thread-2"),
                ],
            }
        )

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            if text.startswith("bad"):
                raise executor_module.DetailChunkTransportCorruption("truncated")
            return _synthetic_chunk(
                chunk_index,
                len(pair_config_ids("detail", requirements.detail_image_count or 4)),
            )

        result = _run_detail_executor(
            requirements=_single_requirements(detail_count=4, handheld_detail=0),
            transport=transport,
            parse_chunk=parse_chunk,
        )

        self.assertEqual({1: 1, 2: 1}, transport.run_counts)
        self.assertEqual({1: 2, 2: 1}, transport.continue_counts)
        self.assertEqual(3, result.metadata["recovery_attempts"])

    def test_all_chunks_finish_before_lowest_failed_chunk_is_reported(self) -> None:
        transport = _RoutedSequenceTransport(
            {
                chunk_index: [
                    CodexTurnResult(
                        text=f"ok-{chunk_index}",
                        thread_id=f"thread-{chunk_index}",
                    )
                ]
                for chunk_index in (1, 2, 3, 4)
            }
        )
        attempted: list[int] = []
        succeeded: list[int] = []
        lock = threading.Lock()

        def parse_chunk(
            _text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            with lock:
                attempted.append(chunk_index)
            if chunk_index == 2:
                raise ExecutorExecutionError("chunk-2-failure")
            if chunk_index == 3:
                time.sleep(0.02)
            if chunk_index == 4:
                raise ExecutorExecutionError("chunk-4-failure")
            result = _synthetic_chunk(
                chunk_index,
                len(pair_config_ids("detail", requirements.detail_image_count or 8)),
            )
            with lock:
                succeeded.append(chunk_index)
            return result

        with self.assertRaises(ExecutorExecutionError) as caught:
            _run_detail_executor(
                requirements=_single_requirements(detail_count=8, handheld_detail=0),
                transport=transport,
                parse_chunk=parse_chunk,
            )

        self.assertEqual("chunk-2-failure", str(caught.exception))
        self.assertEqual({1, 2, 3, 4}, set(attempted))
        self.assertIn(3, succeeded)


class P2cDetailChunkSummaryTests(unittest.TestCase):
    def _parse_single(
        self,
        chunk: dict[str, object],
        *,
        requirements: UserConfirmedRequirements,
    ) -> dict[str, object]:
        return parse_detail_variable_config_chunk(
            json.dumps(chunk, ensure_ascii=False),
            int(chunk["chunk_index"]),
            requirements=requirements,
            angle_inventory=_single_angle_inventory(),
        )

    def _parse_set(
        self,
        chunk: dict[str, object],
        *,
        requirements: UserConfirmedRequirements,
    ) -> dict[str, object]:
        return parse_detail_variable_config_chunk(
            json.dumps(chunk, ensure_ascii=False),
            int(chunk["chunk_index"]),
            requirements=requirements,
            angle_inventory=valid_set_layout_inventory(),
            set_identity=valid_set_identity(),
            component_identities=(
                valid_component_identity(1),
                valid_component_identity(2),
            ),
            set_angle_layout_inventory=valid_set_layout_inventory(),
        )

    def test_single_chunk_accepts_exact_quota_and_exact_summary(self) -> None:
        requirements = _single_requirements()
        chunks = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )

        parsed = [
            self._parse_single(chunk, requirements=requirements)
            for chunk in chunks
        ]

        self.assertEqual([1, 0, 0, 0], [
            chunk["handheld_chunk_summary"]["本段实际启用数量"]
            for chunk in parsed
        ])

    def test_single_chunk_rejects_more_than_its_exact_quota(self) -> None:
        requirements = _single_requirements()
        chunk = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01", "detail_02")),
            requirements=requirements,
            is_set=False,
        )[0]

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_single(chunk, requirements=requirements)
        self.assertEqual("handheld_count", caught.exception.code)

    def test_single_chunk_rejects_fewer_than_its_exact_quota(self) -> None:
        requirements = _single_requirements()
        chunk = _detail_chunks_v2(
            _single_response(enabled_ids=()),
            requirements=requirements,
            is_set=False,
        )[0]

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_single(chunk, requirements=requirements)
        self.assertEqual("handheld_count", caught.exception.code)

    def test_single_chunk_rejects_tampered_actual_count_in_summary(self) -> None:
        requirements = _single_requirements()
        chunk = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )[0]
        chunk["handheld_chunk_summary"]["本段实际启用数量"] = 0

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_single(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("整数 1", caught.exception.details.expected)

    def test_single_chunk_rejects_tampered_enabled_list_in_summary(self) -> None:
        requirements = _single_requirements()
        chunk = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )[0]
        chunk["handheld_chunk_summary"]["本段启用手持配置"] = []

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_single(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("detail_01", caught.exception.details.expected)

    def test_single_chunk_rejects_tampered_quota_in_summary(self) -> None:
        requirements = _single_requirements()
        chunk = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )[0]
        chunk["handheld_chunk_summary"]["本段手持配额"] = 0

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_single(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("整数 1", caught.exception.details.expected)

    def test_set_chunk_accepts_equal_and_below_quota(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        cases = {
            "equal": ("detail_01",),
            "below": (),
        }
        for label, enabled_ids in cases.items():
            with self.subTest(label=label):
                response = valid_set_variable_response(
                    "detail",
                    count=7,
                    handheld_target=1,
                    enabled_ids=enabled_ids,
                )
                chunk = _detail_chunks_v2(
                    response,
                    requirements=requirements,
                    is_set=True,
                )[0]
                parsed = self._parse_set(chunk, requirements=requirements)
                self.assertEqual(
                    len(enabled_ids),
                    parsed["handheld_chunk_summary"]["本段实际启用数量"],
                )

    def test_set_chunk_rejects_enabled_count_above_quota(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        response = valid_set_variable_response(
            "detail",
            count=7,
            handheld_target=1,
            enabled_ids=("detail_01", "detail_02"),
        )
        chunk = _detail_chunks_v2(
            response,
            requirements=requirements,
            is_set=True,
        )[0]

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_set(chunk, requirements=requirements)
        self.assertEqual("handheld_count", caught.exception.code)

    def test_set_equal_quota_explanation_requires_literal_anchor(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        response = valid_set_variable_response(
            "detail",
            count=7,
            handheld_target=1,
            enabled_ids=("detail_01",),
        )
        chunk = _detail_chunks_v2(
            response,
            requirements=requirements,
            is_set=True,
        )[0]
        chunk["handheld_chunk_summary"]["本段手持启用说明"] = "实际启用 1 项。"

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_set(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("已按配额启用", caught.exception.details.expected)

    def test_set_below_quota_explanation_requires_reason_literal(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        response = valid_set_variable_response(
            "detail",
            count=7,
            handheld_target=1,
            enabled_ids=(),
        )
        chunk = _detail_chunks_v2(
            response,
            requirements=requirements,
            is_set=True,
        )[0]
        chunk["handheld_chunk_summary"]["本段手持启用说明"] = "实际启用 0 项。"

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_set(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("原因", caught.exception.details.expected)

    def test_set_below_quota_explanation_requires_actual_number(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        response = valid_set_variable_response(
            "detail",
            count=7,
            handheld_target=1,
            enabled_ids=(),
        )
        chunk = _detail_chunks_v2(
            response,
            requirements=requirements,
            is_set=True,
        )[0]
        chunk["handheld_chunk_summary"]["本段手持启用说明"] = "原因：档案置信度不足。"

        with self.assertRaises(ContentPredicateViolation) as caught:
            self._parse_set(chunk, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)
        self.assertIn("数字 0", caught.exception.details.expected)


class P2cDetailChunkAggregationTests(unittest.TestCase):
    def test_single_aggregate_summary_passes_unchanged_full_response_parser(self) -> None:
        requirements = _single_requirements()
        raw_chunks = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )
        parsed_chunks = [
            parse_detail_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False),
                chunk_index,
                requirements=requirements,
                angle_inventory=_single_angle_inventory(),
            )
            for chunk_index, chunk in enumerate(raw_chunks, start=1)
        ]
        assembled = assemble_detail_variable_config_chunks(
            parsed_chunks,
            requirements=requirements,
            is_set=False,
        )

        self.assertEqual(
            {
                "用户要求详情图手持数量": 1,
                "实际启用手持数量": 1,
                "未启用手持数量": 7,
                "启用手持配置": ["detail_01"],
                "是否完全满足用户数量": "是",
            },
            assembled["handheld_count_summary"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            style = copy.deepcopy(VALID_STYLE_MASTER)
            style["product_id"] = "p2c-single"
            style_path = root / "style_master.json"
            style_path.write_text(
                json.dumps(style, ensure_ascii=False),
                encoding="utf-8",
            )
            artifact = parse_variable_config_response(
                json.dumps(assembled, ensure_ascii=False),
                mode="detail",
                product_id="p2c-single",
                requirements=requirements,
                angle_inventory=_single_angle_inventory(),
                upstream_paths={
                    "product_identity_archive": root / "identity.json",
                    "style_master": style_path,
                    "angle_inventory": root / "angle.json",
                    "main_variable_configs": root / "main.json",
                },
            )

        self.assertEqual("detail_variable_config", artifact["artifact_type"])
        self.assertEqual(8, artifact["config_count"])

    def test_set_shortfall_summary_passes_unchanged_full_response_parser(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        response = valid_set_variable_response(
            "detail",
            count=7,
            handheld_target=1,
            enabled_ids=(),
        )
        raw_chunks = _detail_chunks_v2(
            response,
            requirements=requirements,
            is_set=True,
        )
        parsed_chunks = [
            parse_detail_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False),
                chunk_index,
                requirements=requirements,
                angle_inventory=valid_set_layout_inventory(),
                set_identity=valid_set_identity(),
                component_identities=(
                    valid_component_identity(1),
                    valid_component_identity(2),
                ),
                set_angle_layout_inventory=valid_set_layout_inventory(),
            )
            for chunk_index, chunk in enumerate(raw_chunks, start=1)
        ]
        assembled = assemble_detail_variable_config_chunks(
            parsed_chunks,
            requirements=requirements,
            is_set=True,
        )

        summary = assembled["handheld_count_summary"]
        self.assertEqual(0, summary["实际启用手持数量"])
        self.assertEqual("否", summary["是否完全满足用户数量"])
        self.assertEqual(
            "实际启用 0 项，原因："
            "实际启用 0 项，原因：档案置信度不足。；"
            "已按配额启用，实际启用 0 项。；"
            "已按配额启用，实际启用 0 项。；"
            "已按配额启用，实际启用 0 项。",
            summary["手持启用数量说明"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            style_path = root / "style_master.json"
            style_path.write_text(
                json.dumps(valid_set_style_master(), ensure_ascii=False),
                encoding="utf-8",
            )
            artifact = parse_set_variable_config_response(
                json.dumps(assembled, ensure_ascii=False),
                mode="detail",
                product_id=SET_PRODUCT_ID,
                requirements=requirements,
                set_identity=valid_set_identity(),
                component_identities=(
                    valid_component_identity(1),
                    valid_component_identity(2),
                ),
                set_angle_layout_inventory=valid_set_layout_inventory(),
                upstream_paths={
                    "set_product_identity": root / "set_identity.json",
                    "component_identity_archive_01": root / "component_1.json",
                    "component_identity_archive_02": root / "component_2.json",
                    "style_master": style_path,
                    "set_angle_layout_inventory": root / "layout.json",
                    "main_variable_configs": root / "main.json",
                },
            )

        self.assertEqual("detail_variable_config", artifact["artifact_type"])
        self.assertEqual(7, artifact["config_count"])

    def test_aggregate_rejects_global_handheld_count_drift(self) -> None:
        requirements = _single_requirements()
        raw_chunks = _detail_chunks_v2(
            _single_response(enabled_ids=("detail_01",)),
            requirements=requirements,
            is_set=False,
        )
        parsed_chunks = [
            parse_detail_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False),
                chunk_index,
                requirements=requirements,
                angle_inventory=_single_angle_inventory(),
            )
            for chunk_index, chunk in enumerate(raw_chunks, start=1)
        ]
        tampered = copy.deepcopy(parsed_chunks)
        _set_handheld(tampered[0]["configs"][0], enabled=False, is_set=False)

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "codex-dev 收到的详情图变量配置手持数量异常",
        ):
            assemble_detail_variable_config_chunks(
                tampered,
                requirements=requirements,
                is_set=False,
            )


class P2cRiderTimeoutTests(unittest.TestCase):
    def test_literal_constants_keep_detail_capacity_and_default_timeout_aligned(self) -> None:
        self.assertEqual(4, DETAIL_CHUNK_MAX_CONCURRENCY)
        self.assertEqual(1200.0, FINAL_PROMPT_TURN_TIMEOUT_SECONDS)
        self.assertEqual(600.0, CanvasAgentCodexTransport(config={}).turn_timeout)

    def test_only_final_initial_and_correction_turns_receive_1200_seconds(self) -> None:
        transport = _TimeoutRecordingTransport()
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )

        main_results, detail_results = executor._run_final_prompt_chunks(
            main_prompts=("main-final-1", "main-final-2"),
            main_parse_turn=lambda _index, turn: executor_module._FinalPromptChainResult(
                batch={},
                turn=turn,
                correction_attempts=0,
            ),
            detail_prompts=("detail-final-1",),
            detail_parse_turn=lambda _index, turn: executor_module._FinalPromptChainResult(
                batch={},
                turn=turn,
                correction_attempts=0,
            ),
        )
        other_result = executor._run_transport("main-vc-other", ())
        parse_attempts = 0

        def parse_response(_text: str) -> dict[str, dict[str, str]]:
            nonlocal parse_attempts
            parse_attempts += 1
            if parse_attempts == 1:
                raise FinalPromptLiteralViolation(
                    mode="main",
                    safe_reason="未保留画布比例",
                )
            return {}

        _batch, corrected_turn, correction_attempts = (
            executor._parse_final_prompt_with_bounded_correction(
                CodexTurnResult(text="initial", thread_id="thread-correction"),
                mode="main",
                product_id="p2c-product",
                requirements=_single_requirements(),
                angle_inventory={},
                variable_config={},
                style_master_text="style",
                correction_attempts=0,
                parse_response=parse_response,
                repair_prompt_builder=lambda: "main-final-repair",
            )
        )

        final_run_timeouts = {
            prompt: timeout
            for prompt, timeout in transport.run_calls
            if prompt in {"main-final-1", "main-final-2", "detail-final-1"}
        }
        self.assertEqual(
            {
                "main-final-1": 1200.0,
                "main-final-2": 1200.0,
                "detail-final-1": 1200.0,
            },
            final_run_timeouts,
        )
        self.assertIn(("main-vc-other", 600.0), transport.run_calls)
        self.assertEqual([("thread-correction", 1200.0)], transport.continue_calls)
        self.assertEqual(
            ("thread-main-final-1", "thread-main-final-2"),
            tuple(result.turn.thread_id for result in main_results),
        )
        self.assertEqual(
            ("thread-detail-final-1",),
            tuple(result.turn.thread_id for result in detail_results),
        )
        self.assertEqual("thread-main-vc-other", other_result.thread_id)
        self.assertEqual("thread-correction", corrected_turn.thread_id)
        self.assertEqual(1, correction_attempts)


class P2cRuntimePromptSourceAnchorTests(CodexDevFixture):
    def test_single_runtime_prompts_embed_full_base_quota_and_v2_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, _detail_output, main_output = self.make_detail_fixture(root)
            requirements = parse_user_confirmed_requirements(context.manifest, root)
            artifacts_root = root / "workspace" / "artifacts"
            identity = json.loads(
                (artifacts_root / "identity" / "product_identity_archive.json").read_text(
                    encoding="utf-8"
                )
            )
            style_master = json.loads(
                (artifacts_root / "style_master" / "style_master.json").read_text(
                    encoding="utf-8"
                )
            )
            angle_inventory = json.loads(
                (artifacts_root / "angle_inventory" / "angle_inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            main_variable_config = json.loads(main_output.read_text(encoding="utf-8"))
            base_prompt = build_variable_config_prompt(
                mode="detail",
                product_id="p1",
                repository_root=root,
                identity=identity,
                style_master=style_master,
                angle_inventory=angle_inventory,
                requirements=requirements,
                main_variable_config=main_variable_config,
            )
            chunk_one = build_detail_variable_config_chunk_prompt(
                base_prompt,
                1,
                requirements=requirements,
                is_set=False,
            )
            chunk_two = build_detail_variable_config_chunk_prompt(
                base_prompt,
                2,
                requirements=requirements,
                is_set=False,
            )
            chunk_one_suffix = chunk_one[len(base_prompt) :]
            chunk_two_suffix = chunk_two[len(base_prompt) :]

        self.assertIn("用户要求详情图手持总数为 1 项", base_prompt)
        self.assertTrue(chunk_one.startswith(base_prompt))
        self.assertTrue(chunk_two.startswith(base_prompt))
        self.assertIn("本段必须恰好启用 1 项手持", chunk_one_suffix)
        self.assertIn("本段所有图位一律不启用手持场景", chunk_two_suffix)
        self.assertIn(
            '顶层键必须恰好为：["chunk_index", "chunk_count", "configs", '
            '"common_constraints", "notes", "handheld_chunk_summary"]',
            chunk_one_suffix,
        )
        self.assertIn(
            '顶层键必须恰好为：["chunk_index", "chunk_count", "configs", '
            '"handheld_chunk_summary"]',
            chunk_two_suffix,
        )
        summary_instruction = (
            "handheld_chunk_summary 必须是 JSON 对象，且必须恰好包含这些字段："
            "本段手持配额、本段实际启用数量、本段启用手持配置。"
        )
        for suffix in (chunk_one_suffix, chunk_two_suffix):
            self.assertIn(summary_instruction, suffix)
            self.assertIn("任一分段均不得返回 handheld_count_summary", suffix)

    def test_set_runtime_prompts_embed_full_base_upper_bound_and_explanation_key(self) -> None:
        requirements = set_requirements(detail_count=7, handheld_detail=1)
        base_prompt = build_set_variable_config_prompt(
            mode="detail",
            product_id=SET_PRODUCT_ID,
            repository_root=ROOT,
            set_identity=valid_set_identity(),
            component_identities=(
                valid_component_identity(1),
                valid_component_identity(2),
            ),
            style_master=valid_set_style_master(),
            set_angle_layout_inventory=valid_set_layout_inventory(),
            requirements=requirements,
            set_skill_text="套装变量配置教学。",
            set_variable_config_supplement="套装变量配置补充教学。",
            set_workflow_supplement="套装工作流补充教学。",
            set_layout_rules="套装编排规则。",
            main_variable_config=valid_set_variable_response(
                "main",
                count=2,
                handheld_target=1,
                enabled_ids=("main_01",),
            ),
        )
        chunk_one = build_detail_variable_config_chunk_prompt(
            base_prompt,
            1,
            requirements=requirements,
            is_set=True,
        )
        chunk_two = build_detail_variable_config_chunk_prompt(
            base_prompt,
            2,
            requirements=requirements,
            is_set=True,
        )
        chunk_one_suffix = chunk_one[len(base_prompt) :]
        chunk_two_suffix = chunk_two[len(base_prompt) :]

        self.assertIn("手持全局目标为 1 项", base_prompt)
        self.assertTrue(chunk_one.startswith(base_prompt))
        self.assertTrue(chunk_two.startswith(base_prompt))
        self.assertIn("本段手持上限 1 项，允许少于", chunk_one_suffix)
        self.assertIn("本段手持上限 0 项，允许少于", chunk_two_suffix)
        self.assertIn(
            '顶层键必须恰好为：["chunk_index", "chunk_count", "configs", '
            '"common_constraints", "notes", "handheld_chunk_summary"]',
            chunk_one_suffix,
        )
        self.assertIn(
            '顶层键必须恰好为：["chunk_index", "chunk_count", "configs", '
            '"handheld_chunk_summary"]',
            chunk_two_suffix,
        )
        summary_instruction = (
            "handheld_chunk_summary 必须是 JSON 对象，且必须恰好包含这些字段："
            "本段手持配额、本段实际启用数量、本段启用手持配置、本段手持启用说明。"
        )
        explanation_instruction = (
            "【本段手持启用说明】在本段实际启用数量等于配额时必须包含"
            "“已按配额启用”；少于配额时必须包含“原因”与本段实际启用数量的数字。"
        )
        for suffix in (chunk_one_suffix, chunk_two_suffix):
            self.assertIn(summary_instruction, suffix)
            self.assertIn(explanation_instruction, suffix)
            self.assertIn("任一分段均不得返回 handheld_count_summary", suffix)


if __name__ == "__main__":
    unittest.main()
