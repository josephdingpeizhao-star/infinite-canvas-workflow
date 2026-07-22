from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import codex_dev_downstream as downstream  # noqa: E402
from executor_contract import ExecutorExecutionError  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "detail_vc_chunk1_20260722.json"
FIXTURE_SHA256 = "19ca5ab590dc1a661d6f6600272ab628301eb82fb067c37a0c5c47890ef2c404"
PRODUCT_ID = "杯子_20260722"
PRODUCT_MATERIAL_TERM_CONTRACT = (
    "所有字段中提及产品材质时，一律使用“材质”统称，不得写出“陶瓷”“玻璃”"
    "“不锈钢”“塑料”等具体材质词；环境道具描述除外，但环境道具仍必须遵守正式"
    "风格母版与现有门禁。"
)
NEW_PROP_CONTEXT_FIELDS = (
    "构图方式",
    "道具密度等级",
    "真实感要求",
    "风格防退化检查",
)
STYLE_MASTER_PROP_TEXT = "背景绿植或空玻璃花器虚化，远处可有玻璃器皿。"


def confirmed_requirements() -> downstream.UserConfirmedRequirements:
    return downstream.parse_user_confirmed_requirements(
        {
            "user_confirmed_facts": {
                "product_type": "杯子",
                "height_cm": 8,
                "handheld_main": 2,
                "handheld_detail": 1,
                "allow_clear_water": False,
                "forbid_pouring_and_heating": True,
                "missing_d_no_retake": True,
            }
        }
    )


def angle_inventory() -> dict[str, object]:
    return {
        "angle_slots": [
            {
                "source_asset_id": "img_001",
                "angle_slot": "B",
                "admission_result": "合格，可进入对应槽位",
            }
        ],
        "missing_angle_slots": ["D"],
    }


def fixture_text() -> str:
    text = FIXTURE.read_text(encoding="utf-8")
    return text[text.index("{") :]


def fixture_chunk() -> dict[str, object]:
    return json.loads(fixture_text())


def complete_detail_response() -> dict[str, object]:
    chunk = fixture_chunk()
    configs = copy.deepcopy(chunk["configs"])
    template = copy.deepcopy(configs[1])
    for index in range(3, 9):
        config = copy.deepcopy(template)
        config["config_id"] = f"detail_{index:02d}"
        overrides = config["per_image_overrides"]
        overrides["标准模块归属"] = f"模块{index:02d} 测试模块"
        overrides["绑定角度槽位"] = (
            "绑定唯一合格源图 img_001，B 槽位；本张只使用该合格源图。"
        )
        overrides["尺寸比例锁定"] = "约 8 厘米"
        if index == 5:
            overrides["尺寸标注信息"] = (
                "高度约 8 厘米；禁止补写容量、宽度、直径、重量、材质"
            )
            overrides["尺寸标注图规则"] = "本图只允许标注高度约 8 厘米"
        else:
            overrides["尺寸标注信息"] = "非尺寸标注图，不启用尺寸标注信息"
            overrides["尺寸标注图规则"] = "非尺寸标注图，不启用"
        if index == 6:
            overrides["手持交互声明"] = (
                "本张图启用手持场景。手持子场景类型：动态拿起。自然握持，不倾倒。"
            )
            overrides["动态手持样式参考图调用"] = "未提供，不调用"
        else:
            overrides["手持交互声明"] = "本张图不启用手持场景"
            overrides["动态手持样式参考图调用"] = "无"
        configs.append(config)
    return {
        "common_constraints": copy.deepcopy(chunk["common_constraints"]),
        "configs": configs,
        "handheld_count_summary": {
            "用户要求详情图手持数量": 1,
            "实际启用手持数量": 1,
            "未启用手持数量": 7,
            "启用手持配置": ["detail_06"],
            "是否完全满足用户数量": "是",
        },
        "notes": str(chunk["notes"]),
    }


def write_style_master(root: Path) -> Path:
    path = root / "style_master.json"
    path.write_text(
        json.dumps(
            {
                "product_id": PRODUCT_ID,
                "artifact_type": "style_master",
                "style_master": {"prop_rules": STYLE_MASTER_PROP_TEXT},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class CodexDevPropContextGuardTest(unittest.TestCase):
    def test_detail_vc_chunk1_20260722_fixture_sha256_is_frozen(self) -> None:
        self.assertEqual(FIXTURE_SHA256, hashlib.sha256(FIXTURE.read_bytes()).hexdigest())

    def test_all_contract_fields_have_exactly_one_semantic_context(self) -> None:
        groups = {
            "final": (
                ("config_id", "final_prompt", "negative_prompt"),
                downstream.FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS,
            ),
            "main": (
                downstream.MAIN_REQUIRED_OVERRIDE_FIELDS,
                downstream.MAIN_VARIABLE_FIELD_SEMANTIC_CONTEXTS,
            ),
            "detail": (
                downstream.DETAIL_REQUIRED_OVERRIDE_FIELDS,
                downstream.DETAIL_VARIABLE_FIELD_SEMANTIC_CONTEXTS,
            ),
        }
        allowed = {
            downstream._SEMANTIC_CONTEXT_POSITIVE,
            downstream._SEMANTIC_CONTEXT_NEGATIVE_LIST,
            downstream._SEMANTIC_CONTEXT_NON_SEMANTIC,
        }

        for group_name, (fields, contexts) in groups.items():
            with self.subTest(group=group_name):
                self.assertEqual(set(fields), set(contexts))
                self.assertTrue(set(contexts.values()) <= allowed)
        self.assertEqual(3, len(downstream.FINAL_PROMPT_FIELD_SEMANTIC_CONTEXTS))
        self.assertEqual(23, len(downstream.MAIN_VARIABLE_FIELD_SEMANTIC_CONTEXTS))
        self.assertEqual(33, len(downstream.DETAIL_VARIABLE_FIELD_SEMANTIC_CONTEXTS))
        self.assertEqual(
            downstream._SEMANTIC_CONTEXT_NEGATIVE_LIST,
            downstream.DETAIL_VARIABLE_FIELD_SEMANTIC_CONTEXTS["禁止事项"],
        )

    def test_real_detail_vc_chunk1_accepts_non_product_material_by_contract(self) -> None:
        parsed = downstream.parse_detail_variable_config_chunk(
            fixture_text(),
            1,
            requirements=confirmed_requirements(),
            angle_inventory=angle_inventory(),
            prior_chunks=[],
        )

        self.assertEqual(["detail_01", "detail_02"], [item["config_id"] for item in parsed["configs"]])
        self.assertIn(
            "玻璃花器",
            parsed["configs"][0]["per_image_overrides"]["风格防退化检查"],
        )

    def test_real_detail_vc_fixture_passes_without_phrase_allowlist_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            style_path = write_style_master(Path(tmp))
            artifact = downstream.parse_variable_config_response(
                json.dumps(complete_detail_response(), ensure_ascii=False),
                mode="detail",
                product_id=PRODUCT_ID,
                requirements=confirmed_requirements(),
                angle_inventory=angle_inventory(),
                upstream_paths={
                    "product_identity_archive": Path("identity.json"),
                    "style_master": style_path,
                    "angle_inventory": Path("angles.json"),
                    "main_variable_configs": Path("main.json"),
                },
            )

        self.assertEqual(8, artifact["config_count"])
        self.assertIn(
            "玻璃花器",
            artifact["configs"][0]["per_image_overrides"]["风格防退化检查"],
        )

    def test_positive_context_fields_reject_product_material_in_chunk_mode(self) -> None:
        for field in NEW_PROP_CONTEXT_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ExecutorExecutionError):
                    downstream._reject_unsupported_claims(
                        {"per_image_overrides": {field: "杯身为玻璃"}},
                        8,
                        "详情图变量配置",
                        product_type="杯子",
                        defer_style_master_prop_materials=True,
                    )

    def test_positive_context_fields_reject_product_material_in_full_mode(self) -> None:
        for field in NEW_PROP_CONTEXT_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ExecutorExecutionError):
                    downstream._reject_unsupported_claims(
                        {"per_image_overrides": {field: "杯身为玻璃"}},
                        8,
                        "详情图变量配置",
                        product_type="杯子",
                    )

    def test_main_variable_prompt_uses_generic_product_material_term(self) -> None:
        prompt = downstream.build_variable_config_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=angle_inventory(),
            requirements=confirmed_requirements(),
        )

        self.assertIn(PRODUCT_MATERIAL_TERM_CONTRACT, prompt)

    def test_detail_variable_prompt_uses_generic_product_material_term(self) -> None:
        prompt = downstream.build_variable_config_prompt(
            mode="detail",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=angle_inventory(),
            requirements=confirmed_requirements(),
            main_variable_config={},
        )

        self.assertIn(PRODUCT_MATERIAL_TERM_CONTRACT, prompt)


if __name__ == "__main__":
    unittest.main()
