from __future__ import annotations

import json
import math
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

from category_recipes import installed_category_metadata, load_category_recipe  # noqa: E402
from codex_dev_downstream import (  # noqa: E402
    DETAIL_REQUIRED_OVERRIDE_FIELDS,
    FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS,
    MAIN_REQUIRED_OVERRIDE_FIELDS,
    UserConfirmedRequirements,
    build_final_prompt_batch_prompt,
    stable_json_sha256,
)


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

# 这些信号不代替字段契约，只用于判断教学正文是否具有可执行边界。
# 候选正文还必须达到杯类同字段蓝本的长度比例；仅写“字段名：请填写”会失败。
SUBSTANTIVE_CONTRACT_SIGNALS = (
    "必须",
    "不得",
    "只能",
    "禁止",
    "固定写入",
    "固定包含",
    "唯一",
    "如果",
    "若",
    "冲突",
    "依据",
    "角度",
    "颜色",
    "尺寸",
    "道具",
    "背景",
    "文字",
    "手持",
    "风格",
    "真实",
    "遮挡",
    "参考图",
    "产品身份",
    "页面任务",
    "主体",
    "信息",
)
FORCE_OR_BOUNDARY_SIGNALS = (
    "必须",
    "不得",
    "只能",
    "禁止",
    "固定写入",
    "固定包含",
)
PLACEHOLDER_TEACHING_PATTERNS = (
    "请填写",
    "按要求执行",
    "同上",
    "略",
    "待补充",
)

# 源码内联位置：canvas-bridge/codex_dev_downstream.py:2421。
FINAL_BATCH_TOP_LEVEL_FIELDS = frozenset({"prompts"})

# 最终单项字段在源码第 2456 行仍是内联集合；字段名不再抄写，
# 直接取第 100-104 行的 FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS。
FINAL_PROMPT_ITEM_FIELDS = frozenset(FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS)

# 源码内联精确值位置：
# - 画布比例：1927、2120-2122、2452-2484；
# - 手持声明与动态参考：1932-1943、2125-2138；
# - 详情模块：1945-1953、2139-2147。
INLINE_EXACT_FIELD_TERMS = {
    "main": {
        "输出画布比例": ("1:1",),
        "手持交互声明": (
            "本张图不启用手持场景",
            "启用手持场景",
            "静态握持",
            "动态拿起",
        ),
        "动态手持样式参考图调用": (
            "无",
            "无，仅动态拿起场景可调用",
            "未提供，不调用",
        ),
    },
    "detail": {
        "标准模块归属": tuple(f"模块{number:02d}" for number in range(1, 9)),
        "输出画布比例": ("3:4",),
        "手持交互声明": (
            "本张图不启用手持场景",
            "启用手持场景",
            "静态握持",
            "动态拿起",
        ),
        "动态手持样式参考图调用": (
            "无",
            "无，仅动态拿起场景可调用",
            "未提供，不调用",
        ),
    },
}


def _runtime_text(recipe, stage: str) -> str:
    package = recipe.runtime_packages[f"{stage}_runtime"]
    return "\n".join(str(item.get("text") or "") for item in package["slices"])


def _stage_text(recipe, stage: str) -> str:
    return "\n".join((recipe.prompts[f"{stage}_prompt"], _runtime_text(recipe, stage)))


def _normalized_contract_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _field_teaching_evidence(recipe, stage: str, field: str) -> str:
    fields = (
        MAIN_REQUIRED_OVERRIDE_FIELDS
        if stage == "main"
        else DETAIL_REQUIRED_OVERRIDE_FIELDS
    )
    required_headings = {f"【{item}】" for item in fields}
    heading = f"【{field}】"
    candidates: list[str] = []
    slices = recipe.runtime_packages[f"{stage}_runtime"]["slices"]
    for rule_slice in slices:
        text = str(rule_slice.get("text") or "")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != heading:
                continue
            body: list[str] = []
            for following in lines[index + 1 :]:
                if following.strip() in required_headings:
                    break
                body.append(following)
            candidates.append("\n".join(body).strip())

    if not candidates:
        for rule_slice in slices:
            for line in str(rule_slice.get("text") or "").splitlines():
                if field in line:
                    candidates.append(line.strip())

    return max(candidates, key=lambda item: len(_normalized_contract_text(item)), default="")


def _substantive_teaching_problems(blueprint: str, candidate: str) -> list[str]:
    normalized_blueprint = _normalized_contract_text(blueprint)
    normalized_candidate = _normalized_contract_text(candidate)
    problems: list[str] = []
    minimum_length = max(24, math.ceil(len(normalized_blueprint) * 0.6))
    if len(normalized_candidate) < minimum_length:
        problems.append(
            f"正文过短：{len(normalized_candidate)} < 蓝本下限 {minimum_length}"
        )

    blueprint_signals = {
        signal for signal in SUBSTANTIVE_CONTRACT_SIGNALS if signal in blueprint
    }
    candidate_signals = {
        signal for signal in SUBSTANTIVE_CONTRACT_SIGNALS if signal in candidate
    }
    minimum_signals = math.ceil(len(blueprint_signals) * 0.55)
    if len(candidate_signals) < minimum_signals:
        problems.append(
            f"约束信号不足：{len(candidate_signals)} < 蓝本下限 {minimum_signals}"
        )

    if any(signal in blueprint for signal in FORCE_OR_BOUNDARY_SIGNALS) and not any(
        signal in candidate for signal in FORCE_OR_BOUNDARY_SIGNALS
    ):
        problems.append("缺少必须/不得/只能/禁止等执行边界")
    for fixed_marker in ("固定写入", "固定包含"):
        if fixed_marker in blueprint and fixed_marker not in candidate:
            problems.append(f"缺少蓝本标记：{fixed_marker}")
    if any(pattern in candidate for pattern in PLACEHOLDER_TEACHING_PATTERNS) and (
        len(normalized_candidate) < minimum_length * 2
    ):
        problems.append("疑似占位式教学")
    return problems


def _requirements_for_category(recipe) -> UserConfirmedRequirements:
    required_dimensions = set(recipe.form["dimensions"]["required"])
    return UserConfirmedRequirements(
        product_type=recipe.product_noun,
        length_cm=18 if "length_cm" in required_dimensions else None,
        width_cm=16 if "width_cm" in required_dimensions else None,
        height_cm=8,
        main_image_count=1,
        detail_image_count=1,
        handheld_main=0,
        handheld_detail=0,
        allow_clear_water=False,
        forbid_pouring_and_heating=True,
        missing_d_no_retake=True,
        category=recipe.key,
        recipe=recipe,
    )


def _final_stage_prompt(recipe) -> str:
    product_id = f"{recipe.key}_字段合同测试"
    requirements = _requirements_for_category(recipe)
    common = {"已确认高度": "约 8 厘米"}
    overrides = {
        "绑定角度槽位": "A 槽位，唯一绑定源图 img_001",
        "手持交互声明": "本张图不启用手持场景",
    }
    resolved = dict(common)
    resolved.update(overrides)
    variable_config = {
        "product_id": product_id,
        "artifact_type": "main_variable_config",
        "config_count": 1,
        "common_constraints": common,
        "configs": [
            {
                "config_id": "main_01",
                "output_type": "main",
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": stable_json_sha256(resolved),
                "notes": "字段合同测试",
            }
        ],
    }
    return build_final_prompt_batch_prompt(
        mode="main",
        product_id=product_id,
        repository_root=ROOT,
        identity={"artifact_type": "product_identity_archive"},
        style_master={"artifact_type": "style_master"},
        angle_inventory={
            "angle_slots": [
                {
                    "source_asset_id": "img_001",
                    "angle_slot": "A",
                    "admission_result": "合格，可进入对应槽位",
                }
            ],
            "missing_angle_slots": ["D"],
        },
        variable_config=variable_config,
        requirements=requirements,
    )


class CategoryExecutorContentContractTest(unittest.TestCase):
    def test_every_installed_category_teaches_imported_required_fields_in_consuming_stage(
        self,
    ) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]

        for category_key in category_keys:
            with self.subTest(category=category_key):
                recipe = load_category_recipe(ROOT, category_key)
                main = _stage_text(recipe, "main")
                detail = _stage_text(recipe, "detail")
                final = _final_stage_prompt(recipe)

                self.assertEqual(
                    [],
                    [field for field in MAIN_REQUIRED_OVERRIDE_FIELDS if field not in main],
                    f"品类“{category_key}”主图阶段缺少执行器必填字段教学",
                )
                self.assertEqual(
                    [],
                    [
                        field
                        for field in DETAIL_REQUIRED_OVERRIDE_FIELDS
                        if field not in detail
                    ],
                    f"品类“{category_key}”详情阶段缺少执行器必填字段教学",
                )
                self.assertEqual(
                    [],
                    [field for field in FINAL_PROMPT_ITEM_FIELDS if field not in final],
                    f"品类“{category_key}”最终阶段缺少中央三字段教学",
                )
                self.assertTrue(
                    all(field in final for field in FINAL_BATCH_TOP_LEVEL_FIELDS),
                    f"品类“{category_key}”最终阶段缺少批次包装字段",
                )

    def test_every_required_field_has_substantive_runtime_value_specification(
        self,
    ) -> None:
        category_keys = [item["key"] for item in installed_category_metadata(ROOT)]
        blueprint = load_category_recipe(ROOT, "杯类")

        for category_key in category_keys:
            recipe = load_category_recipe(ROOT, category_key)
            for stage, fields in (
                ("main", MAIN_REQUIRED_OVERRIDE_FIELDS),
                ("detail", DETAIL_REQUIRED_OVERRIDE_FIELDS),
            ):
                stage_text = _stage_text(recipe, stage)
                for field in fields:
                    with self.subTest(
                        category=category_key,
                        stage=stage,
                        field=field,
                    ):
                        blueprint_evidence = _field_teaching_evidence(
                            blueprint,
                            stage,
                            field,
                        )
                        candidate_evidence = _field_teaching_evidence(
                            recipe,
                            stage,
                            field,
                        )
                        self.assertTrue(
                            blueprint_evidence,
                            f"杯类蓝本无法定位字段教学：{stage}/{field}",
                        )
                        self.assertTrue(
                            candidate_evidence,
                            f"品类“{category_key}”运行规则缺少字段正文：{stage}/{field}",
                        )
                        self.assertEqual(
                            [],
                            _substantive_teaching_problems(
                                blueprint_evidence,
                                candidate_evidence,
                            ),
                            (
                                f"品类“{category_key}”存在占位或残缺字段教学："
                                f"{stage}/{field}"
                            ),
                        )
                        for exact_term in INLINE_EXACT_FIELD_TERMS.get(
                            stage,
                            {},
                        ).get(field, ()):
                            exact_scope = (
                                candidate_evidence
                                if field
                                in {
                                    "手持交互声明",
                                    "动态手持样式参考图调用",
                                }
                                else stage_text
                            )
                            self.assertIn(
                                exact_term,
                                exact_scope,
                                (
                                    f"品类“{category_key}”字段精确值缺失："
                                    f"{stage}/{field}/{exact_term}"
                                ),
                            )

    def test_placeholder_field_copy_fails_the_substance_gate(self) -> None:
        blueprint = load_category_recipe(ROOT, "杯类")
        blueprint_evidence = _field_teaching_evidence(
            blueprint,
            "main",
            "产品颜色依据",
        )

        problems = _substantive_teaching_problems(
            blueprint_evidence,
            "产品颜色依据：请填写，并按要求执行。",
        )

        self.assertTrue(problems)
        self.assertTrue(
            any("正文过短" in problem or "占位式教学" in problem for problem in problems)
        )

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
