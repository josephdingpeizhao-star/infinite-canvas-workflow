from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from image_count_contract import (  # noqa: E402
    chinese_image_count,
    config_ids,
    detail_module_groups,
    expected_config_ids,
    pair_config_ids,
)


class ChineseImageCountTests(unittest.TestCase):
    def test_all_supported_values_have_one_canonical_rendering(self) -> None:
        expected = (
            "一",
            "二",
            "三",
            "四",
            "五",
            "六",
            "七",
            "八",
            "九",
            "十",
            "十一",
            "十二",
            "十三",
            "十四",
            "十五",
            "十六",
            "十七",
            "十八",
            "十九",
            "二十",
            "二十一",
            "二十二",
            "二十三",
            "二十四",
            "二十五",
            "二十六",
            "二十七",
            "二十八",
            "二十九",
            "三十",
        )
        self.assertEqual(expected, tuple(chinese_image_count(value) for value in range(1, 31)))

    def test_renderer_rejects_values_outside_its_declared_domain(self) -> None:
        for value in (0, 31, -1, 1.5, "6", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    chinese_image_count(value)  # type: ignore[arg-type]


class ConfigIdTests(unittest.TestCase):
    def test_single_generator_produces_zero_padded_ordered_ids(self) -> None:
        self.assertEqual(("main_01", "main_02", "main_03"), config_ids("main", 3))
        self.assertEqual(("detail_01", "detail_02"), config_ids("detail", 2))
        self.assertEqual("main_30", config_ids("main", 30)[-1])
        self.assertEqual("detail_30", config_ids("detail", 30)[-1])
        self.assertEqual(
            ("main_01", "main_02", "main_03", "detail_01", "detail_02"),
            expected_config_ids(3, 2),
        )

    def test_generator_rejects_bad_mode_type_and_bounds(self) -> None:
        for mode, count in (
            ("main", 0),
            ("detail", 31),
            ("other", 1),
            ("main", -1),
            ("main", 1.5),
            ("main", "6"),
            ("main", True),
        ):
            with self.subTest(mode=mode, count=count):
                with self.assertRaises(ValueError):
                    config_ids(mode, count)  # type: ignore[arg-type]

    def test_pairing_uses_the_same_generated_sequence_and_allows_a_final_single(self) -> None:
        self.assertEqual(
            (("detail_01", "detail_02"), ("detail_03",)),
            pair_config_ids("detail", 3),
        )
        self.assertEqual(15, len(pair_config_ids("detail", 30)))


class DetailModulePlanTests(unittest.TestCase):
    def test_approved_module_mapping_examples(self) -> None:
        self.assertEqual(((1, 2, 3, 4, 6, 7, 8),), detail_module_groups(1))
        self.assertEqual(
            ((1,), (2,), (3,), (4, 5, 6, 7), (8,)),
            detail_module_groups(5),
        )
        self.assertEqual(
            ((1,), (2,), (3,), (4,), (5,), (6, 7), (8,)),
            detail_module_groups(7),
        )
        self.assertEqual(tuple((value,) for value in range(1, 9)), detail_module_groups(8))
        self.assertEqual(
            tuple((value,) for value in range(1, 9)) + ((1,),),
            detail_module_groups(9),
        )
        plan_30 = detail_module_groups(30)
        self.assertEqual(30, len(plan_30))
        self.assertEqual(1, sum(1 for group in plan_30 if 5 in group))
        self.assertEqual(
            (1, 2, 3, 4, 6, 7, 8, 1, 2, 3, 4, 6, 7, 8),
            tuple(group[0] for group in plan_30[8:22]),
        )


if __name__ == "__main__":
    unittest.main()
