from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import (  # noqa: E402
    CategoryRecipeError,
    load_category_recipe,
)
from codex_dev_downstream import _reject_unsupported_claims  # noqa: E402
from executor_contract import ExecutorExecutionError  # noqa: E402


_EXPECTED_DIMENSION_LABEL_TERMS = {
    "杯类": {
        "length_cm": ["长度", "长"],
        "width_cm": ["宽度", "宽"],
        "height_cm": ["高度", "高"],
    },
    "盘子": {
        "length_cm": ["长度", "长"],
        "width_cm": ["宽度", "宽"],
        "height_cm": ["高度", "高"],
    },
    "碗": {
        "length_cm": ["口径"],
        "width_cm": ["宽度", "宽"],
        "height_cm": ["高度", "高"],
    },
}
_BOWL_CONFIRMED_DIMENSIONS = {
    "length_cm": 18,
    "height_cm": 8,
}
_BOWL_DIMENSION_TEACHING_VALUE = {
    "prompts": [
        {
            "final_prompt": "产品口径约 18 厘米、高度约 8 厘米。"
        }
    ]
}


def _write_lexicons(path: Path, lexicons: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(lexicons, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class Cat05DimensionLabelTermsTest(unittest.TestCase):
    def test_installed_category_declarations_are_exact(self) -> None:
        for category_key, expected in _EXPECTED_DIMENSION_LABEL_TERMS.items():
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                self.assertEqual(
                    expected,
                    recipe.lexicons["dimension_label_terms"],
                )

    def test_recipe_loader_rejects_invalid_dimension_label_terms(self) -> None:
        valid_lexicons = json.loads(
            (ROOT / "categories" / "碗" / "lexicons.json").read_text(
                encoding="utf-8"
            )
        )
        invalid_cases: dict[str, tuple[dict[str, Any], str]] = {}

        missing_declaration = copy.deepcopy(valid_lexicons)
        missing_declaration.pop("dimension_label_terms")
        invalid_cases["missing declaration"] = (
            missing_declaration,
            "品类词表结构无效",
        )

        non_mapping_declaration = copy.deepcopy(valid_lexicons)
        non_mapping_declaration["dimension_label_terms"] = []
        invalid_cases["non-mapping declaration"] = (
            non_mapping_declaration,
            "品类尺寸标签词表无效",
        )

        missing_dimension_key = copy.deepcopy(valid_lexicons)
        missing_dimension_key["dimension_label_terms"].pop("width_cm")
        invalid_cases["missing width key"] = (
            missing_dimension_key,
            "品类尺寸标签词表无效",
        )

        extra_dimension_key = copy.deepcopy(valid_lexicons)
        extra_dimension_key["dimension_label_terms"]["depth_cm"] = ["深度"]
        invalid_cases["extra key"] = (
            extra_dimension_key,
            "品类尺寸标签词表无效",
        )

        empty_labels = copy.deepcopy(valid_lexicons)
        empty_labels["dimension_label_terms"]["length_cm"] = []
        invalid_cases["empty list"] = (
            empty_labels,
            "品类尺寸标签词表无效",
        )

        non_string_label = copy.deepcopy(valid_lexicons)
        non_string_label["dimension_label_terms"]["length_cm"] = ["口径", 18]
        invalid_cases["non-string label"] = (
            non_string_label,
            "品类尺寸标签词表无效",
        )

        duplicate_within_dimension = copy.deepcopy(valid_lexicons)
        duplicate_within_dimension["dimension_label_terms"]["length_cm"] = [
            "口径",
            "口径",
        ]
        invalid_cases["duplicate within dimension"] = (
            duplicate_within_dimension,
            "品类尺寸标签词表无效",
        )

        duplicate_across_dimensions = copy.deepcopy(valid_lexicons)
        duplicate_across_dimensions["dimension_label_terms"]["length_cm"] = ["长"]
        duplicate_across_dimensions["dimension_label_terms"]["width_cm"] = [
            "宽度",
            "宽",
            "长",
        ]
        invalid_cases["duplicate across dimensions"] = (
            duplicate_across_dimensions,
            "品类尺寸标签词表无效",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(ROOT / "categories", temp_root / "categories")
            target = temp_root / "categories" / "碗" / "lexicons.json"

            for case_name, (lexicons, expected_message) in invalid_cases.items():
                with self.subTest(case=case_name):
                    _write_lexicons(target, lexicons)
                    with self.assertRaisesRegex(
                        CategoryRecipeError,
                        expected_message,
                    ):
                        load_category_recipe(temp_root, "碗")

    def test_runtime_rejects_missing_dimension_label_terms(self) -> None:
        lexicons = copy.deepcopy(dict(load_category_recipe(ROOT, "碗").lexicons))
        lexicons.pop("dimension_label_terms")

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "无法读取有效的",
        ):
            _reject_unsupported_claims(
                _BOWL_DIMENSION_TEACHING_VALUE,
                8,
                "CAT-05 运行时测试",
                product_type="碗",
                lexicons=lexicons,
                confirmed_dimensions=_BOWL_CONFIRMED_DIMENSIONS,
            )

    def test_bowl_diameter_recognition_comes_from_recipe_declaration(self) -> None:
        installed_lexicons = copy.deepcopy(
            dict(load_category_recipe(ROOT, "碗").lexicons)
        )
        without_diameter_label = copy.deepcopy(installed_lexicons)
        without_diameter_label["dimension_label_terms"]["length_cm"] = [
            "长度",
            "长",
        ]

        with self.assertRaisesRegex(
            ExecutorExecutionError,
            "未确认参数",
        ):
            _reject_unsupported_claims(
                _BOWL_DIMENSION_TEACHING_VALUE,
                8,
                "CAT-05 反向测试",
                product_type="碗",
                lexicons=without_diameter_label,
                confirmed_dimensions=_BOWL_CONFIRMED_DIMENSIONS,
            )

        _reject_unsupported_claims(
            _BOWL_DIMENSION_TEACHING_VALUE,
            8,
            "CAT-05 正向测试",
            product_type="碗",
            lexicons=installed_lexicons,
            confirmed_dimensions=_BOWL_CONFIRMED_DIMENSIONS,
        )


if __name__ == "__main__":
    unittest.main()
