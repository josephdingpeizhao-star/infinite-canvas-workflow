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
from typing import Callable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import codex_dev_executor as executor_module  # noqa: E402
from codex_dev_executor import (  # noqa: E402
    DETAIL_CHUNK_MAX_CONCURRENCY,
    CodexDevExecutor,
    CodexTurnResult,
)
from image_count_contract import (  # noqa: E402
    ImageCountContractError,
    main_handheld_chunk_quotas,
    pair_config_ids,
)
from codex_dev_downstream import (  # noqa: E402
    MainChunkEnvelopeCorrection,
    MainChunkTransportCorruption,
    UserConfirmedRequirements,
    assemble_main_variable_config_chunks,
    build_main_variable_config_chunk_prompt,
    main_chunk_business_fingerprint,
    main_variable_config_chunk_count,
    parse_main_variable_config_chunk,
    parse_set_variable_config_response,
    parse_variable_config_response,
    write_json_exclusive,
)
from content_correction import ContentPredicateViolation  # noqa: E402
from executor_contract import ExecutorContext, ExecutorExecutionError  # noqa: E402
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    FakeTransport,
    VALID_STYLE_MASTER,
    valid_main_variable_response,
)
from tests.test_st03b_set_variable_config import (  # noqa: E402
    PRODUCT_ID as SET_PRODUCT_ID,
    SetVariableConfigFixture,
    set_requirements,
    valid_component_identity,
    valid_set_identity,
    valid_set_layout_inventory,
    valid_set_variable_response,
    valid_style_master as valid_set_style_master,
)


def _requirements(
    *,
    main_count: int = 3,
    handheld_main: int = 2,
) -> UserConfirmedRequirements:
    return UserConfirmedRequirements(
        product_type="家居盛水水壶",
        height_cm=25,
        handheld_main=handheld_main,
        handheld_detail=1,
        allow_clear_water=True,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        main_image_count=main_count,
        detail_image_count=8,
        category="杯类",
    )


def _angle_inventory() -> dict[str, object]:
    assets = (("img_001", "A"), ("img_006", "B"), ("img_007", "C"))
    return {
        "product_id": "p2d-single",
        "artifact_type": "angle_inventory",
        "image_assets": [
            {"asset_id": asset_id, "file_path": f"{asset_id}.jpg"}
            for asset_id, _slot in assets
        ],
        "angle_slots": [
            {
                "source_asset_id": asset_id,
                "angle_slot": slot,
                "admission_result": "合格，可进入对应槽位",
                "camera_angle": f"{slot} 机位",
            }
            for asset_id, slot in assets
        ],
        "missing_angle_slots": ["D"],
        "retake_recommendations": [],
        "notes": "D 不补拍",
    }


def _set_handheld(config: dict[str, object], *, enabled: bool, is_set: bool) -> None:
    overrides = config["per_image_overrides"]
    if enabled:
        overrides["手持交互声明"] = (
            "本张图启用手持场景。手持子场景类型：静态握持。"
            + (
                "持握套装中某一主体单件，其余单件作为静物陈列。"
                if is_set
                else "单手自然握住把手，不离桌，不倾倒"
            )
        )
        overrides["动态手持样式参考图调用"] = "无，仅动态拿起场景可调用"
    else:
        overrides["手持交互声明"] = "本张图不启用手持场景"
        overrides["动态手持样式参考图调用"] = "无"


def _quota_enabled_ids(main_count: int, handheld_main: int) -> tuple[str, ...]:
    batches = pair_config_ids("main", main_count)
    quotas = main_handheld_chunk_quotas(main_count, handheld_main)
    return tuple(
        config_id
        for batch, quota in zip(batches, quotas, strict=True)
        for config_id in batch[:quota]
    )


def _single_response(
    *,
    main_count: int,
    handheld_main: int,
) -> dict[str, object]:
    response = copy.deepcopy(valid_main_variable_response())
    templates = response["configs"]
    response["configs"] = [
        {
            **copy.deepcopy(templates[index % len(templates)]),
            "config_id": f"main_{index + 1:02d}",
        }
        for index in range(main_count)
    ]
    enabled_ids = set(_quota_enabled_ids(main_count, handheld_main))
    for config in response["configs"]:
        _set_handheld(
            config,
            enabled=config["config_id"] in enabled_ids,
            is_set=False,
        )
    response["handheld_count_summary"] = {
        "用户要求主图手持数量": handheld_main,
        "实际启用手持数量": handheld_main,
        "未启用手持数量": main_count - handheld_main,
        "启用手持配置": [
            config["config_id"]
            for config in response["configs"]
            if config["config_id"] in enabled_ids
        ],
        "是否完全满足用户数量": "是",
    }
    return response


def _set_response(
    *,
    main_count: int,
    handheld_main: int,
) -> dict[str, object]:
    return valid_set_variable_response(
        "main",
        count=main_count,
        handheld_target=handheld_main,
        enabled_ids=_quota_enabled_ids(main_count, handheld_main),
    )


def _main_chunks(
    response: dict[str, object],
    *,
    requirements: UserConfirmedRequirements,
    is_set: bool,
) -> list[dict[str, object]]:
    configs = response["configs"]
    batches = pair_config_ids("main", len(configs))
    quotas = main_handheld_chunk_quotas(
        len(configs),
        requirements.handheld_main,
    )
    chunks: list[dict[str, object]] = []
    offset = 0
    for chunk_index, (batch, quota) in enumerate(
        zip(batches, quotas, strict=True),
        start=1,
    ):
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


def _parse_single_chunk(
    chunk: dict[str, object],
    *,
    requirements: UserConfirmedRequirements,
) -> dict[str, object]:
    return parse_main_variable_config_chunk(
        json.dumps(chunk, ensure_ascii=False),
        int(chunk["chunk_index"]),
        requirements=requirements,
        angle_inventory=_angle_inventory(),
    )


def _parse_set_chunk(
    chunk: dict[str, object],
    *,
    requirements: UserConfirmedRequirements,
) -> dict[str, object]:
    return parse_main_variable_config_chunk(
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


class MainChunkContractTests(unittest.TestCase):
    def test_pair_boundaries_use_one_canonical_two_config_partition(self) -> None:
        expected = {
            1: (("main_01",),),
            2: (("main_01", "main_02"),),
            3: (("main_01", "main_02"), ("main_03",)),
        }
        for count, chunks in expected.items():
            with self.subTest(count=count):
                self.assertEqual(chunks, pair_config_ids("main", count))

        chunks_30 = pair_config_ids("main", 30)
        self.assertEqual(15, len(chunks_30))
        self.assertEqual(("main_01", "main_02"), chunks_30[0])
        self.assertEqual(("main_29", "main_30"), chunks_30[-1])

    def test_quota_distribution_is_deterministic_and_preserves_target(self) -> None:
        cases = (
            (1, 0, (0,)),
            (1, 1, (1,)),
            (2, 0, (0,)),
            (2, 1, (1,)),
            (2, 2, (2,)),
            (3, 0, (0, 0)),
            (3, 1, (1, 0)),
            (3, 2, (1, 1)),
            (3, 3, (2, 1)),
            (30, 0, (0,) * 15),
            (30, 1, (1,) + (0,) * 14),
            (30, 29, (2,) * 14 + (1,)),
            (30, 30, (2,) * 15),
        )
        for count, target, expected in cases:
            with self.subTest(count=count, target=target):
                first = main_handheld_chunk_quotas(count, target)
                second = main_handheld_chunk_quotas(count, target)
                self.assertEqual(expected, first)
                self.assertEqual(first, second)
                self.assertEqual(target, sum(first))

    def test_each_quota_stays_within_its_chunk_capacity(self) -> None:
        for count in (1, 2, 3, 30):
            chunks = pair_config_ids("main", count)
            for target in (0, 1, max(0, count - 1), count):
                with self.subTest(count=count, target=target):
                    quotas = main_handheld_chunk_quotas(count, target)
                    self.assertEqual(len(chunks), len(quotas))
                    self.assertTrue(
                        all(
                            0 <= quota <= len(chunk)
                            for quota, chunk in zip(quotas, chunks, strict=True)
                        )
                    )

    def test_invalid_targets_and_over_capacity_fail_closed(self) -> None:
        for count, target in (
            (1, 2),
            (2, 3),
            (3, 4),
            (30, 31),
            (3, -1),
            (3, True),
            (3, 1.5),
            (3, "1"),
            (3, None),
        ):
            with self.subTest(count=count, target=target):
                with self.assertRaises(ImageCountContractError):
                    main_handheld_chunk_quotas(count, target)  # type: ignore[arg-type]

    def test_invalid_counts_fail_closed_before_quota_distribution(self) -> None:
        for count in (0, 31, -1, True, 1.5, "3", None):
            with self.subTest(count=count):
                with self.assertRaises(ImageCountContractError):
                    main_handheld_chunk_quotas(count, 0)  # type: ignore[arg-type]


class MainChunkPromptTests(unittest.TestCase):
    def test_all_counts_use_the_chunk_envelope_without_a_single_chunk_bypass(self) -> None:
        for count, expected_chunks in ((1, 1), (2, 1), (3, 2)):
            requirements = _requirements(main_count=count, handheld_main=min(1, count))
            with self.subTest(count=count):
                self.assertEqual(
                    expected_chunks,
                    main_variable_config_chunk_count(requirements),
                )
                prompt = build_main_variable_config_chunk_prompt(
                    "UNCHANGED_FULL_BASE",
                    1,
                    requirements=requirements,
                )
                self.assertTrue(prompt.startswith("UNCHANGED_FULL_BASE\n\n"))
                self.assertIn(f"本轮只返回第 1/{expected_chunks} 段", prompt)
                self.assertIn('"handheld_chunk_summary"', prompt)
                self.assertIn("任一分段均不得返回 handheld_count_summary", prompt)

    def test_set_and_recovery_prompts_keep_chunk_scope_and_mutual_exclusion(self) -> None:
        requirements = set_requirements(main_count=3, handheld_main=2)
        set_prompt = build_main_variable_config_chunk_prompt(
            "SET_FULL_BASE",
            2,
            requirements=requirements,
            is_set=True,
        )
        self.assertIn("main_03", set_prompt)
        self.assertIn("本段手持上限 1 项，允许少于", set_prompt)
        self.assertIn("本段手持启用说明", set_prompt)

        repair = build_main_variable_config_chunk_prompt(
            "DO_NOT_REPEAT_BASE",
            2,
            requirements=requirements,
            repair=True,
        )
        self.assertNotIn("DO_NOT_REPEAT_BASE", repair)
        self.assertIn("传输完整性门禁", repair)
        with self.assertRaisesRegex(ExecutorExecutionError, "冲突"):
            build_main_variable_config_chunk_prompt(
                "base",
                1,
                requirements=requirements,
                repair=True,
                structure_correction=True,
            )


class MainChunkParserTests(unittest.TestCase):
    def test_single_chunks_accept_exact_quota_and_reject_more_or_fewer(self) -> None:
        requirements = _requirements(main_count=3, handheld_main=2)
        chunks = _main_chunks(
            _single_response(main_count=3, handheld_main=2),
            requirements=requirements,
            is_set=False,
        )
        parsed = [
            _parse_single_chunk(chunk, requirements=requirements)
            for chunk in chunks
        ]
        self.assertEqual(
            [1, 1],
            [chunk["handheld_chunk_summary"]["本段实际启用数量"] for chunk in parsed],
        )

        for enabled in (0, 2):
            with self.subTest(enabled=enabled):
                tampered = copy.deepcopy(chunks[0])
                for index, config in enumerate(tampered["configs"]):
                    _set_handheld(config, enabled=index < enabled, is_set=False)
                tampered["handheld_chunk_summary"]["本段实际启用数量"] = enabled
                tampered["handheld_chunk_summary"]["本段启用手持配置"] = [
                    config["config_id"] for config in tampered["configs"][:enabled]
                ]
                with self.assertRaises(ContentPredicateViolation) as caught:
                    _parse_single_chunk(tampered, requirements=requirements)
                self.assertEqual("handheld_count", caught.exception.code)

    def test_summary_drift_and_full_batch_summary_are_fail_closed(self) -> None:
        requirements = _requirements(main_count=2, handheld_main=1)
        chunk = _main_chunks(
            _single_response(main_count=2, handheld_main=1),
            requirements=requirements,
            is_set=False,
        )[0]
        tampered = copy.deepcopy(chunk)
        tampered["handheld_chunk_summary"]["本段启用手持配置"] = []
        with self.assertRaises(ContentPredicateViolation) as caught:
            _parse_single_chunk(tampered, requirements=requirements)
        self.assertEqual("handheld_summary", caught.exception.code)

        forbidden = copy.deepcopy(chunk)
        forbidden["handheld_count_summary"] = {}
        with self.assertRaises(ExecutorExecutionError):
            _parse_single_chunk(forbidden, requirements=requirements)

    def test_set_chunk_uses_upper_bound_and_does_not_enforce_global_all_appear(self) -> None:
        requirements = set_requirements(main_count=2, handheld_main=1)
        for enabled_ids in (("main_01",), ()):
            with self.subTest(enabled_ids=enabled_ids):
                response = valid_set_variable_response(
                    "main",
                    count=2,
                    handheld_target=1,
                    enabled_ids=enabled_ids,
                )
                chunk = _main_chunks(
                    response,
                    requirements=requirements,
                    is_set=True,
                )[0]
                for config in chunk["configs"]:
                    config["per_image_overrides"]["套装组成调用"] = "按档案调用全部组成单件。"
                parsed = _parse_set_chunk(chunk, requirements=requirements)
                self.assertEqual(
                    len(enabled_ids),
                    parsed["handheld_chunk_summary"]["本段实际启用数量"],
                )

        above = valid_set_variable_response(
            "main",
            count=2,
            handheld_target=1,
            enabled_ids=("main_01", "main_02"),
        )
        chunk = _main_chunks(above, requirements=requirements, is_set=True)[0]
        with self.assertRaises(ContentPredicateViolation) as caught:
            _parse_set_chunk(chunk, requirements=requirements)
        self.assertEqual("handheld_count", caught.exception.code)

    def test_transport_damage_envelope_correction_and_fingerprint_boundaries(self) -> None:
        requirements = _requirements(main_count=2, handheld_main=1)
        chunk = _main_chunks(
            _single_response(main_count=2, handheld_main=1),
            requirements=requirements,
            is_set=False,
        )[0]
        with self.assertRaises(MainChunkTransportCorruption):
            parse_main_variable_config_chunk(
                '{"chunk_index": 1, "text": "\ufffd"}',
                1,
                requirements=requirements,
                angle_inventory=_angle_inventory(),
            )
        with self.assertRaises(MainChunkTransportCorruption):
            parse_main_variable_config_chunk(
                json.dumps(chunk, ensure_ascii=False)[:-8],
                1,
                requirements=requirements,
                angle_inventory=_angle_inventory(),
            )
        with self.assertRaises(ExecutorExecutionError):
            parse_main_variable_config_chunk(
                "{not-json}",
                1,
                requirements=requirements,
                angle_inventory=_angle_inventory(),
            )

        bad_notes = copy.deepcopy(chunk)
        bad_notes["notes"] = {"not": "a string"}
        with self.assertRaises(MainChunkEnvelopeCorrection) as caught:
            _parse_single_chunk(bad_notes, requirements=requirements)
        self.assertEqual(
            main_chunk_business_fingerprint(bad_notes, 1),
            caught.exception.business_fingerprint,
        )

        original = main_chunk_business_fingerprint(chunk, 1)
        changed_notes = copy.deepcopy(chunk)
        changed_notes["notes"] = "wrapper-only change"
        self.assertEqual(original, main_chunk_business_fingerprint(changed_notes, 1))
        changed_summary = copy.deepcopy(chunk)
        changed_summary["handheld_chunk_summary"]["本段实际启用数量"] = 0
        self.assertNotEqual(original, main_chunk_business_fingerprint(changed_summary, 1))
        changed_common = copy.deepcopy(chunk)
        changed_common["common_constraints"]["extra"] = "business change"
        self.assertNotEqual(original, main_chunk_business_fingerprint(changed_common, 1))


class MainChunkAggregationTests(unittest.TestCase):
    @staticmethod
    def _set_full_parser_kwargs(
        root: Path,
        requirements: UserConfirmedRequirements,
    ) -> dict[str, object]:
        return {
            "mode": "main",
            "product_id": SET_PRODUCT_ID,
            "requirements": requirements,
            "set_identity": valid_set_identity(),
            "component_identities": (
                valid_component_identity(1),
                valid_component_identity(2),
            ),
            "set_angle_layout_inventory": valid_set_layout_inventory(),
            "upstream_paths": {
                "set_product_identity": root / "set_identity.json",
                "component_identity_archive_01": root / "component_1.json",
                "component_identity_archive_02": root / "component_2.json",
                "style_master": root / "style_master.json",
                "set_angle_layout_inventory": root / "layout.json",
            },
        }

    def test_fixed_full_and_chunk_roundtrip_artifacts_match_through_count_thirty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            single_style = copy.deepcopy(VALID_STYLE_MASTER)
            single_style["product_id"] = "p2d-single"
            single_style_path = root / "single_style_master.json"
            single_style_path.write_text(
                json.dumps(single_style, ensure_ascii=False),
                encoding="utf-8",
            )
            (root / "style_master.json").write_text(
                json.dumps(valid_set_style_master(), ensure_ascii=False),
                encoding="utf-8",
            )

            for count in (1, 2, 3, 30):
                target = min(2, count)
                with self.subTest(batch_type="single", count=count):
                    requirements = _requirements(
                        main_count=count,
                        handheld_main=target,
                    )
                    response = _single_response(
                        main_count=count,
                        handheld_main=target,
                    )
                    parser_kwargs = {
                        "mode": "main",
                        "product_id": "p2d-single",
                        "requirements": requirements,
                        "angle_inventory": _angle_inventory(),
                        "upstream_paths": {
                            "product_identity_archive": root / "identity.json",
                            "style_master": single_style_path,
                            "angle_inventory": root / "angle.json",
                        },
                    }
                    direct_artifact = parse_variable_config_response(
                        json.dumps(response, ensure_ascii=False),
                        **parser_kwargs,
                    )
                    parsed_chunks = [
                        _parse_single_chunk(chunk, requirements=requirements)
                        for chunk in _main_chunks(
                            response,
                            requirements=requirements,
                            is_set=False,
                        )
                    ]
                    assembled = assemble_main_variable_config_chunks(
                        parsed_chunks,
                        requirements=requirements,
                        is_set=False,
                    )
                    chunk_artifact = parse_variable_config_response(
                        json.dumps(assembled, ensure_ascii=False),
                        **parser_kwargs,
                    )
                    self.assertEqual(direct_artifact, chunk_artifact)
                    self.assertEqual(
                        (
                            json.dumps(
                                direct_artifact,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8"),
                        (
                            json.dumps(
                                chunk_artifact,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )
                    if count == 30:
                        direct_path = root / "single_30_direct.json"
                        chunk_path = root / "single_30_chunked.json"
                        write_json_exclusive(direct_path, direct_artifact, "direct single")
                        write_json_exclusive(chunk_path, chunk_artifact, "chunked single")
                        self.assertEqual(direct_path.read_bytes(), chunk_path.read_bytes())

                with self.subTest(batch_type="set", count=count):
                    requirements = set_requirements(
                        main_count=count,
                        handheld_main=target,
                    )
                    response = _set_response(
                        main_count=count,
                        handheld_main=target,
                    )
                    parser_kwargs = self._set_full_parser_kwargs(root, requirements)
                    direct_artifact = parse_set_variable_config_response(
                        json.dumps(response, ensure_ascii=False),
                        **parser_kwargs,
                    )
                    parsed_chunks = [
                        _parse_set_chunk(chunk, requirements=requirements)
                        for chunk in _main_chunks(
                            response,
                            requirements=requirements,
                            is_set=True,
                        )
                    ]
                    assembled = assemble_main_variable_config_chunks(
                        parsed_chunks,
                        requirements=requirements,
                        is_set=True,
                    )
                    chunk_artifact = parse_set_variable_config_response(
                        json.dumps(assembled, ensure_ascii=False),
                        **parser_kwargs,
                    )
                    self.assertEqual(direct_artifact, chunk_artifact)
                    self.assertEqual(
                        (
                            json.dumps(
                                direct_artifact,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8"),
                        (
                            json.dumps(
                                chunk_artifact,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )
                    if count == 30:
                        direct_path = root / "set_30_direct.json"
                        chunk_path = root / "set_30_chunked.json"
                        write_json_exclusive(direct_path, direct_artifact, "direct set")
                        write_json_exclusive(chunk_path, chunk_artifact, "chunked set")
                        self.assertEqual(direct_path.read_bytes(), chunk_path.read_bytes())

    def test_set_all_appear_minimum_is_enforced_only_after_cross_chunk_merge(self) -> None:
        requirements = set_requirements(main_count=3, handheld_main=0)

        def chunked_with_all_appear(
            enabled_all_appear: tuple[str, ...],
        ) -> dict[str, object]:
            response = _set_response(main_count=3, handheld_main=0)
            for config in response["configs"]:
                config["per_image_overrides"]["套装组成调用"] = (
                    "全员出镜，档案件数完整。"
                    if config["config_id"] in enabled_all_appear
                    else "按档案调用全部组成单件。"
                )
            raw_chunks = _main_chunks(
                response,
                requirements=requirements,
                is_set=True,
            )
            parsed_chunks = [
                _parse_set_chunk(chunk, requirements=requirements)
                for chunk in raw_chunks
            ]
            return assemble_main_variable_config_chunks(
                parsed_chunks,
                requirements=requirements,
                is_set=True,
            )

        across_chunks = chunked_with_all_appear(("main_01", "main_03"))
        insufficient = chunked_with_all_appear(("main_03",))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "style_master.json").write_text(
                json.dumps(valid_set_style_master(), ensure_ascii=False),
                encoding="utf-8",
            )
            parser_kwargs = self._set_full_parser_kwargs(root, requirements)
            artifact = parse_set_variable_config_response(
                json.dumps(across_chunks, ensure_ascii=False),
                **parser_kwargs,
            )
            self.assertEqual(3, artifact["config_count"])

            with self.assertRaises(ContentPredicateViolation) as caught:
                parse_set_variable_config_response(
                    json.dumps(insufficient, ensure_ascii=False),
                    **parser_kwargs,
                )
            self.assertEqual("field_content", caught.exception.code)
            self.assertIn("至少 2 项", caught.exception.details.expected)

    def test_single_merge_passes_the_unchanged_full_parser(self) -> None:
        requirements = _requirements(main_count=3, handheld_main=2)
        raw_chunks = _main_chunks(
            _single_response(main_count=3, handheld_main=2),
            requirements=requirements,
            is_set=False,
        )
        parsed_chunks = [
            _parse_single_chunk(chunk, requirements=requirements)
            for chunk in raw_chunks
        ]
        assembled = assemble_main_variable_config_chunks(
            parsed_chunks,
            requirements=requirements,
            is_set=False,
        )
        self.assertEqual(
            {
                "用户要求主图手持数量": 2,
                "实际启用手持数量": 2,
                "未启用手持数量": 1,
                "启用手持配置": ["main_01", "main_03"],
                "是否完全满足用户数量": "是",
            },
            assembled["handheld_count_summary"],
        )
        self.assertNotIn("chunk_index", assembled)
        self.assertNotIn("handheld_chunk_summary", assembled)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            style = copy.deepcopy(VALID_STYLE_MASTER)
            style["product_id"] = "p2d-single"
            style_path = root / "style_master.json"
            style_path.write_text(json.dumps(style, ensure_ascii=False), encoding="utf-8")
            artifact = parse_variable_config_response(
                json.dumps(assembled, ensure_ascii=False),
                mode="main",
                product_id="p2d-single",
                requirements=requirements,
                angle_inventory=_angle_inventory(),
                upstream_paths={
                    "product_identity_archive": root / "identity.json",
                    "style_master": style_path,
                    "angle_inventory": root / "angle.json",
                },
            )
        self.assertEqual("main_variable_config", artifact["artifact_type"])
        self.assertEqual(3, artifact["config_count"])

    def test_set_shortfall_merge_passes_full_parser_but_global_all_appear_stays_global(self) -> None:
        requirements = set_requirements(main_count=2, handheld_main=1)
        response = valid_set_variable_response(
            "main",
            count=2,
            handheld_target=1,
            enabled_ids=(),
        )
        raw_chunks = _main_chunks(response, requirements=requirements, is_set=True)
        parsed_chunks = [
            _parse_set_chunk(chunk, requirements=requirements)
            for chunk in raw_chunks
        ]
        assembled = assemble_main_variable_config_chunks(
            parsed_chunks,
            requirements=requirements,
            is_set=True,
        )
        self.assertEqual(0, assembled["handheld_count_summary"]["实际启用手持数量"])
        self.assertEqual("否", assembled["handheld_count_summary"]["是否完全满足用户数量"])
        self.assertIn("原因", assembled["handheld_count_summary"]["手持启用数量说明"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            style_path = root / "style_master.json"
            style_path.write_text(
                json.dumps(valid_set_style_master(), ensure_ascii=False),
                encoding="utf-8",
            )
            parser_kwargs = {
                "mode": "main",
                "product_id": SET_PRODUCT_ID,
                "requirements": requirements,
                "set_identity": valid_set_identity(),
                "component_identities": (
                    valid_component_identity(1),
                    valid_component_identity(2),
                ),
                "set_angle_layout_inventory": valid_set_layout_inventory(),
                "upstream_paths": {
                    "set_product_identity": root / "set_identity.json",
                    "component_identity_archive_01": root / "component_1.json",
                    "component_identity_archive_02": root / "component_2.json",
                    "style_master": style_path,
                    "set_angle_layout_inventory": root / "layout.json",
                },
            }
            artifact = parse_set_variable_config_response(
                json.dumps(assembled, ensure_ascii=False),
                **parser_kwargs,
            )
            self.assertEqual("main_variable_config", artifact["artifact_type"])

            missing_global_all_appear = copy.deepcopy(assembled)
            for config in missing_global_all_appear["configs"]:
                config["per_image_overrides"]["套装组成调用"] = "按档案调用全部组成单件。"
            with self.assertRaises(ContentPredicateViolation) as caught:
                parse_set_variable_config_response(
                    json.dumps(missing_global_all_appear, ensure_ascii=False),
                    **parser_kwargs,
                )
            self.assertEqual("field_content", caught.exception.code)
            self.assertIn("至少 2 项", caught.exception.details.expected)

    def test_merge_rejects_missing_out_of_order_and_global_handheld_drift(self) -> None:
        requirements = _requirements(main_count=3, handheld_main=2)
        parsed = [
            _parse_single_chunk(chunk, requirements=requirements)
            for chunk in _main_chunks(
                _single_response(main_count=3, handheld_main=2),
                requirements=requirements,
                is_set=False,
            )
        ]
        with self.assertRaisesRegex(ExecutorExecutionError, "分段数量异常"):
            assemble_main_variable_config_chunks(
                parsed[:-1],
                requirements=requirements,
            )
        with self.assertRaisesRegex(ExecutorExecutionError, "分段覆盖异常"):
            assemble_main_variable_config_chunks(
                list(reversed(parsed)),
                requirements=requirements,
            )

        drift = copy.deepcopy(parsed)
        _set_handheld(drift[1]["configs"][0], enabled=False, is_set=False)
        with self.assertRaisesRegex(ExecutorExecutionError, "手持数量异常"):
            assemble_main_variable_config_chunks(
                drift,
                requirements=requirements,
            )


_MAIN_CHUNK_INDEX_PATTERN = re.compile(r"本轮只返回第 (\d+)/(\d+) 段")


def _prompt_chunk_index(prompt: str) -> int:
    match = _MAIN_CHUNK_INDEX_PATTERN.search(prompt)
    if match is None:
        raise AssertionError("main prompt did not expose its chunk identity")
    return int(match.group(1))


def _synthetic_main_chunk(chunk_index: int, chunk_count: int) -> dict[str, object]:
    return {
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "configs": [],
    }


def _run_main_executor(
    *,
    requirements: UserConfirmedRequirements,
    transport: object,
    parse_chunk: object,
    progress_events: list[str] | None = None,
    correction_events: list[int] | None = None,
    content_correction_callback: Callable[[int, str, str], None] | None = None,
):
    context = ExecutorContext(manifest={"batch_type": "single"})
    executor = CodexDevExecutor(context, transport=transport, repository_root=ROOT)
    if progress_events is not None:
        executor.set_turn_progress_callback(lambda: progress_events.append("turn"))
    if content_correction_callback is not None:
        executor.set_content_correction_callback(content_correction_callback)
    elif correction_events is not None:
        executor.set_content_correction_callback(
            lambda attempt, _code, _message: correction_events.append(attempt)
        )
    with tempfile.TemporaryDirectory() as temporary:
        output_path = Path(temporary) / "main_variable_configs.json"
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
                return_value="P2D_COMPLETE_MAIN_BASE_PROMPT",
            ),
            mock.patch.object(
                executor_module,
                "parse_main_variable_config_chunk",
                new=parse_chunk,
            ),
            mock.patch.object(
                executor_module,
                "assemble_main_variable_config_chunks",
                side_effect=lambda chunks, **_kwargs: {"chunks": chunks},
            ),
            mock.patch.object(
                executor_module,
                "parse_variable_config_response",
                return_value={"artifact_type": "main_variable_config"},
            ),
            mock.patch.object(executor_module, "write_json_exclusive"),
        ):
            return executor._execute_main_variable_config("p2d-product")


class _MainOverlapTransport:
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
        self.started: set[int] = set()
        self.completion_order: list[int] = []
        self.timeouts: dict[int, float] = {}

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        chunk_index = _prompt_chunk_index(prompt)
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.started.add(chunk_index)
            self.timeouts[chunk_index] = turn_timeout
            if self.active >= self.gate_size:
                self.release.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("parallel main workers did not overlap")
        if self.reverse_completion and chunk_index <= self.gate_size:
            if chunk_index < self.gate_size and not self.completion_events[
                chunk_index + 1
            ].wait(timeout=2.0):
                raise AssertionError("reverse main completion chain did not advance")
        else:
            time.sleep(0.01)
        with self.lock:
            self.completion_order.append(chunk_index)
            self.active -= 1
        if self.reverse_completion and chunk_index <= self.gate_size:
            self.completion_events[chunk_index].set()
        return CodexTurnResult(
            text=f"ok-{chunk_index}",
            thread_id=f"main-thread-{chunk_index}",
        )

    def continue_turn(self, *_args, **_kwargs) -> CodexTurnResult:
        raise AssertionError("valid main overlap responses must not continue")


class _MainRoutedSequenceTransport:
    def __init__(self, plans: dict[int, list[CodexTurnResult]]) -> None:
        self.plans = {chunk_index: list(values) for chunk_index, values in plans.items()}
        self.lock = threading.Lock()
        self.run_counts: dict[int, int] = {}
        self.continue_counts: dict[int, int] = {}

    def _take(self, chunk_index: int) -> CodexTurnResult:
        with self.lock:
            values = self.plans[chunk_index]
            if not values:
                raise AssertionError(f"missing main response for chunk {chunk_index}")
            return values.pop(0)

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
        *,
        turn_timeout: float = 600.0,
    ) -> CodexTurnResult:
        if turn_timeout != 600.0:
            raise AssertionError("main_vc must keep the default timeout")
        chunk_index = _prompt_chunk_index(prompt)
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
            raise AssertionError("main_vc corrections must keep the default timeout")
        chunk_index = _prompt_chunk_index(prompt)
        if thread_id != f"main-thread-{chunk_index}":
            raise AssertionError("main continuation was routed to another chunk")
        with self.lock:
            self.continue_counts[chunk_index] = (
                self.continue_counts.get(chunk_index, 0) + 1
            )
        return self._take(chunk_index)


class _MainConcurrentCorrectionTransport:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0

    def run_turn(
        self,
        prompt: str,
        _attachments: tuple[object, ...],
    ) -> CodexTurnResult:
        chunk_index = _prompt_chunk_index(prompt)
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        self.barrier.wait(timeout=2.0)
        time.sleep(0.02)
        with self.lock:
            self.active -= 1
        return CodexTurnResult(
            text=f"bad-{chunk_index}",
            thread_id=f"main-thread-{chunk_index}",
        )

    def continue_turn(
        self,
        thread_id: str,
        prompt: str,
        _attachments: tuple[object, ...],
    ) -> CodexTurnResult:
        chunk_index = _prompt_chunk_index(prompt)
        if thread_id != f"main-thread-{chunk_index}":
            raise AssertionError("main continuation was routed to another chunk")
        return CodexTurnResult(
            text=f"ok-{chunk_index}",
            thread_id=thread_id,
        )


class MainChunkParallelExecutorTests(unittest.TestCase):
    @staticmethod
    def _successful_parser(
        _text: str,
        chunk_index: int,
        *,
        requirements: UserConfirmedRequirements,
        **_kwargs,
    ) -> dict[str, object]:
        return _synthetic_main_chunk(
            chunk_index,
            len(pair_config_ids("main", requirements.main_image_count or 6)),
        )

    def test_counts_one_and_two_each_run_exactly_one_chunk_chain(self) -> None:
        for count in (1, 2):
            with self.subTest(count=count):
                transport = _MainRoutedSequenceTransport(
                    {
                        1: [
                            CodexTurnResult(
                                text="ok-1",
                                thread_id="main-thread-1",
                            )
                        ]
                    }
                )
                result = _run_main_executor(
                    requirements=_requirements(main_count=count, handheld_main=0),
                    transport=transport,
                    parse_chunk=self._successful_parser,
                )

                self.assertEqual({1: 1}, transport.run_counts)
                self.assertEqual(("main-thread-1",), result.metadata["thread_ids"])
                self.assertNotIn("thread_id", result.metadata)

    def test_reverse_completion_reassembles_chunks_and_thread_ids_by_chunk(self) -> None:
        transport = _MainOverlapTransport(gate_size=2, reverse_completion=True)

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            self.assertEqual(f"ok-{chunk_index}", text)
            return _synthetic_main_chunk(
                chunk_index,
                len(pair_config_ids("main", requirements.main_image_count or 3)),
            )

        result = _run_main_executor(
            requirements=_requirements(main_count=3, handheld_main=0),
            transport=transport,
            parse_chunk=parse_chunk,
        )

        self.assertEqual([2, 1], transport.completion_order)
        self.assertEqual(
            ("main-thread-1", "main-thread-2"),
            result.metadata["thread_ids"],
        )
        self.assertNotIn("thread_id", result.metadata)
        self.assertEqual({600.0}, set(transport.timeouts.values()))

    def test_thirty_configs_make_fifteen_chunks_and_never_exceed_four_workers(self) -> None:
        transport = _MainOverlapTransport(gate_size=4)
        result = _run_main_executor(
            requirements=_requirements(main_count=30, handheld_main=0),
            transport=transport,
            parse_chunk=self._successful_parser,
        )

        self.assertEqual(set(range(1, 16)), transport.started)
        self.assertEqual(15, len(result.metadata["thread_ids"]))
        self.assertEqual(4, transport.peak_active)
        self.assertLessEqual(transport.peak_active, DETAIL_CHUNK_MAX_CONCURRENCY)

    def test_all_chunks_drain_before_the_lowest_failed_chunk_is_reported(self) -> None:
        transport = _MainRoutedSequenceTransport(
            {
                chunk_index: [
                    CodexTurnResult(
                        text=f"ok-{chunk_index}",
                        thread_id=f"main-thread-{chunk_index}",
                    )
                ]
                for chunk_index in (1, 2, 3, 4, 5)
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
            if chunk_index == 1:
                raise ExecutorExecutionError("main-chunk-1-failure")
            if chunk_index == 3:
                time.sleep(0.02)
            if chunk_index == 4:
                raise ExecutorExecutionError("main-chunk-4-failure")
            result = _synthetic_main_chunk(
                chunk_index,
                len(pair_config_ids("main", requirements.main_image_count or 8)),
            )
            with lock:
                succeeded.append(chunk_index)
            return result

        with self.assertRaises(ExecutorExecutionError) as caught:
            _run_main_executor(
                requirements=_requirements(main_count=10, handheld_main=0),
                transport=transport,
                parse_chunk=parse_chunk,
            )

        self.assertEqual("main-chunk-1-failure", str(caught.exception))
        self.assertEqual({1, 2, 3, 4, 5}, set(attempted))
        self.assertIn(5, succeeded)

    def test_transport_recovery_budget_is_independent_per_chunk(self) -> None:
        transport = _MainRoutedSequenceTransport(
            {
                1: [
                    CodexTurnResult(text="bad-1a", thread_id="main-thread-1"),
                    CodexTurnResult(text="bad-1b", thread_id="main-thread-1"),
                    CodexTurnResult(text="ok-1", thread_id="main-thread-1"),
                ],
                2: [
                    CodexTurnResult(text="bad-2a", thread_id="main-thread-2"),
                    CodexTurnResult(text="ok-2", thread_id="main-thread-2"),
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
                raise MainChunkTransportCorruption("truncated")
            return _synthetic_main_chunk(
                chunk_index,
                len(pair_config_ids("main", requirements.main_image_count or 4)),
            )

        result = _run_main_executor(
            requirements=_requirements(main_count=4, handheld_main=0),
            transport=transport,
            parse_chunk=parse_chunk,
        )

        self.assertEqual({1: 1, 2: 1}, transport.run_counts)
        self.assertEqual({1: 2, 2: 1}, transport.continue_counts)
        self.assertEqual(3, result.metadata["recovery_attempts"])

    def test_structure_and_content_correction_budgets_are_independent_per_chunk(self) -> None:
        for correction_kind in ("structure", "content"):
            with self.subTest(correction_kind=correction_kind):
                transport = _MainRoutedSequenceTransport(
                    {
                        chunk_index: [
                            CodexTurnResult(
                                text=f"bad-{chunk_index}",
                                thread_id=f"main-thread-{chunk_index}",
                            ),
                            CodexTurnResult(
                                text=f"ok-{chunk_index}",
                                thread_id=f"main-thread-{chunk_index}",
                            ),
                        ]
                        for chunk_index in (1, 2)
                    }
                )
                correction_events: list[int] = []

                def parse_chunk(
                    text: str,
                    chunk_index: int,
                    *,
                    requirements: UserConfirmedRequirements,
                    **_kwargs,
                ) -> dict[str, object]:
                    if text.startswith("bad"):
                        if correction_kind == "structure":
                            raise MainChunkEnvelopeCorrection(f"fingerprint-{chunk_index}")
                        raise ContentPredicateViolation(
                            "content violation",
                            code="field_content",
                            config_id=f"main_{chunk_index:02d}",
                            field="测试字段",
                            expected="必须纠正",
                        )
                    return {
                        **_synthetic_main_chunk(
                            chunk_index,
                            len(pair_config_ids("main", requirements.main_image_count or 4)),
                        ),
                        "fingerprint": f"fingerprint-{chunk_index}",
                    }

                with mock.patch.object(
                    executor_module,
                    "main_chunk_business_fingerprint",
                    side_effect=lambda chunk, _index: chunk["fingerprint"],
                ):
                    result = _run_main_executor(
                        requirements=_requirements(main_count=4, handheld_main=0),
                        transport=transport,
                        parse_chunk=parse_chunk,
                        correction_events=(
                            correction_events if correction_kind == "content" else None
                        ),
                    )

                self.assertEqual({1: 1, 2: 1}, transport.continue_counts)
                if correction_kind == "structure":
                    self.assertEqual(2, result.metadata["structure_correction_attempts"])
                else:
                    self.assertEqual([1, 2], sorted(correction_events))

    def test_concurrent_chunks_serialize_only_content_correction_callbacks(self) -> None:
        transport = _MainConcurrentCorrectionTransport()
        callback_lock = threading.Lock()
        callback_active = 0
        callback_peak_active = 0
        callback_events: list[tuple[int, str, str]] = []

        def record_correction(chunk_index: int, code: str, config_id: str) -> None:
            nonlocal callback_active, callback_peak_active
            with callback_lock:
                callback_active += 1
                callback_peak_active = max(callback_peak_active, callback_active)
            time.sleep(0.03)
            with callback_lock:
                callback_events.append((chunk_index, code, config_id))
                callback_active -= 1

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            if text.startswith("bad"):
                raise ContentPredicateViolation(
                    "content violation",
                    code="field_content",
                    config_id=f"main_{chunk_index:02d}",
                    field="测试字段",
                    expected="必须纠正",
                )
            return _synthetic_main_chunk(
                chunk_index,
                len(pair_config_ids("main", requirements.main_image_count or 4)),
            )

        _run_main_executor(
            requirements=_requirements(main_count=4, handheld_main=0),
            transport=transport,
            parse_chunk=parse_chunk,
            content_correction_callback=record_correction,
        )

        self.assertEqual(2, transport.peak_active)
        self.assertEqual(1, callback_peak_active)
        self.assertEqual(
            [
                (1, "field_content", "main_01"),
                (2, "field_content", "main_02"),
            ],
            sorted(callback_events),
        )

    def test_structure_correction_cannot_rewrite_the_chunk_summary_fingerprint(self) -> None:
        transport = _MainRoutedSequenceTransport(
            {
                1: [
                    CodexTurnResult(text="bad-1", thread_id="main-thread-1"),
                    CodexTurnResult(text="changed-1", thread_id="main-thread-1"),
                ],
                2: [CodexTurnResult(text="ok-2", thread_id="main-thread-2")],
            }
        )

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            if text == "bad-1":
                raise MainChunkEnvelopeCorrection("original-summary")
            return {
                **_synthetic_main_chunk(
                    chunk_index,
                    len(pair_config_ids("main", requirements.main_image_count or 4)),
                ),
                "fingerprint": (
                    "changed-summary" if text == "changed-1" else "stable-summary"
                ),
            }

        with (
            mock.patch.object(
                executor_module,
                "main_chunk_business_fingerprint",
                side_effect=lambda chunk, _index: chunk["fingerprint"],
            ),
            self.assertRaisesRegex(ExecutorExecutionError, "改变了业务内容"),
        ):
            _run_main_executor(
                requirements=_requirements(main_count=4, handheld_main=0),
                transport=transport,
                parse_chunk=parse_chunk,
            )

    def test_every_successful_initial_and_recovery_turn_emits_heartbeat(self) -> None:
        transport = _MainRoutedSequenceTransport(
            {
                1: [
                    CodexTurnResult(text="bad-1a", thread_id="main-thread-1"),
                    CodexTurnResult(text="bad-1b", thread_id="main-thread-1"),
                    CodexTurnResult(text="ok-1", thread_id="main-thread-1"),
                ],
                2: [
                    CodexTurnResult(text="bad-2", thread_id="main-thread-2"),
                    CodexTurnResult(text="ok-2", thread_id="main-thread-2"),
                ],
            }
        )
        progress_events: list[str] = []

        def parse_chunk(
            text: str,
            chunk_index: int,
            *,
            requirements: UserConfirmedRequirements,
            **_kwargs,
        ) -> dict[str, object]:
            if text.startswith("bad"):
                raise MainChunkTransportCorruption("truncated")
            return _synthetic_main_chunk(
                chunk_index,
                len(pair_config_ids("main", requirements.main_image_count or 4)),
            )

        _run_main_executor(
            requirements=_requirements(main_count=4, handheld_main=0),
            transport=transport,
            parse_chunk=parse_chunk,
            progress_events=progress_events,
        )

        self.assertEqual(5, len(progress_events))

    def test_failure_priority_is_independent_from_submission_order(self) -> None:
        attempted: list[str] = []
        lock = threading.Lock()

        def fail(key: str) -> None:
            with lock:
                attempted.append(key)
            raise ExecutorExecutionError(f"{key}-failure")

        with self.assertRaisesRegex(ExecutorExecutionError, "main-failure"):
            executor_module._run_parallel_chains(
                (
                    ("detail", lambda: fail("detail")),
                    ("main", lambda: fail("main")),
                ),
                failure_priority=("main", "detail"),
            )
        self.assertEqual({"main", "detail"}, set(attempted))


class MainChunkExecutorArtifactEquivalenceTests(
    CodexDevFixture,
    SetVariableConfigFixture,
):
    def test_single_executor_bytes_equal_the_direct_full_response_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, output_path = self.make_downstream_fixture(root)
            requirements = executor_module.parse_user_confirmed_requirements(
                context.manifest,
                root,
            )
            response = _single_response(
                main_count=requirements.main_image_count or 6,
                handheld_main=requirements.handheld_main,
            )
            chunks = _main_chunks(
                response,
                requirements=requirements,
                is_set=False,
            )
            transport = FakeTransport(
                tuple(
                    CodexTurnResult(
                        text=json.dumps(chunk, ensure_ascii=False),
                        thread_id=f"p2d-main-chunk-{chunk['chunk_index']}",
                    )
                    for chunk in chunks
                )
            )

            result = CodexDevExecutor(
                context,
                transport=transport,
                repository_root=root,
            ).execute(executor_module.ExecutionRequest(step="main_vc"))

            identity_path = (
                Path(context.manifest["artifacts"]["product_identity_archive"])
                / "product_identity_archive.json"
            )
            style_path = (
                Path(context.manifest["artifacts"]["style_master"])
                / "style_master.json"
            )
            angle_path = (
                Path(context.manifest["artifacts"]["angle_inventory"])
                / "angle_inventory.json"
            )
            direct_artifact = parse_variable_config_response(
                json.dumps(response, ensure_ascii=False),
                mode="main",
                product_id="p1",
                requirements=requirements,
                angle_inventory=json.loads(angle_path.read_text(encoding="utf-8")),
                upstream_paths={
                    "product_identity_archive": identity_path,
                    "style_master": style_path,
                    "angle_inventory": angle_path,
                },
            )
            expected_bytes = (
                json.dumps(
                    direct_artifact,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

            self.assertEqual(expected_bytes, output_path.read_bytes())
            self.assertEqual(
                tuple(f"p2d-main-chunk-{index}" for index in range(1, 4)),
                result.metadata["thread_ids"],
            )
            self.assertNotIn("thread_id", result.metadata)

    def test_set_executor_bytes_equal_the_direct_full_response_path(self) -> None:
        response = _set_response(main_count=2, handheld_main=1)
        requirements = set_requirements(main_count=2, handheld_main=1)
        chunks = _main_chunks(
            response,
            requirements=requirements,
            is_set=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executor, _transport, manifest, paths = self.make_executor(root, chunks)

            result = executor.execute(executor_module.ExecutionRequest(step="main_vc"))
            output_path = paths["main"] / "main_variable_configs.json"
            upstream_paths = {
                "set_product_identity": paths["identity"] / "set_product_identity.json",
                "component_identity_archive_01": (
                    paths["identity"] / "component_01_product_identity_archive.json"
                ),
                "component_identity_archive_02": (
                    paths["identity"] / "component_02_product_identity_archive.json"
                ),
                "style_master": paths["style"] / "style_master.json",
                "set_angle_layout_inventory": (
                    paths["layout"] / "set_angle_layout_inventory.json"
                ),
            }
            direct_artifact = parse_set_variable_config_response(
                json.dumps(response, ensure_ascii=False),
                mode="main",
                product_id=SET_PRODUCT_ID,
                requirements=requirements,
                set_identity=valid_set_identity(),
                component_identities=(
                    valid_component_identity(1),
                    valid_component_identity(2),
                ),
                set_angle_layout_inventory=valid_set_layout_inventory(),
                upstream_paths=upstream_paths,
            )
            expected_bytes = (
                json.dumps(
                    direct_artifact,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

            self.assertEqual(expected_bytes, output_path.read_bytes())
            self.assertEqual(("st03b-main-chunk-1",), result.metadata["thread_ids"])
            self.assertNotIn("thread_id", result.metadata)


if __name__ == "__main__":
    unittest.main()
