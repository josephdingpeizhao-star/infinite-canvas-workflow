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


FIXTURE = ROOT / "tests" / "fixtures" / "main_final_prompts_reply_20260722_r2.json"
FIXTURE_SIZE = 18_615
FIXTURE_SHA256 = "fd56b63fdfe158aade13a3fdbf1cdb3d2fdacbc70ca0a35a3bd675bddc5eb82a"
PRODUCT_ID = "杯子_20260722"
SCENE_SAFETY_COLLECTIVE_CONTRACT = (
    "final_prompt 正文必须遵守以下场景安全规则：表达内容物与动作安全边界时，不得逐词"
    "列举“倾倒、倒水、倒出、加热、沸腾、炉灶、热水”等禁止动作词；统一使用“不出现"
    "任何禁止的内容物或动作”这一统称表述，或原样复述本提示中的场景规则句，不得自行"
    "改写为禁止词清单。"
)
NEGATIVE_PROMPT_ONLY_CONTRACT = (
    "如需逐词列出禁止项，只能写入 negative_prompt 字段；不得把逐词禁止清单写入 "
    "final_prompt 正文。"
)

_MAIN_BINDINGS = (
    ("img_001", "B", False),
    ("img_001", "B", False),
    ("img_001", "B", True),
    ("img_002", "C", False),
    ("img_001", "B", True),
    ("img_001", "B", False),
)
_DETAIL_BINDINGS = (
    ("img_001", "B", False),
    ("img_001", "B", False),
    ("img_001", "B", True),
    ("img_002", "C", False),
    ("img_001", "B", False),
    ("img_001", "B", False),
    ("img_001", "B", False),
    ("img_001", "B", False),
)


def requirements(
    *,
    allow_clear_water: bool = False,
    forbid_pouring_and_heating: bool = True,
) -> downstream.UserConfirmedRequirements:
    return downstream.UserConfirmedRequirements(
        "杯子",
        8,
        2,
        1,
        allow_clear_water,
        forbid_pouring_and_heating,
        True,
    )


def requirement_variants() -> tuple[downstream.UserConfirmedRequirements, ...]:
    return (
        requirements(),
        requirements(allow_clear_water=True, forbid_pouring_and_heating=False),
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


def variable_config(mode: str) -> dict[str, object]:
    bindings = _MAIN_BINDINGS if mode == "main" else _DETAIL_BINDINGS
    configs = []
    for index, (asset_id, slot, handheld) in enumerate(bindings, start=1):
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
                "resolved_variable_config_sha256": downstream.stable_json_sha256(
                    overrides
                ),
            }
        )
    return {
        "product_id": PRODUCT_ID,
        "artifact_type": f"{mode}_variable_config",
        "config_count": len(configs),
        "common_constraints": {},
        "configs": configs,
    }


class CodexDevFinalPromptSceneContractCompletionTest(unittest.TestCase):
    def assert_scene_rejected(self, claim: str) -> None:
        with self.assertRaises(ExecutorExecutionError) as caught:
            downstream._reject_scene_policy_violations(
                {"notes": claim}, requirements(), "主图最终提示词"
            )
        self.assertIn("违反用户确认场景边界", str(caught.exception))

    def build_final_prompt(
        self,
        mode: str,
        confirmed: downstream.UserConfirmedRequirements,
    ) -> str:
        return downstream.build_final_prompt_batch_prompt(
            mode=mode,
            product_id=PRODUCT_ID,
            repository_root=ROOT,
            identity={},
            style_master={},
            angle_inventory=angle_inventory(),
            variable_config=variable_config(mode),
            requirements=confirmed,
        )

    def test_r2_fixture_bytes_and_sha256_are_frozen(self) -> None:
        payload = FIXTURE.read_bytes()

        self.assertEqual(FIXTURE_SIZE, len(payload))
        self.assertEqual(FIXTURE_SHA256, hashlib.sha256(payload).hexdigest())

    def test_r2_fixture_passes_full_main_chain(self) -> None:
        parsed = downstream.parse_final_prompt_batch_response(
            FIXTURE.read_text(encoding="utf-8"),
            mode="main",
            product_id=PRODUCT_ID,
            requirements=requirements(),
            angle_inventory=angle_inventory(),
            variable_config=variable_config("main"),
            style_master_text="正式风格母版正文",
        )

        self.assertEqual([f"main_{index:02d}" for index in range(1, 7)], list(parsed))

    def test_first_item_action_enumeration_is_protected(self) -> None:
        for claim in (
            "不表现倒出、饮用、蒸汽或热饮",
            "未描绘倾倒、饮用或蒸汽",
            "无渲染加热、饮用或蒸汽",
            "不再倒出、饮用",
        ):
            with self.subTest(claim=claim):
                downstream._reject_scene_policy_violations(
                    {"notes": claim}, requirements(), "主图最终提示词"
                )

    def test_first_item_clear_water_enumeration_is_protected(self) -> None:
        for claim in (
            "不表现清水、茶水或其他内容物",
            "未描绘清水、茶水或其他内容物",
        ):
            with self.subTest(claim=claim):
                downstream._reject_scene_policy_violations(
                    {"notes": claim}, requirements(), "主图最终提示词"
                )

    def test_closed_adverb_heads_and_isolated_negations_still_reject(self) -> None:
        for claim in (
            "不慎倒出的水",
            "不小心倒出",
            "不慎倒出、饮用",
            "不小心倒出、饮用",
            "不停倒出、饮用",
            "不断倒出、饮用",
            "不住倒出、饮用",
            "不禁倒出、饮用",
            "不表现倒出",
        ):
            with self.subTest(claim=claim):
                self.assert_scene_rejected(claim)

    def test_fifth_shape_does_not_cross_internal_scope_breakers(self) -> None:
        for claim in (
            "不表现倒出、而加热",
            "不表现倒出、但加热",
            "不表现倒出、却加热",
            "不表现倒出、仍加热",
            "不表现倒出、再加热",
        ):
            with self.subTest(claim=claim):
                self.assert_scene_rejected(claim)

    def test_main_final_prompt_contract_is_dynamic_for_requirement_variants(self) -> None:
        for confirmed in requirement_variants():
            with self.subTest(confirmed=confirmed):
                prompt = self.build_final_prompt("main", confirmed)
                self.assertIn(SCENE_SAFETY_COLLECTIVE_CONTRACT, prompt)
                self.assertIn(
                    "场景规则句（如需复述必须原样）："
                    + downstream._variable_scene_rule(confirmed),
                    prompt,
                )
                self.assertIn(NEGATIVE_PROMPT_ONLY_CONTRACT, prompt)

    def test_detail_final_prompt_contract_is_dynamic_for_requirement_variants(self) -> None:
        for confirmed in requirement_variants():
            with self.subTest(confirmed=confirmed):
                prompt = self.build_final_prompt("detail", confirmed)
                self.assertIn(SCENE_SAFETY_COLLECTIVE_CONTRACT, prompt)
                self.assertIn(
                    "场景规则句（如需复述必须原样）："
                    + downstream._variable_scene_rule(confirmed),
                    prompt,
                )
                self.assertIn(NEGATIVE_PROMPT_ONLY_CONTRACT, prompt)


if __name__ == "__main__":
    unittest.main()
