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


CHUNK1_FIXTURE = ROOT / "tests" / "fixtures" / "detail_vc_chunk1_20260722.json"
CHUNK2_FIXTURE = (
    ROOT / "tests" / "fixtures" / "detail_vc_chunk2_20260722_scene.json"
)
CHUNK2_SHA256 = "f3da8d6fbfde5df02a8eeef37edb74d4b83f6b04eb60e0d5f3069a3805aa4b67"
PRODUCT_ID = "杯子_20260722"
SCENE_SAFETY_COLLECTIVE_CONTRACT = (
    "表达内容物与动作安全边界时，不得逐词列举“倾倒、倒水、倒出、加热、沸腾、"
    "炉灶、热水”等禁止动作词；统一使用“不出现任何禁止的内容物或动作”这一统称"
    "表述，或原样复述本提示中的场景规则句，不得自行改写为禁止词清单。"
)


def fixture_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text[text.index("{") :]


def fixture_json(path: Path) -> dict[str, object]:
    return json.loads(fixture_text(path))


def confirmed_requirements(
    *, handheld_detail: int = 1
) -> downstream.UserConfirmedRequirements:
    return downstream.parse_user_confirmed_requirements(
        {
            "user_confirmed_facts": {
                "product_type": "杯子",
                "height_cm": 8,
                "handheld_main": 2,
                "handheld_detail": handheld_detail,
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
            },
            {
                "source_asset_id": "img_002",
                "angle_slot": "C",
                "admission_result": "合格，可进入对应槽位",
            },
        ],
        "missing_angle_slots": ["D"],
    }


def complete_detail_response() -> dict[str, object]:
    chunk1 = fixture_json(CHUNK1_FIXTURE)
    chunk2 = fixture_json(CHUNK2_FIXTURE)
    configs = copy.deepcopy(chunk1["configs"] + chunk2["configs"])
    template = copy.deepcopy(chunk2["configs"][1])
    for index in range(5, 9):
        config = copy.deepcopy(template)
        config["config_id"] = f"detail_{index:02d}"
        overrides = config["per_image_overrides"]
        overrides["标准模块归属"] = f"模块{index:02d} 测试模块"
        overrides["绑定角度槽位"] = (
            "绑定唯一合格源图 img_002，C 槽位；本张只使用该合格源图。"
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
        overrides["手持交互声明"] = "本张图不启用手持场景"
        overrides["动态手持样式参考图调用"] = "无"
        configs.append(config)
    return {
        "common_constraints": copy.deepcopy(chunk1["common_constraints"]),
        "configs": configs,
        "handheld_count_summary": {
            "用户要求详情图手持数量": 1,
            "实际启用手持数量": 1,
            "未启用手持数量": 7,
            "启用手持配置": ["detail_03"],
            "是否完全满足用户数量": "是",
        },
        "notes": str(chunk1["notes"]),
    }


class CodexDevSceneNegationScopeTest(unittest.TestCase):
    def assert_scene_rejected(self, claim: str) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            downstream._reject_scene_policy_violations(
                {"notes": claim},
                confirmed_requirements(),
                "详情图变量配置",
            )
        self.assertIn("违反用户确认场景边界", str(caught.exception))

    def test_fixture_sha256_is_frozen(self) -> None:
        self.assertEqual(
            CHUNK2_SHA256,
            hashlib.sha256(CHUNK2_FIXTURE.read_bytes()).hexdigest(),
        )

    def test_real_detail_vc_chunk2_passes_chunk_validation(self) -> None:
        chunk = fixture_json(CHUNK2_FIXTURE)
        chunk["handheld_chunk_summary"] = {
            "本段手持配额": 1,
            "本段实际启用数量": 1,
            "本段启用手持配置": ["detail_03"],
        }
        second = downstream.parse_detail_variable_config_chunk(
            json.dumps(chunk, ensure_ascii=False),
            2,
            requirements=confirmed_requirements(handheld_detail=2),
            angle_inventory=angle_inventory(),
        )

        self.assertEqual(
            ["detail_03", "detail_04"],
            [item["config_id"] for item in second["configs"]],
        )

    def test_real_detail_vc_chunk2_passes_full_package_validation(self) -> None:
        response = complete_detail_response()
        with tempfile.TemporaryDirectory() as tmp:
            style_path = Path(tmp) / "style_master.json"
            style_path.write_text(
                json.dumps(
                    {
                        "product_id": PRODUCT_ID,
                        "artifact_type": "style_master",
                        "style_master": {
                            "fixture_reference": json.dumps(
                                response, ensure_ascii=False
                            )
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            artifact = downstream.parse_variable_config_response(
                json.dumps(response, ensure_ascii=False),
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

        self.assertEqual("detail_variable_config", artifact["artifact_type"])
        self.assertEqual(8, artifact["config_count"])

    def test_20260721_unarranged_enumeration_still_passes(self) -> None:
        downstream._reject_scene_policy_violations(
            {"notes": "未安排倾倒、沸腾、炉灶加热或热水动作"},
            confirmed_requirements(),
            "详情图变量配置",
        )

    def test_bounded_negation_head_covers_enumerated_actions(self) -> None:
        downstream._reject_scene_policy_violations(
            {"notes": "不表现饮用、倒水、加热或任何内容物使用"},
            confirmed_requirements(),
            "详情图变量配置",
        )

    def test_clear_water_later_in_negated_enumeration_passes(self) -> None:
        for claim in (
            "不表现茶水、清水或其他内容物",
            "不出现茶水、清水或其他内容物",
        ):
            with self.subTest(claim=claim):
                downstream._reject_scene_policy_violations(
                    {"notes": claim},
                    confirmed_requirements(),
                    "详情图变量配置",
                )

    def test_positive_scene_phrases_still_reject(self) -> None:
        for claim in (
            "表现倒水动作",
            "安排加热",
            "在炉灶上加热",
            "水正在沸腾",
        ):
            with self.subTest(claim=claim):
                self.assert_scene_rejected(claim)

    def test_stainless_steel_prefix_is_not_negation(self) -> None:
        for claim in (
            "不锈钢杯用于倒水",
            "使用不锈钢、倒水动作",
            "不锈钢、倒水动作",
        ):
            with self.subTest(claim=claim):
                self.assert_scene_rejected(claim)

    def test_contrast_breaks_enumeration_scope(self) -> None:
        for claim in (
            "不空置，而是倒水",
            "不空置而是倒水",
            "不表现饮用、但仍、倒水",
            "不表现饮用、再、倒水",
        ):
            with self.subTest(claim=claim):
                self.assert_scene_rejected(claim)

    def test_main_prompt_uses_scene_safety_collective_contract(self) -> None:
        prompt = downstream.build_variable_config_prompt(
            mode="main",
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=angle_inventory(),
            requirements=confirmed_requirements(),
        )

        self.assertIn(SCENE_SAFETY_COLLECTIVE_CONTRACT, prompt)

    def test_detail_prompt_uses_scene_safety_collective_contract(self) -> None:
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

        self.assertIn(SCENE_SAFETY_COLLECTIVE_CONTRACT, prompt)


if __name__ == "__main__":
    unittest.main()
