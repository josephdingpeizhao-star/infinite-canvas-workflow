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
TESTS = ROOT / "tests"
for path in (BRIDGE, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import codex_dev_downstream as downstream  # noqa: E402
import codex_dev_executor as executor_module  # noqa: E402
from codex_dev_executor import (  # noqa: E402
    CodexDevExecutor,
    CodexTurnResult,
    _FinalPromptChainResult,
)
from codex_dev_downstream import (  # noqa: E402
    UserConfirmedRequirements,
    assemble_final_prompt_chunks,
    build_final_prompt_bundle,
    build_final_prompt_chunk_prompt,
    build_final_prompt_repair_prompt,
    build_set_final_prompt_bundle,
    build_set_final_prompt_repair_prompt,
    final_prompt_chunk_count,
    parse_final_prompt_batch_response,
    parse_set_final_prompt_batch_response,
    stable_json_sha256,
)
from executor_contract import (  # noqa: E402
    ExecutionRequest,
    ExecutorContext,
    ExecutorExecutionError,
)
from image_count_contract import config_ids, pair_config_ids  # noqa: E402
from test_st03b_set_variable_config import (  # noqa: E402
    PRODUCT_ID as SET_PRODUCT_ID,
    SetVariableConfigFixture,
    set_manifest_facts,
    valid_component_identity,
    valid_set_identity,
    valid_set_layout_inventory,
)
from test_st03c_set_final_prompts import (  # noqa: E402
    _component_identity_with_negatives,
    _final_prompt_response,
    _formal_set_variable_config,
)
from test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    valid_final_prompt_response,
)


SINGLE_PRODUCT_ID = "p2d-single"


def _requirements(
    *,
    main_count: int = 3,
    detail_count: int = 3,
    handheld_main: int = 1,
    handheld_detail: int = 1,
) -> UserConfirmedRequirements:
    return UserConfirmedRequirements(
        product_type="杯子",
        height_cm=25,
        handheld_main=handheld_main,
        handheld_detail=handheld_detail,
        allow_clear_water=True,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        main_image_count=main_count,
        detail_image_count=detail_count,
        category="杯类",
    )


def _angle_inventory() -> dict[str, object]:
    return {
        "product_id": SINGLE_PRODUCT_ID,
        "artifact_type": "angle_inventory",
        "image_assets": [{"asset_id": "img_001", "file_path": "img_001.jpg"}],
        "angle_slots": [
            {
                "source_asset_id": "img_001",
                "angle_slot": "A",
                "admission_result": "合格，可进入对应槽位",
            },
            {
                "source_asset_id": "img_999",
                "angle_slot": "D",
                "admission_result": "不适合入库，需重拍",
            },
        ],
        "missing_angle_slots": ["D"],
    }


def _variable_config(
    mode: str,
    count: int,
    *,
    product_id: str = SINGLE_PRODUCT_ID,
    enabled_ids: tuple[str, ...] = (),
    is_set: bool = False,
) -> dict[str, object]:
    common = {"产品类型": "杯子", "已确认高度": "约 25 厘米"}
    configs: list[dict[str, object]] = []
    for config_id in config_ids(mode, count):
        overrides = {
            "绑定角度槽位": (
                "整体机位 A：正面微俯视机位。对应白底图：图1，group.png。"
                if is_set
                else "A 槽位，绑定源图 img_001；本张仅调用这一张白底图。"
            ),
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：静态握持"
                if config_id in enabled_ids
                else "本张图不启用手持场景"
            ),
        }
        if is_set:
            overrides["套装编排槽位"] = (
                "编排槽位一：并列陈列。对应套装合影白底图：图1，group.png。"
            )
        resolved = dict(common)
        resolved.update(overrides)
        configs.append(
            {
                "config_id": config_id,
                "output_type": mode,
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "P2-d 离线夹具",
            }
        )
    return {
        "product_id": product_id,
        "artifact_type": f"{mode}_variable_config",
        "config_count": count,
        "upstream_artifacts": {},
        "common_constraints": common,
        "configs": configs,
        "notes": "P2-d 离线夹具",
    }


def _response(
    mode: str,
    ids: tuple[str, ...],
    *,
    enabled_ids: tuple[str, ...] = (),
    is_set: bool = False,
) -> dict[str, object]:
    ratio = "1:1" if mode == "main" else "3:4"
    prompts: list[dict[str, str]] = []
    for config_id in ids:
        handheld = (
            "启用手持场景。"
            if config_id in enabled_ids
            else "本张图不启用手持场景。"
        )
        binding = "图1，group.png，编排槽位一。" if is_set else "img_001，A 槽位。"
        height = "" if is_set else "产品高度约 25 厘米。"
        prompts.append(
            {
                "config_id": config_id,
                "final_prompt": f"{binding}画布比例固定为 {ratio}。{height}{handheld}",
                "negative_prompt": "禁止虚构未确认卖点",
            }
        )
    return {"prompts": prompts}


def _single_parse(
    response: dict[str, object],
    *,
    mode: str,
    requirements: UserConfirmedRequirements,
    variable_config: dict[str, object],
    expected_config_ids: tuple[str, ...] | None = None,
) -> dict[str, dict[str, str]]:
    return parse_final_prompt_batch_response(
        json.dumps(response, ensure_ascii=False),
        mode=mode,
        product_id=SINGLE_PRODUCT_ID,
        requirements=requirements,
        angle_inventory=_angle_inventory(),
        variable_config=variable_config,
        expected_config_ids=expected_config_ids,
    )


def _set_parse(
    response: dict[str, object],
    *,
    mode: str,
    requirements: UserConfirmedRequirements,
    variable_config: dict[str, object],
    expected_config_ids: tuple[str, ...] | None = None,
) -> dict[str, dict[str, str]]:
    return parse_set_final_prompt_batch_response(
        json.dumps(response, ensure_ascii=False),
        mode=mode,
        product_id=SET_PRODUCT_ID,
        requirements=requirements,
        set_identity=valid_set_identity(),
        component_identities=(valid_component_identity(1), valid_component_identity(2)),
        set_angle_layout_inventory=valid_set_layout_inventory(),
        variable_config=variable_config,
        expected_config_ids=expected_config_ids,
    )


class _SharedPoolProbeTransport:
    def __init__(self, *, wait_for_peak: int = 4) -> None:
        self.wait_for_peak = wait_for_peak
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.active = 0
        self.peak = 0
        self.active_modes: dict[str, int] = {"main": 0, "detail": 0}
        self.saw_both_modes = False
        self.timeouts: list[float | None] = []
        self.completed: list[str] = []

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        mode = "main" if prompt.startswith("main-") else "detail"
        with self.lock:
            self.active += 1
            self.active_modes[mode] += 1
            self.peak = max(self.peak, self.active)
            self.saw_both_modes = self.saw_both_modes or all(self.active_modes.values())
            self.timeouts.append(turn_timeout)
            if self.active >= self.wait_for_peak:
                self.release.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("shared final-prompt pool did not reach expected peak")
        time.sleep(0.003)
        with self.lock:
            self.active -= 1
            self.active_modes[mode] -= 1
            self.completed.append(prompt)
        return CodexTurnResult(text=prompt, thread_id=f"thread-{prompt}")

    def continue_turn(
        self,
        _thread_id: str,
        _prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        raise AssertionError(f"unexpected continuation with timeout {turn_timeout}")


class _CorrectionTransport:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.initial_timeouts: list[float | None] = []
        self.correction_timeouts: list[float | None] = []
        self.continuations: list[tuple[str, str]] = []

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        with self.lock:
            self.initial_timeouts.append(turn_timeout)
        return CodexTurnResult(text="initial", thread_id=f"thread-{prompt}")

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        with self.lock:
            self.correction_timeouts.append(turn_timeout)
            self.continuations.append((thread_id, prompt))
        return CodexTurnResult(text="corrected", thread_id=thread_id)


class _FixtureChunkTransport:
    _IDS_PATTERN = re.compile(
        r"本段唯一允许返回的配置编号为：(\[[^。]+\])。"
    )

    def __init__(self, responses: dict[str, dict[str, object]] | None = None) -> None:
        self.lock = threading.Lock()
        self.timeouts: list[float | None] = []
        self.responses = copy.deepcopy(responses) if responses is not None else None

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        match = self._IDS_PATTERN.search(prompt)
        if match is None:
            raise AssertionError("final-prompt chunk ids were not injected")
        ids = tuple(json.loads(match.group(1)))
        mode = ids[0].split("_", 1)[0]
        full = (
            copy.deepcopy(self.responses[mode])
            if self.responses is not None
            else valid_final_prompt_response(mode)
        )
        by_id = {item["config_id"]: item for item in full["prompts"]}
        response = {"prompts": [by_id[config_id] for config_id in ids]}
        with self.lock:
            self.timeouts.append(turn_timeout)
        return CodexTurnResult(
            text=json.dumps(response, ensure_ascii=False),
            thread_id=f"thread-{mode}-{ids[0]}",
        )

    def continue_turn(
        self,
        _thread_id: str,
        _prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float | None = None,
    ) -> CodexTurnResult:
        raise AssertionError(f"unexpected continuation with timeout {turn_timeout}")


def _assert_assembled_chains_revalidated_as_full_batches(
    test_case: unittest.TestCase,
    parser_spy: mock.Mock,
    *,
    counts: dict[str, int],
) -> None:
    full_calls = [
        call
        for call in parser_spy.call_args_list
        if call.kwargs.get("expected_config_ids") is None
    ]
    chunk_calls = [
        call
        for call in parser_spy.call_args_list
        if call.kwargs.get("expected_config_ids") is not None
    ]
    test_case.assertEqual(
        sum((count + 1) // 2 for count in counts.values()),
        len(chunk_calls),
    )
    test_case.assertEqual(2, len(full_calls))

    for mode, count in counts.items():
        mode_calls = [call for call in full_calls if call.kwargs["mode"] == mode]
        test_case.assertEqual(1, len(mode_calls), f"{mode} 合并响应必须完整终验一次")
        call = mode_calls[0]
        test_case.assertIsNone(call.kwargs.get("expected_config_ids"))
        response = json.loads(call.args[0])
        test_case.assertEqual(
            list(config_ids(mode, count)),
            [item["config_id"] for item in response["prompts"]],
        )


class P2dFinalPromptChunkContractTests(unittest.TestCase):
    def test_chunk_boundaries_and_full_base_prefix_are_stable(self) -> None:
        for count, expected_chunks in ((1, 1), (2, 1), (3, 2), (30, 15)):
            requirements = _requirements(main_count=count)
            base = "BASE\r\n含全批逐编号契约 main_01 main_30"
            self.assertEqual(expected_chunks, final_prompt_chunk_count("main", requirements))
            for chunk_index, expected_ids in enumerate(
                pair_config_ids("main", count), start=1
            ):
                with self.subTest(count=count, chunk_index=chunk_index):
                    prompt = build_final_prompt_chunk_prompt(
                        base,
                        chunk_index,
                        mode="main",
                        requirements=requirements,
                    )
                    self.assertEqual(base.encode("utf-8"), prompt[: len(base)].encode("utf-8"))
                    suffix = prompt[len(base) :]
                    for config_id in expected_ids:
                        self.assertIn(config_id, suffix)
                    for config_id in set(config_ids("main", count)) - set(expected_ids):
                        self.assertNotIn(config_id, suffix)

    def test_initial_and_frozen_repair_bases_use_the_same_chunk_wrapper(self) -> None:
        requirements = _requirements(main_count=3, detail_count=3)
        bases = (
            "完整 initial base",
            build_final_prompt_repair_prompt(mode="main", requirements=requirements),
            build_set_final_prompt_repair_prompt(mode="detail"),
        )
        for base in bases:
            prompt = build_final_prompt_chunk_prompt(
                base,
                2,
                mode="main" if base != bases[-1] else "detail",
                requirements=requirements,
            )
            self.assertTrue(prompt.startswith(base))
            self.assertIn("P2-d 最终提示词分段执行覆盖", prompt[len(base) :])

    def test_single_and_set_subsets_reuse_per_item_validation_for_both_modes(self) -> None:
        requirements = _requirements()
        for mode in ("main", "detail"):
            enabled = (f"{mode}_03",)
            expected_ids = pair_config_ids(mode, 3)[0]
            single_config = _variable_config(mode, 3, enabled_ids=enabled)
            set_config = _variable_config(
                mode,
                3,
                product_id=SET_PRODUCT_ID,
                enabled_ids=enabled,
                is_set=True,
            )
            for is_set, parser, variable_config in (
                (False, _single_parse, single_config),
                (True, _set_parse, set_config),
            ):
                with self.subTest(mode=mode, is_set=is_set):
                    response = _response(mode, expected_ids, enabled_ids=enabled, is_set=is_set)
                    parsed = parser(
                        response,
                        mode=mode,
                        requirements=requirements,
                        variable_config=variable_config,
                        expected_config_ids=expected_ids,
                    )
                    self.assertEqual(list(expected_ids), list(parsed))

                    wrong_count = _response(
                        mode,
                        tuple(config_ids(mode, 3)),
                        enabled_ids=enabled,
                        is_set=is_set,
                    )
                    with self.assertRaises(ExecutorExecutionError):
                        parser(
                            wrong_count,
                            mode=mode,
                            requirements=requirements,
                            variable_config=variable_config,
                            expected_config_ids=expected_ids,
                        )

                    mutated = copy.deepcopy(response)
                    mutated["prompts"][0]["final_prompt"] = "画布比例固定为 9:9。"
                    with self.assertRaises(ExecutorExecutionError):
                        parser(
                            mutated,
                            mode=mode,
                            requirements=requirements,
                            variable_config=variable_config,
                            expected_config_ids=expected_ids,
                        )

    def test_default_full_parse_keeps_the_original_complete_batch_behavior(self) -> None:
        requirements = _requirements()
        for is_set, parser, product_id in (
            (False, _single_parse, SINGLE_PRODUCT_ID),
            (True, _set_parse, SET_PRODUCT_ID),
        ):
            for mode in ("main", "detail"):
                enabled = (f"{mode}_03",)
                ids = tuple(config_ids(mode, 3))
                variable_config = _variable_config(
                    mode,
                    3,
                    product_id=product_id,
                    enabled_ids=enabled,
                    is_set=is_set,
                )
                response = _response(mode, ids, enabled_ids=enabled, is_set=is_set)
                default = parser(
                    response,
                    mode=mode,
                    requirements=requirements,
                    variable_config=variable_config,
                )
                self.assertEqual(list(ids), list(default))

    def test_explicit_subset_must_be_exactly_one_pair_chunk(self) -> None:
        requirements = _requirements(main_count=3, handheld_main=1)
        variable_config = _variable_config("main", 3, enabled_ids=("main_03",))

        odd_tail = ("main_03",)
        parsed_tail = _single_parse(
            _response("main", odd_tail, enabled_ids=odd_tail),
            mode="main",
            requirements=requirements,
            variable_config=variable_config,
            expected_config_ids=odd_tail,
        )
        self.assertEqual(["main_03"], list(parsed_tail))

        invalid_subsets = (
            ("main_01", "main_02", "main_03"),
            ("main_02", "main_03"),
            ("main_01",),
            ("main_02", "main_01"),
            ("main_01", "main_01"),
            ("main_99",),
        )
        for invalid_ids in invalid_subsets:
            with self.subTest(expected_config_ids=invalid_ids):
                with self.assertRaisesRegex(ExecutorExecutionError, "分段编号异常"):
                    _single_parse(
                        _response("main", invalid_ids),
                        mode="main",
                        requirements=requirements,
                        variable_config=variable_config,
                        expected_config_ids=invalid_ids,
                    )

    def test_n_at_most_two_explicit_all_ids_are_the_single_legal_chunk(self) -> None:
        requirements = _requirements(main_count=2, detail_count=2)
        for is_set, parser, product_id in (
            (False, _single_parse, SINGLE_PRODUCT_ID),
            (True, _set_parse, SET_PRODUCT_ID),
        ):
            for mode in ("main", "detail"):
                enabled = (f"{mode}_02",)
                ids = tuple(config_ids(mode, 2))
                variable_config = _variable_config(
                    mode,
                    2,
                    product_id=product_id,
                    enabled_ids=enabled,
                    is_set=is_set,
                )
                response = _response(mode, ids, enabled_ids=enabled, is_set=is_set)
                default = parser(
                    response,
                    mode=mode,
                    requirements=requirements,
                    variable_config=variable_config,
                )
                chunk = parser(
                    response,
                    mode=mode,
                    requirements=requirements,
                    variable_config=variable_config,
                    expected_config_ids=ids,
                )
                self.assertEqual(default, chunk)

    def test_aggregate_handheld_gate_runs_only_after_single_chunks_merge(self) -> None:
        requirements = _requirements(handheld_main=1)
        variable_config = _variable_config("main", 3, enabled_ids=())
        chunks = []
        for ids in pair_config_ids("main", 3):
            chunks.append(
                _single_parse(
                    _response("main", ids),
                    mode="main",
                    requirements=requirements,
                    variable_config=variable_config,
                    expected_config_ids=ids,
                )
            )
        merged = assemble_final_prompt_chunks(chunks, mode="main", requirements=requirements)
        with self.assertRaisesRegex(ExecutorExecutionError, "上游手持数量异常"):
            _single_parse(
                merged,
                mode="main",
                requirements=requirements,
                variable_config=variable_config,
            )

    def test_set_aggregate_helper_is_skipped_by_subset_and_called_by_full_parse(self) -> None:
        requirements = _requirements()
        variable_config = _variable_config(
            "main", 3, product_id=SET_PRODUCT_ID, enabled_ids=("main_03",), is_set=True
        )
        first_ids = pair_config_ids("main", 3)[0]
        with mock.patch.object(
            downstream,
            "_set_final_prompt_enabled_count",
            wraps=downstream._set_final_prompt_enabled_count,
        ) as aggregate:
            _set_parse(
                _response("main", first_ids, enabled_ids=("main_03",), is_set=True),
                mode="main",
                requirements=requirements,
                variable_config=variable_config,
                expected_config_ids=first_ids,
            )
            self.assertEqual(0, aggregate.call_count)
            _set_parse(
                _response(
                    "main",
                    tuple(config_ids("main", 3)),
                    enabled_ids=("main_03",),
                    is_set=True,
                ),
                mode="main",
                requirements=requirements,
                variable_config=variable_config,
            )
            self.assertEqual(1, aggregate.call_count)

    def test_assembler_rejects_missing_duplicate_and_out_of_order_chunks(self) -> None:
        requirements = _requirements()
        valid_chunks = [
            {
                config_id: {"final_prompt": f"prompt {config_id}", "negative_prompt": "negative"}
                for config_id in ids
            }
            for ids in pair_config_ids("main", 3)
        ]
        with self.assertRaisesRegex(ExecutorExecutionError, "分段数量异常"):
            assemble_final_prompt_chunks(valid_chunks[:1], mode="main", requirements=requirements)
        for invalid in (
            [valid_chunks[0], valid_chunks[0]],
            list(reversed(valid_chunks)),
            [dict(reversed(list(valid_chunks[0].items()))), valid_chunks[1]],
        ):
            with self.assertRaisesRegex(ExecutorExecutionError, "分段覆盖异常"):
                assemble_final_prompt_chunks(invalid, mode="main", requirements=requirements)


class P2dFinalPromptExecutorParallelTests(unittest.TestCase):
    @staticmethod
    def _result(turn: CodexTurnResult) -> _FinalPromptChainResult:
        return _FinalPromptChainResult(batch={}, turn=turn, correction_attempts=0)

    def test_one_chunk_per_mode_still_uses_segment_pool_and_1200_timeout(self) -> None:
        transport = _SharedPoolProbeTransport(wait_for_peak=2)
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )
        heartbeats = 0

        def heartbeat() -> None:
            nonlocal heartbeats
            heartbeats += 1

        executor.set_turn_progress_callback(heartbeat)
        main, detail = executor._run_final_prompt_chunks(
            main_prompts=("main-1",),
            main_parse_turn=lambda _index, turn: self._result(turn),
            detail_prompts=("detail-1",),
            detail_parse_turn=lambda _index, turn: self._result(turn),
        )

        self.assertEqual(1, len(main))
        self.assertEqual(1, len(detail))
        self.assertEqual(2, transport.peak)
        self.assertTrue(transport.saw_both_modes)
        self.assertEqual([1200.0, 1200.0], sorted(transport.timeouts))
        self.assertEqual(2, heartbeats)

    def test_main_and_detail_thirty_images_share_one_pool_with_peak_four(self) -> None:
        transport = _SharedPoolProbeTransport()
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )
        main_prompts = tuple(f"main-{index}" for index in range(1, 16))
        detail_prompts = tuple(f"detail-{index}" for index in range(1, 16))

        main, detail = executor._run_final_prompt_chunks(
            main_prompts=main_prompts,
            main_parse_turn=lambda _index, turn: self._result(turn),
            detail_prompts=detail_prompts,
            detail_parse_turn=lambda _index, turn: self._result(turn),
        )

        self.assertEqual(15, len(main))
        self.assertEqual(15, len(detail))
        self.assertEqual(4, transport.peak)
        self.assertTrue(transport.saw_both_modes)
        self.assertCountEqual((*main_prompts, *detail_prompts), transport.completed)
        self.assertEqual({1200.0}, set(transport.timeouts))

    def test_failures_drain_all_segments_then_choose_smallest_main_chunk(self) -> None:
        transport = _SharedPoolProbeTransport()
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )
        parsed: list[tuple[str, int]] = []
        failure_completion_order: list[tuple[str, int]] = []
        lock = threading.Lock()
        main_chunk_three_failed = threading.Event()

        def parse(mode: str, index: int, turn: CodexTurnResult) -> _FinalPromptChainResult:
            with lock:
                parsed.append((mode, index))
            if mode == "main" and index == 1:
                if not main_chunk_three_failed.wait(timeout=3):
                    raise AssertionError("queued main chunk 3 did not finish first")
                with lock:
                    failure_completion_order.append((mode, index))
                raise ExecutorExecutionError("main chunk 1 failed")
            if mode == "main" and index == 3:
                with lock:
                    failure_completion_order.append((mode, index))
                main_chunk_three_failed.set()
                raise ExecutorExecutionError("main chunk 3 failed")
            if mode == "detail" and index == 1:
                with lock:
                    failure_completion_order.append((mode, index))
                raise ExecutorExecutionError("detail chunk 1 failed")
            return self._result(turn)

        with self.assertRaisesRegex(ExecutorExecutionError, "main chunk 1 failed"):
            executor._run_final_prompt_chunks(
                main_prompts=tuple(f"main-{index}" for index in range(1, 4)),
                main_parse_turn=lambda index, turn: parse("main", index, turn),
                detail_prompts=tuple(f"detail-{index}" for index in range(1, 4)),
                detail_parse_turn=lambda index, turn: parse("detail", index, turn),
            )

        self.assertCountEqual(
            [(mode, index) for mode in ("main", "detail") for index in range(1, 4)],
            parsed,
        )
        self.assertEqual(6, len(transport.completed))
        self.assertLess(
            failure_completion_order.index(("main", 3)),
            failure_completion_order.index(("main", 1)),
        )

    def test_correction_limit_is_independent_per_segment_and_all_turns_heartbeat(self) -> None:
        transport = _CorrectionTransport()
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )
        heartbeats = 0
        attempts: dict[tuple[str, int], int] = {}
        lock = threading.Lock()

        def heartbeat() -> None:
            nonlocal heartbeats
            with lock:
                heartbeats += 1

        executor.set_turn_progress_callback(heartbeat)

        def parse_chunk(
            mode: str,
            index: int,
            turn: CodexTurnResult,
        ) -> _FinalPromptChainResult:
            key = (mode, index)

            def parse_response(_text: str) -> dict[str, dict[str, str]]:
                with lock:
                    attempts[key] = attempts.get(key, 0) + 1
                    attempt = attempts[key]
                if attempt <= 2:
                    raise downstream.FinalPromptLiteralViolation(
                        mode=mode,
                        safe_reason="未保留画布比例",
                    )
                return {}

            batch, corrected_turn, correction_attempts = (
                executor._parse_final_prompt_with_bounded_correction(
                    turn,
                    mode=mode,
                    product_id=SINGLE_PRODUCT_ID,
                    requirements=_requirements(),
                    angle_inventory={},
                    variable_config={},
                    style_master_text="style",
                    correction_attempts=0,
                    parse_response=parse_response,
                    repair_prompt_builder=lambda: f"repair-{mode}-{index}",
                )
            )
            return _FinalPromptChainResult(
                batch=batch,
                turn=corrected_turn,
                correction_attempts=correction_attempts,
            )

        main, detail = executor._run_final_prompt_chunks(
            main_prompts=("main-1", "main-2"),
            main_parse_turn=lambda index, turn: parse_chunk("main", index, turn),
            detail_prompts=("detail-1", "detail-2"),
            detail_parse_turn=lambda index, turn: parse_chunk("detail", index, turn),
        )

        self.assertEqual({3}, set(attempts.values()))
        self.assertEqual(8, sum(result.correction_attempts for result in (*main, *detail)))
        self.assertEqual({1200.0}, set(transport.initial_timeouts))
        self.assertEqual({1200.0}, set(transport.correction_timeouts))
        self.assertEqual(12, heartbeats)

    def test_exhausted_segment_does_not_cancel_other_segment_corrections(self) -> None:
        transport = _CorrectionTransport()
        executor = CodexDevExecutor(
            ExecutorContext(manifest={}),
            transport=transport,
            repository_root=ROOT,
        )
        attempts: dict[tuple[str, int], int] = {}
        lock = threading.Lock()

        def parse_chunk(
            mode: str,
            index: int,
            turn: CodexTurnResult,
        ) -> _FinalPromptChainResult:
            key = (mode, index)
            required_failures = 3 if key == ("main", 1) else 2

            def parse_response(_text: str) -> dict[str, dict[str, str]]:
                with lock:
                    attempts[key] = attempts.get(key, 0) + 1
                    attempt = attempts[key]
                if attempt <= required_failures:
                    raise downstream.FinalPromptLiteralViolation(
                        mode=mode,
                        safe_reason="未保留画布比例",
                    )
                return {}

            batch, corrected_turn, correction_attempts = (
                executor._parse_final_prompt_with_bounded_correction(
                    turn,
                    mode=mode,
                    product_id=SINGLE_PRODUCT_ID,
                    requirements=_requirements(),
                    angle_inventory={},
                    variable_config={},
                    style_master_text="style",
                    correction_attempts=0,
                    parse_response=parse_response,
                    repair_prompt_builder=lambda: f"repair-{mode}-{index}",
                )
            )
            return _FinalPromptChainResult(
                batch=batch,
                turn=corrected_turn,
                correction_attempts=correction_attempts,
            )

        with self.assertRaisesRegex(ExecutorExecutionError, "主图最终提示词纠正已达到上限"):
            executor._run_final_prompt_chunks(
                main_prompts=("main-1", "main-2"),
                main_parse_turn=lambda index, turn: parse_chunk("main", index, turn),
                detail_prompts=("detail-1", "detail-2"),
                detail_parse_turn=lambda index, turn: parse_chunk("detail", index, turn),
            )

        self.assertEqual(3, attempts[("main", 1)])
        self.assertEqual(3, attempts[("main", 2)])
        self.assertEqual(3, attempts[("detail", 1)])
        self.assertEqual(3, attempts[("detail", 2)])
        self.assertEqual(8, len(transport.continuations))


class P2dFinalPromptBundleParityTests(unittest.TestCase):
    def _parse_all_chunks(
        self,
        *,
        mode: str,
        requirements: UserConfirmedRequirements,
        variable_config: dict[str, object],
        enabled_ids: tuple[str, ...],
        is_set: bool,
    ) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
        parser = _set_parse if is_set else _single_parse
        ids = tuple(config_ids(mode, 3))
        full_response = _response(mode, ids, enabled_ids=enabled_ids, is_set=is_set)
        direct = parser(
            full_response,
            mode=mode,
            requirements=requirements,
            variable_config=variable_config,
        )
        parsed_chunks = [
            parser(
                _response(mode, chunk_ids, enabled_ids=enabled_ids, is_set=is_set),
                mode=mode,
                requirements=requirements,
                variable_config=variable_config,
                expected_config_ids=chunk_ids,
            )
            for chunk_ids in pair_config_ids(mode, 3)
        ]
        merged_response = assemble_final_prompt_chunks(
            parsed_chunks,
            mode=mode,
            requirements=requirements,
        )
        merged = parser(
            merged_response,
            mode=mode,
            requirements=requirements,
            variable_config=variable_config,
        )
        return direct, merged

    def test_single_bundle_bytes_match_direct_full_batch(self) -> None:
        requirements = _requirements()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            variable_configs = {}
            direct_batches = {}
            merged_batches = {}
            for mode in ("main", "detail"):
                enabled = (f"{mode}_03",)
                document = _variable_config(mode, 3, enabled_ids=enabled)
                source = root / f"{mode}_vc.json"
                source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                variable_configs[mode] = (document, source)
                direct_batches[mode], merged_batches[mode] = self._parse_all_chunks(
                    mode=mode,
                    requirements=requirements,
                    variable_config=document,
                    enabled_ids=enabled,
                    is_set=False,
                )
            kwargs = {
                "product_id": SINGLE_PRODUCT_ID,
                "output_dir": root / "final",
                "variable_configs": variable_configs,
                "upstream_paths": {
                    "product_identity_archive": root / "identity.json",
                    "style_master": root / "style.json",
                    "angle_inventory": root / "angles.json",
                },
                "angle_inventory": _angle_inventory(),
                "requirements": requirements,
            }
            direct = build_final_prompt_bundle(prompt_batches=direct_batches, **kwargs)
            chunked = build_final_prompt_bundle(prompt_batches=merged_batches, **kwargs)
            self.assertEqual(direct, chunked)

    def test_set_bundle_bytes_match_direct_full_batch(self) -> None:
        requirements = _requirements()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            variable_configs = {}
            direct_batches = {}
            merged_batches = {}
            for mode in ("main", "detail"):
                enabled = (f"{mode}_03",)
                document = _variable_config(
                    mode,
                    3,
                    product_id=SET_PRODUCT_ID,
                    enabled_ids=enabled,
                    is_set=True,
                )
                source = root / f"set_{mode}_vc.json"
                source.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
                variable_configs[mode] = (document, source)
                direct_batches[mode], merged_batches[mode] = self._parse_all_chunks(
                    mode=mode,
                    requirements=requirements,
                    variable_config=document,
                    enabled_ids=enabled,
                    is_set=True,
                )
            upstream_paths = {
                "set_product_identity": root / "set_identity.json",
                "component_identity_archive_01": root / "component_01.json",
                "component_identity_archive_02": root / "component_02.json",
                "style_master": root / "style.json",
                "set_angle_layout_inventory": root / "set_angles.json",
            }
            kwargs = {
                "product_id": SET_PRODUCT_ID,
                "output_dir": root / "set_final",
                "variable_configs": variable_configs,
                "upstream_paths": upstream_paths,
                "set_angle_layout_inventory": valid_set_layout_inventory(),
                "requirements": requirements,
            }
            direct = build_set_final_prompt_bundle(prompt_batches=direct_batches, **kwargs)
            chunked = build_set_final_prompt_bundle(prompt_batches=merged_batches, **kwargs)
            self.assertEqual(direct, chunked)


class P2dFinalPromptSingleExecutorBundleTests(CodexDevFixture):
    def test_single_executor_revalidates_each_assembled_chain_once_with_full_parser(
        self,
    ) -> None:
        with mock.patch.object(
            executor_module,
            "parse_final_prompt_batch_response",
            wraps=executor_module.parse_final_prompt_batch_response,
        ) as parser_spy:
            self._run_executor_and_assert_direct_full_batch_bytes()

        _assert_assembled_chains_revalidated_as_full_batches(
            self,
            parser_spy,
            counts={"main": 6, "detail": 8},
        )

    def test_executor_writes_direct_full_batch_bytes_and_only_plural_thread_metadata(
        self,
    ) -> None:
        self._run_executor_and_assert_direct_full_batch_bytes()

    def _run_executor_and_assert_direct_full_batch_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, final_dir, main_path, detail_path = self.make_final_prompt_fixture(root)
            requirements = downstream.parse_user_confirmed_requirements(
                context.manifest,
                root,
            )
            identity, identity_path = downstream.load_typed_artifact(
                context.manifest,
                "product_identity_archive",
                "product_identity_archive.json",
                "product_identity_archive",
                "产品身份档案",
            )
            style_master, style_path = downstream.load_typed_artifact(
                context.manifest,
                "style_master",
                "style_master.json",
                "style_master",
                "风格母版",
            )
            angle_inventory, angle_path = downstream.load_typed_artifact(
                context.manifest,
                "angle_inventory",
                "angle_inventory.json",
                "angle_inventory",
                "角度槽位入库表",
            )
            del identity
            main_variable_config = json.loads(main_path.read_text(encoding="utf-8"))
            detail_variable_config = json.loads(detail_path.read_text(encoding="utf-8"))
            style_master_text = downstream.style_master_material_reference_text(
                style_master,
                product_id="p1",
            )
            direct_batches = {
                mode: parse_final_prompt_batch_response(
                    json.dumps(valid_final_prompt_response(mode), ensure_ascii=False),
                    mode=mode,
                    product_id="p1",
                    requirements=requirements,
                    angle_inventory=angle_inventory,
                    variable_config=(
                        main_variable_config if mode == "main" else detail_variable_config
                    ),
                    style_master_text=style_master_text,
                )
                for mode in ("main", "detail")
            }
            expected_bundle = build_final_prompt_bundle(
                product_id="p1",
                output_dir=final_dir,
                prompt_batches=direct_batches,
                variable_configs={
                    "main": (main_variable_config, main_path),
                    "detail": (detail_variable_config, detail_path),
                },
                upstream_paths={
                    "product_identity_archive": identity_path,
                    "style_master": style_path,
                    "angle_inventory": angle_path,
                },
                angle_inventory=angle_inventory,
                requirements=requirements,
            )

            transport = _FixtureChunkTransport()
            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(ExecutionRequest(step="final_prompts"))
            actual_bundle = {path: path.read_bytes() for path in expected_bundle}

            self.assertEqual(expected_bundle, actual_bundle)
            self.assertEqual(7, len(transport.timeouts))
            self.assertEqual({1200.0}, set(transport.timeouts))
            self.assertEqual(3, len(result.metadata["main_thread_ids"]))
            self.assertEqual(4, len(result.metadata["detail_thread_ids"]))
            self.assertEqual(0, result.metadata["correction_attempts"])
            self.assertNotIn("main_thread_id", result.metadata)
            self.assertNotIn("detail_thread_id", result.metadata)


class P2dFinalPromptSetExecutorBundleTests(SetVariableConfigFixture):
    def test_set_executor_revalidates_each_assembled_chain_once_with_full_parser(
        self,
    ) -> None:
        with mock.patch.object(
            executor_module,
            "parse_set_final_prompt_batch_response",
            wraps=executor_module.parse_set_final_prompt_batch_response,
        ) as parser_spy:
            self._run_set_executor_and_assert_direct_full_batch_bytes()

        _assert_assembled_chains_revalidated_as_full_batches(
            self,
            parser_spy,
            counts={"main": 2, "detail": 3},
        )

    def test_set_executor_writes_direct_full_batch_bytes_and_plural_metadata(
        self,
    ) -> None:
        self._run_set_executor_and_assert_direct_full_batch_bytes()

    def _run_set_executor_and_assert_direct_full_batch_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor, _prior_transport, manifest, paths = self.make_executor(root, [])
            main_count = 2
            detail_count = 3
            enabled_ids = {"main": ("main_01",), "detail": ("detail_01",)}
            manifest["user_confirmed_facts"] = set_manifest_facts(
                main_count=main_count,
                detail_count=detail_count,
                handheld_main=1,
                handheld_detail=1,
            )
            final_dir = Path(manifest["workspace"]["artifacts_root"]) / "final_prompts"
            manifest["artifacts"]["final_prompts"] = str(final_dir)
            for index in (1, 2):
                component_path = (
                    paths["identity"]
                    / f"component_{index:02d}_product_identity_archive.json"
                )
                component_path.write_text(
                    json.dumps(
                        _component_identity_with_negatives(index),
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            documents = {
                "main": _formal_set_variable_config(
                    paths["main"],
                    mode="main",
                    count=main_count,
                    handheld_target=1,
                    enabled_ids=enabled_ids["main"],
                ),
                "detail": _formal_set_variable_config(
                    paths["detail"],
                    mode="detail",
                    count=detail_count,
                    handheld_target=1,
                    enabled_ids=enabled_ids["detail"],
                ),
            }
            variable_paths = {
                mode: paths[mode] / f"{mode}_variable_configs.json"
                for mode in ("main", "detail")
            }
            for mode in ("main", "detail"):
                variable_paths[mode].write_text(
                    json.dumps(documents[mode], ensure_ascii=False),
                    encoding="utf-8",
                )
            executor.context.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            requirements = downstream.parse_user_confirmed_requirements(manifest, ROOT)
            set_identity = valid_set_identity()
            component_identities = tuple(
                _component_identity_with_negatives(index) for index in (1, 2)
            )
            layout = valid_set_layout_inventory()
            style_path = paths["style"] / "style_master.json"
            style_master = json.loads(style_path.read_text(encoding="utf-8"))
            style_master_text = downstream.style_master_material_reference_text(
                style_master,
                product_id=SET_PRODUCT_ID,
            )
            full_responses = {
                mode: _final_prompt_response(
                    mode,
                    count=main_count if mode == "main" else detail_count,
                    enabled_ids=enabled_ids[mode],
                )
                for mode in ("main", "detail")
            }
            direct_batches = {
                mode: parse_set_final_prompt_batch_response(
                    json.dumps(full_responses[mode], ensure_ascii=False),
                    mode=mode,
                    product_id=SET_PRODUCT_ID,
                    requirements=requirements,
                    set_identity=set_identity,
                    component_identities=component_identities,
                    set_angle_layout_inventory=layout,
                    variable_config=documents[mode],
                    style_master_text=style_master_text,
                )
                for mode in ("main", "detail")
            }
            set_identity_path = paths["identity"] / "set_product_identity.json"
            component_paths = tuple(
                paths["identity"]
                / f"component_{index:02d}_product_identity_archive.json"
                for index in (1, 2)
            )
            layout_path = paths["layout"] / "set_angle_layout_inventory.json"
            upstream_keys = downstream.expand_set_final_prompt_upstream_keys(2)[:-1]
            upstream_paths = dict(
                zip(
                    upstream_keys,
                    (
                        set_identity_path,
                        *component_paths,
                        style_path,
                        layout_path,
                    ),
                    strict=True,
                )
            )
            expected_bundle = build_set_final_prompt_bundle(
                product_id=SET_PRODUCT_ID,
                output_dir=final_dir,
                prompt_batches=direct_batches,
                variable_configs={
                    mode: (documents[mode], variable_paths[mode])
                    for mode in ("main", "detail")
                },
                upstream_paths=upstream_paths,
                set_angle_layout_inventory=layout,
                requirements=requirements,
            )

            transport = _FixtureChunkTransport(full_responses)
            executor.transport = transport
            result = executor.execute(ExecutionRequest(step="final_prompts"))
            actual_bundle = {path: path.read_bytes() for path in expected_bundle}

            self.assertEqual(expected_bundle, actual_bundle)
            self.assertEqual(3, len(transport.timeouts))
            self.assertEqual({1200.0}, set(transport.timeouts))
            self.assertEqual(1, len(result.metadata["main_thread_ids"]))
            self.assertEqual(2, len(result.metadata["detail_thread_ids"]))
            self.assertEqual(0, result.metadata["correction_attempts"])
            self.assertNotIn("main_thread_id", result.metadata)
            self.assertNotIn("detail_thread_id", result.metadata)


if __name__ == "__main__":
    unittest.main()
