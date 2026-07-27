from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from codex_dev_downstream import (  # noqa: E402
    build_detail_variable_config_chunk_prompt,
    build_variable_config_prompt,
    detail_variable_config_chunk_count,
    parse_user_confirmed_requirements,
)
from tests.test_cat01_cup_golden import (  # noqa: E402
    PRODUCT_ID,
    _angle_inventory,
    _identity,
    _style_master,
)


def requirements(main_count: int, detail_count: int):
    return parse_user_confirmed_requirements(
        {
            "category": "杯类",
            "user_confirmed_facts": {
                "product_type": "水壶",
                "length_cm": None,
                "width_cm": None,
                "height_cm": 25,
                "main_image_count": main_count,
                "detail_image_count": detail_count,
                "handheld_main": 0,
                "handheld_detail": 0,
                "allow_clear_water": True,
                "forbid_pouring_and_heating": True,
                "missing_d_no_retake": True,
            },
        },
        ROOT,
    )


def variable_prompt(mode: str, main_count: int, detail_count: int) -> str:
    confirmed = requirements(main_count, detail_count)
    return build_variable_config_prompt(
        mode=mode,
        product_id=PRODUCT_ID,
        repository_root=ROOT,
        identity=_identity(),
        style_master=_style_master(),
        angle_inventory=_angle_inventory(),
        requirements=confirmed,
        main_variable_config={} if mode == "detail" else None,
    )


class NonDefaultPromptTests(unittest.TestCase):
    def test_three_main_and_two_detail_render_exact_ranges_and_chinese_counts(self) -> None:
        main_prompt = variable_prompt("main", 3, 2)
        detail_prompt = variable_prompt("detail", 3, 2)

        self.assertIn("main_01 至 main_03 三项", main_prompt)
        self.assertIn("detail_01 至 detail_02 二项", detail_prompt)
        self.assertIn(
            "detail_01：模块01 首屏 · 主视觉与卖点承接 + "
            "模块02 核心卖点证明 + 模块03 使用场景与方法 + "
            "模块04 细节实拍与材质工艺 + 模块05 规格尺寸与容量 + "
            "模块06 质感可信视觉呈现 + 模块07 决策辅助与场景想象",
            detail_prompt,
        )
        self.assertIn("detail_02：模块08 收尾氛围与风险克制", detail_prompt)
        self.assertIn(
            "若用户明确限制为 7 套，则启用【旧版 7 套兼容模式】",
            detail_prompt,
        )
        self.assertNotIn("标准完整详情页模式默认 8 套", detail_prompt)

    def test_thirty_each_renders_two_digit_terminal_ids_and_thirty(self) -> None:
        self.assertIn(
            "main_01 至 main_30 三十项",
            variable_prompt("main", 30, 30),
        )
        self.assertIn(
            "detail_01 至 detail_30 三十项",
            variable_prompt("detail", 30, 30),
        )


class DetailChunkTests(unittest.TestCase):
    def test_odd_detail_count_uses_one_item_in_the_last_pair(self) -> None:
        confirmed = requirements(3, 3)
        self.assertEqual(2, detail_variable_config_chunk_count(confirmed))
        prompt = build_detail_variable_config_chunk_prompt(
            "BASE",
            2,
            requirements=confirmed,
        )
        self.assertIn("第 2/2 段", prompt)
        self.assertIn("只包含配置 detail_03", prompt)
        self.assertIn("configs 必须按上述顺序包含一项", prompt)
        self.assertIn("汇总完整三项配置", prompt)

    def test_two_details_stay_in_one_two_item_chunk(self) -> None:
        confirmed = requirements(3, 2)
        self.assertEqual(1, detail_variable_config_chunk_count(confirmed))
        prompt = build_detail_variable_config_chunk_prompt(
            "BASE",
            1,
            requirements=confirmed,
        )
        self.assertIn("第 1/1 段", prompt)
        self.assertIn("detail_01、detail_02", prompt)
        self.assertIn("configs 必须按上述顺序包含二项", prompt)


if __name__ == "__main__":
    unittest.main()
