from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import installed_category_metadata, load_category_recipe  # noqa: E402


# 这些文字与下游执行器的精确字符串合同保持一致；新品类缺任一项都应直接失败。
FIXED_CONTENT_CONTRACT_TERMS = (
    "手持交互声明",
    "动态手持样式参考图调用",
    "本张图不启用手持场景",
    "启用手持场景",
    "静态握持",
    "动态拿起",
    "无，仅动态拿起场景可调用",
    "未提供，不调用",
    "1:1",
    "3:4",
    "约 {height_cm} 厘米",
    "高度约 {height_cm} 厘米",
    "绑定角度槽位",
    "A/B/C 槽位",
    "标准模块归属",
    "尺寸标注信息",
    "尺寸标注图规则",
    "非尺寸标注图",
    "必须明确禁止容量、宽度、直径、重量、材质",
)


def _runtime_text(recipe, stage: str) -> str:
    package = recipe.runtime_packages[f"{stage}_runtime"]
    return "\n".join(str(item.get("text") or "") for item in package["slices"])


def _stage_text(recipe, stage: str) -> str:
    return "\n".join((recipe.prompts[f"{stage}_prompt"], _runtime_text(recipe, stage)))


class CategoryExecutorContentContractTest(unittest.TestCase):
    def test_every_installed_category_teaches_all_fixed_content_contract_terms(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                archive = json.dumps(
                    {
                        "lexicons": recipe.lexicons,
                        "prompts": recipe.prompts,
                        "runtime": recipe.runtime_packages,
                    },
                    ensure_ascii=False,
                )
                missing = [
                    term for term in FIXED_CONTENT_CONTRACT_TERMS if term not in archive
                ]
                self.assertEqual(
                    [],
                    missing,
                    f"品类“{category_key}”缺少执行器内容合同教学：{missing}",
                )
                for module_number in range(1, 9):
                    module = f"模块{module_number:02d}"
                    self.assertIn(
                        module,
                        archive,
                        f"品类“{category_key}”缺少{module}教学",
                    )

    def test_every_installed_category_teaches_contracts_in_the_consuming_stage(self) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                main = _stage_text(recipe, "main")
                detail = _stage_text(recipe, "detail")
                final = _stage_text(recipe, "final")

                for term in (
                    "1:1",
                    "手持交互声明",
                    "动态手持样式参考图调用",
                    "本张图不启用手持场景",
                    "无，仅动态拿起场景可调用",
                    "未提供，不调用",
                ):
                    self.assertIn(term, main, f"品类“{category_key}”主图阶段缺少：{term}")

                for term in (
                    "3:4",
                    "标准模块归属",
                    "高度约 {height_cm} 厘米",
                    "尺寸标注信息",
                    "尺寸标注图规则",
                    "非尺寸标注图",
                    "手持交互声明",
                    "动态手持样式参考图调用",
                ):
                    self.assertIn(term, detail, f"品类“{category_key}”详情阶段缺少：{term}")

                for term in (
                    "{expected_ratio}",
                    "约 {height_cm} 厘米",
                    "A/B/C 槽位",
                    "手持启用或禁用状态",
                ):
                    self.assertIn(term, final, f"品类“{category_key}”最终阶段缺少：{term}")


if __name__ == "__main__":
    unittest.main()
