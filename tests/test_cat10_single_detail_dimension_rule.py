from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_downstream import (  # noqa: E402
    build_variable_config_prompt,
    parse_user_confirmed_requirements,
    parse_variable_config_response,
)
from content_correction import ContentPredicateViolation  # noqa: E402
from image_count_contract import (  # noqa: E402
    DETAIL_MODULE_LABELS,
    DIMENSION_MODULE,
    _DETAIL_EXTRA_MODULE_CYCLE,
    detail_handheld_limit_message,
    detail_module_assignment_lines,
    detail_module_groups,
    handheld_count_maximum,
)
from tests.test_codex_dev_executor import (  # noqa: E402
    CodexDevFixture,
    valid_detail_variable_response,
)


EXPECTED_NON_DIMENSION_MODULES = (1, 2, 3, 4, 6, 7, 8)


class Cat10SingleDetailDimensionRuleTest(CodexDevFixture):
    def _requirements(self, root: Path, detail_count: int):
        return parse_user_confirmed_requirements(
            {
                "category": "杯类",
                "user_confirmed_facts": {
                    "product_type": "家居盛水水壶",
                    "length_cm": None,
                    "width_cm": None,
                    "height_cm": 25,
                    "main_image_count": 6,
                    "detail_image_count": detail_count,
                    "handheld_main": 2,
                    "handheld_detail": 0 if detail_count == 1 else 1,
                    "allow_clear_water": True,
                    "forbid_pouring_and_heating": True,
                    "missing_d_no_retake": True,
                },
            },
            root,
        )

    def _detail_inputs(self, root: Path):
        _context, _detail_output, main_output = self.make_detail_fixture(root)
        artifacts_root = root / "workspace" / "artifacts"
        upstream_paths = {
            "product_identity_archive": artifacts_root
            / "identity"
            / "product_identity_archive.json",
            "style_master": artifacts_root / "style_master" / "style_master.json",
            "angle_inventory": artifacts_root
            / "angle_inventory"
            / "angle_inventory.json",
            "main_variable_configs": main_output,
        }
        loaded = {
            key: json.loads(path.read_text(encoding="utf-8"))
            for key, path in upstream_paths.items()
        }
        return upstream_paths, loaded

    def _single_detail_response(self, modules: tuple[int, ...]) -> dict[str, object]:
        response = copy.deepcopy(valid_detail_variable_response())
        response["configs"] = response["configs"][:1]  # type: ignore[index]
        config = response["configs"][0]  # type: ignore[index]
        overrides = config["per_image_overrides"]  # type: ignore[index]
        overrides["标准模块归属"] = " + ".join(  # type: ignore[index]
            f"模块{module:02d}" for module in modules
        )
        response["handheld_count_summary"] = {
            "用户要求详情图手持数量": 0,
            "实际启用手持数量": 0,
            "未启用手持数量": 1,
            "启用手持配置": [],
            "是否完全满足用户数量": "是",
        }
        return response

    def test_single_detail_omits_only_the_dimension_module(self) -> None:
        groups = detail_module_groups(1)

        self.assertEqual((EXPECTED_NON_DIMENSION_MODULES,), groups)
        self.assertEqual(tuple(sorted(groups[0])), groups[0])
        self.assertEqual(len(set(groups[0])), len(groups[0]))
        self.assertEqual(7, len(groups[0]))
        self.assertNotIn(DIMENSION_MODULE, groups[0])

    def test_dimension_module_occurs_zero_times_at_one_and_once_at_two_through_thirty(self) -> None:
        self.assertEqual(
            0,
            sum(DIMENSION_MODULE in group for group in detail_module_groups(1)),
        )
        for count in range(2, 31):
            with self.subTest(count=count):
                self.assertEqual(
                    1,
                    sum(
                        DIMENSION_MODULE in group
                        for group in detail_module_groups(count)
                    ),
                )

    def test_extra_module_cycle_remains_the_approved_literal_sequence(self) -> None:
        self.assertEqual(EXPECTED_NON_DIMENSION_MODULES, _DETAIL_EXTRA_MODULE_CYCLE)

    def test_assignment_rendering_changes_only_the_single_detail_plan(self) -> None:
        single_lines = detail_module_assignment_lines(1)

        self.assertEqual(1, len(single_lines))
        self.assertTrue(single_lines[0].startswith("detail_01："))
        self.assertNotIn("模块05", single_lines[0])
        for module in EXPECTED_NON_DIMENSION_MODULES:
            self.assertIn(
                f"模块{module:02d} {DETAIL_MODULE_LABELS[module - 1]}",
                single_lines[0],
            )
        self.assertEqual(
            (
                "detail_01：模块01 首屏 · 主视觉与卖点承接",
                "detail_02：模块02 核心卖点证明",
                "detail_03：模块03 使用场景与方法",
                "detail_04：模块04 细节实拍与材质工艺",
                "detail_05：模块05 规格尺寸与容量",
                "detail_06：模块06 质感可信视觉呈现",
                "detail_07：模块07 决策辅助与场景想象",
                "detail_08：模块08 收尾氛围与风险克制",
            ),
            detail_module_assignment_lines(8),
        )

    def test_detail_prompt_omits_module05_only_for_a_single_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _paths, loaded = self._detail_inputs(root)
            prompts = {
                count: build_variable_config_prompt(
                    mode="detail",
                    product_id="p1",
                    repository_root=root,
                    identity=loaded["product_identity_archive"],
                    style_master=loaded["style_master"],
                    angle_inventory=loaded["angle_inventory"],
                    requirements=self._requirements(root, count),
                    main_variable_config=loaded["main_variable_configs"],
                )
                for count in (1, 2)
            }

        for count, prompt in prompts.items():
            with self.subTest(count=count):
                self.assertIn("【详情页模块覆盖计划】", prompt)
        single_plan = prompts[1].split("【详情页模块覆盖计划】：", 1)[1].split(
            "；输出画布比例", 1
        )[0]
        double_plan = prompts[2].split("【详情页模块覆盖计划】：", 1)[1].split(
            "；输出画布比例", 1
        )[0]
        self.assertNotIn("模块05", single_plan)
        self.assertIn("模块05", double_plan)

    def test_single_detail_module_coverage_is_fail_closed_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            upstream_paths, loaded = self._detail_inputs(root)
            requirements = self._requirements(root, 1)
            invalid = self._single_detail_response(tuple(range(1, 9)))

            with self.assertRaises(ContentPredicateViolation) as caught:
                parse_variable_config_response(
                    json.dumps(invalid, ensure_ascii=False),
                    mode="detail",
                    product_id="p1",
                    requirements=requirements,
                    angle_inventory=loaded["angle_inventory"],
                    upstream_paths=upstream_paths,
                )
            self.assertEqual("module_coverage", caught.exception.code)

            accepted = parse_variable_config_response(
                json.dumps(
                    self._single_detail_response(EXPECTED_NON_DIMENSION_MODULES),
                    ensure_ascii=False,
                ),
                mode="detail",
                product_id="p1",
                requirements=requirements,
                angle_inventory=loaded["angle_inventory"],
                upstream_paths=upstream_paths,
            )

        self.assertEqual(1, accepted["config_count"])
        self.assertEqual(
            "模块01 + 模块02 + 模块03 + 模块04 + 模块06 + 模块07 + 模块08",
            accepted["configs"][0]["per_image_overrides"]["标准模块归属"],
        )

    def test_detail_handheld_limits_and_message_do_not_drift(self) -> None:
        self.assertEqual(0, handheld_count_maximum("detail", 1))
        self.assertEqual(1, handheld_count_maximum("detail", 2))
        self.assertEqual(7, handheld_count_maximum("detail", 8))
        self.assertEqual(29, handheld_count_maximum("detail", 30))
        self.assertEqual(
            "含尺寸标注的详情图位不可手持，详情图手持最多 7 张。",
            detail_handheld_limit_message(8),
        )


if __name__ == "__main__":
    unittest.main()
