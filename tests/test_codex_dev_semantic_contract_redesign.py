from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "canvas-bridge"
if str(BRIDGE) not in sys.path:
    sys.path.insert(0, str(BRIDGE))

import codex_dev_downstream as downstream  # noqa: E402
from executor_contract import ExecutorExecutionError  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures"
MAIN_VC_FIXTURE = FIXTURES / "main_vc_reply_20260722_semantic_gate.json"
DETAIL_FINAL_FIXTURE = FIXTURES / "detail_final_prompts_reply_20260722.json"
MAIN_FINAL_FIXTURE = FIXTURES / "main_final_prompts_reply_20260722.json"
PRODUCT_ID = "杯子_20260722"

EXPECTED_FIXTURES = {
    MAIN_VC_FIXTURE: (
        33_839,
        "ae05bc6ae743f9358d46005c9a014ff597d006cf3bf7527213f154fa870b907d",
    ),
    DETAIL_FINAL_FIXTURE: (
        18_397,
        "fbf3518696f51573d376466145b3ab4e9b54adb920163930c62972ed1e35e123",
    ),
    MAIN_FINAL_FIXTURE: (
        15_020,
        "d45dd34b9dd1d679082d69adede5ea75f0438886f2204cb78ecb71fb6ef42229",
    ),
}

_BINDINGS = {
    "main": (
        ("img_001", "B", False),
        ("img_001", "B", False),
        ("img_001", "B", True),
        ("img_002", "C", False),
        ("img_001", "B", True),
        ("img_001", "B", False),
    ),
    "detail": (
        ("img_001", "B", False),
        ("img_001", "B", False),
        ("img_001", "B", True),
        ("img_002", "C", False),
        ("img_001", "B", False),
        ("img_001", "B", False),
        ("img_001", "B", False),
        ("img_001", "B", False),
    ),
}


def requirements() -> downstream.UserConfirmedRequirements:
    return downstream.UserConfirmedRequirements("杯子", 8, 2, 1, False, True, True)


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


def variable_config(mode: str) -> dict[str, object]:
    configs = []
    for index, (asset_id, slot, handheld) in enumerate(_BINDINGS[mode], start=1):
        overrides = {
            "绑定角度槽位": f"绑定唯一合格源图{asset_id}，{slot} 槽位。",
            "手持交互声明": (
                "本张图启用手持场景。手持子场景类型：静态握持。"
                if handheld
                else "本张图不启用手持场景"
            ),
        }
        configs.append(
            {
                "config_id": f"{mode}_{index:02d}",
                "output_type": mode,
                "per_image_overrides": overrides,
                "resolved_variable_config_sha256": downstream.stable_json_sha256(overrides),
            }
        )
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": f"{mode}_variable_config",
        "config_count": len(configs),
        "common_constraints": {},
        "configs": configs,
    }


class CodexDevSemanticContractRedesignTest(unittest.TestCase):
    def test_three_real_fixture_bytes_and_sha256_are_frozen(self) -> None:
        for path, (expected_size, expected_sha) in EXPECTED_FIXTURES.items():
            with self.subTest(path=path.name):
                payload = path.read_bytes()
                self.assertEqual(expected_size, len(payload))
                self.assertEqual(expected_sha, hashlib.sha256(payload).hexdigest())

    def test_real_detail_final_prompt_fixture_passes_full_chain(self) -> None:
        parsed = downstream.parse_final_prompt_batch_response(
            DETAIL_FINAL_FIXTURE.read_text(encoding="utf-8"),
            mode="detail",
            product_id=PRODUCT_ID,
            requirements=requirements(),
            angle_inventory=angle_inventory(),
            variable_config=variable_config("detail"),
            style_master_text="正式风格母版正文",
        )

        self.assertEqual([f"detail_{index:02d}" for index in range(1, 9)], list(parsed))

    def test_real_main_final_prompt_fixture_passes_without_reverse_must_include(self) -> None:
        text = MAIN_FINAL_FIXTURE.read_text(encoding="utf-8")
        parsed = downstream.parse_final_prompt_batch_response(
            text,
            mode="main",
            product_id=PRODUCT_ID,
            requirements=requirements(),
            angle_inventory=angle_inventory(),
            variable_config=variable_config("main"),
            style_master_text="正式风格母版正文",
        )

        self.assertEqual([f"main_{index:02d}" for index in range(1, 7)], list(parsed))
        self.assertFalse(any(term in text for term in downstream._PROHIBITED_ACTION_TERMS))

    def test_negative_list_exempts_material_certification_and_measurements(self) -> None:
        downstream._reject_unsupported_claims(
            {
                "prompts": [
                    {
                        "config_id": "detail_01",
                        "final_prompt": "保持商品外观真实",
                        "negative_prompt": "陶瓷、不锈钢、认证编号、500 毫升",
                    }
                ]
            },
            8,
            "详情图最终提示词",
            product_type="杯子",
        )
        downstream._reject_unsupported_claims(
            {"configs": [{"per_image_overrides": {"禁止事项": "玻璃、300ml、食品级"}}]},
            8,
            "详情图变量配置",
            product_type="杯子",
        )

    def test_negative_list_exempts_scene_terms(self) -> None:
        downstream._reject_scene_policy_violations(
            {
                "prompts": [
                    {
                        "config_id": "detail_01",
                        "final_prompt": "杯子保持空置",
                        "negative_prompt": "清水、倾倒、加热、沸腾、炉灶、热水",
                    }
                ]
            },
            requirements(),
            "详情图最终提示词",
        )

    def test_exact_variable_scene_rule_substring_is_exempt(self) -> None:
        rule = downstream._variable_scene_rule(requirements())
        downstream._reject_scene_policy_violations(
            {"notes": f"场景规则句（原样）：{rule}其余要求保持真实。"},
            requirements(),
            "详情图变量配置",
        )

    def test_positive_scene_counterexamples_still_reject(self) -> None:
        claims = (
            "表现倒水动作",
            "安排加热",
            "在炉灶上加热",
            "水正在沸腾",
            "不锈钢杯用于倒水",
            "不空置，而是倒水",
            "杯中盛有清水",
        )
        for claim in claims:
            with self.subTest(claim=claim):
                with self.assertRaises(ExecutorExecutionError):
                    downstream._reject_scene_policy_violations(
                        {"notes": claim}, requirements(), "详情图变量配置"
                    )

    def test_tightly_coupled_product_material_claims_still_reject(self) -> None:
        for claim in ("杯身为玻璃", "玻璃质感壶身", "陶瓷马克杯", "产品采用不锈钢"):
            with self.subTest(claim=claim):
                with self.assertRaises(ExecutorExecutionError):
                    downstream._reject_unsupported_claims(
                        {"notes": claim},
                        8,
                        "主图变量配置",
                        product_type="杯子",
                    )

    def test_product_marker_without_material_coupling_does_not_reject(self) -> None:
        downstream._reject_unsupported_claims(
            {"notes": "产品搭配玻璃器皿作为后景"},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_material_claim_without_product_subject_defaults_to_allow(self) -> None:
        downstream._reject_unsupported_claims(
            {"per_image_overrides": {"展示重点": "突出陶瓷材质"}},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_non_product_material_defaults_to_allow_without_style_master_phrase(self) -> None:
        downstream._reject_unsupported_claims(
            {"per_image_overrides": {"展示重点": "后景使用玻璃雕塑"}},
            8,
            "主图变量配置",
            product_type="杯子",
            style_master_text="不含该道具描述",
        )

    def test_positive_measurement_gate_and_confirmed_height_behavior_are_preserved(self) -> None:
        with self.assertRaises(ExecutorExecutionError):
            downstream._reject_unsupported_claims(
                {"notes": "容量为 500 毫升"},
                8,
                "主图变量配置",
                product_type="杯子",
            )
        downstream._reject_unsupported_claims(
            {"notes": "产品高度约 8 厘米"},
            8,
            "主图变量配置",
            product_type="杯子",
        )

    def test_negative_list_does_not_hide_positive_sibling_claims(self) -> None:
        with self.assertRaises(ExecutorExecutionError):
            downstream._reject_unsupported_claims(
                {
                    "prompts": [
                        {
                            "config_id": "detail_01",
                            "final_prompt": "杯身为玻璃",
                            "negative_prompt": "玻璃、认证编号",
                        }
                    ]
                },
                8,
                "详情图最终提示词",
                product_type="杯子",
            )


if __name__ == "__main__":
    unittest.main()
