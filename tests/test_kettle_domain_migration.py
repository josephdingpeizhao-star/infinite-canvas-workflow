from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


RULE_FILES = [
    "产品身份档案提示词.txt",
    "角度槽位入库表生成与识别提示词.txt",
    "手持适配规则.txt",
    "主图单张变量配置提示词生成.txt",
    "详情图单张变量配置提示词生成.txt",
    "电商图片通用质检清单.txt",
    "真实感约束.txt",
    "道具生成规则模块.txt",
    "套装产品身份档案提示词.txt",
    "套装编排规则.txt",
    "套装变量配置补充模块.txt",
]

OLD_CUP_DEFAULTS = [
    "杯类",
    "杯子",
    "马克杯",
    "咖啡杯",
    "水杯",
    "茶杯",
    "玻璃杯",
    "保温杯",
    "随行杯",
    "带盖杯",
    "吸管杯",
    "杯碟",
    "杯勺",
    "杯口",
    "杯沿",
    "杯身",
    "杯壁",
    "杯柄",
    "杯足",
]

KETTLE_DOMAIN_TERMS = [
    "水壶类",
    "壶身",
    "壶嘴",
    "壶盖",
    "壶柄",
    "提梁",
    "壶底",
    "出水口",
    "壶口",
    "容量感",
    "密封结构",
]


class KettleDomainMigrationTest(unittest.TestCase):
    def test_runtime_rule_files_do_not_default_to_cup_domain(self) -> None:
        for file_name in RULE_FILES:
            with self.subTest(file_name=file_name):
                text = (ROOT / file_name).read_text(encoding="utf-8")
                lingering = [term for term in OLD_CUP_DEFAULTS if term in text]
                self.assertEqual([], lingering)
                self.assertTrue(any(term in text for term in KETTLE_DOMAIN_TERMS))

    def test_validation_scripts_use_kettle_product_markers(self) -> None:
        integrity = importlib.import_module("validate_final_prompt_integrity")
        pre_render = importlib.import_module("pre_render_reference_gate")
        compiler = importlib.import_module("compile_final_prompts")

        old_marker_hits = [
            marker
            for marker in integrity.COMPILER_PRODUCT_MARKERS
            if marker in OLD_CUP_DEFAULTS
        ]
        self.assertEqual([], old_marker_hits)
        self.assertIn("壶身", integrity.COMPILER_PRODUCT_MARKERS)
        self.assertIn("壶嘴", integrity.COMPILER_PRODUCT_MARKERS)
        self.assertIn("壶盖", integrity.COMPILER_PRODUCT_MARKERS)

        risk_terms = {
            term
            for terms in pre_render.RISK_PATTERNS.values()
            for term in terms
        }
        self.assertNotIn("重复杯碟", risk_terms)
        self.assertNotIn("多杯碟", risk_terms)
        self.assertIn("多壶", risk_terms)
        self.assertIn("重复壶身", risk_terms)

        self.assertNotIn("杯口", compiler.COMMON_NEGATIVE_PROMPT)
        self.assertNotIn("杯身", compiler.COMMON_NEGATIVE_PROMPT)
        self.assertIn("壶口", compiler.COMMON_NEGATIVE_PROMPT)
        self.assertIn("壶身", compiler.COMMON_NEGATIVE_PROMPT)


if __name__ == "__main__":
    unittest.main()
